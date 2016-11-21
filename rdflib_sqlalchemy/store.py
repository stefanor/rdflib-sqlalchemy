"""SQLAlchemy-based RDF store."""
from __future__ import with_statement

import hashlib
import logging
import re

import sqlalchemy
from rdflib import (
    BNode,
    Literal,
    URIRef
)
from rdflib.graph import Graph, QuotedGraph
from rdflib.namespace import RDF
from rdflib.plugins.stores.regexmatching import PYTHON_REGEX, REGEXTerm
from rdflib.store import CORRUPTED_STORE, VALID_STORE, Store
from six import text_type
from six.moves import reduce
from sqlalchemy import MetaData
from sqlalchemy.engine import reflection
from sqlalchemy.sql import select, expression

from rdflib_sqlalchemy.tables import (
    TABLE_NAME_TEMPLATES,
    create_asserted_statements_table,
    create_literal_statements_table,
    create_namespace_binds_table,
    create_quoted_statements_table,
    create_type_statements_table,
)
from rdflib_sqlalchemy.termutils import (
    REVERSE_TERM_COMBINATIONS,
    TERM_INSTANTIATION_DICT,
    construct_graph,
    type_to_term_combination,
    statement_to_term_combination,
)


_logger = logging.getLogger(__name__)

COUNT_SELECT = 0
CONTEXT_SELECT = 1
TRIPLE_SELECT = 2
TRIPLE_SELECT_NO_ORDER = 3

ASSERTED_NON_TYPE_PARTITION = 3
ASSERTED_TYPE_PARTITION = 4
QUOTED_PARTITION = 5
ASSERTED_LITERAL_PARTITION = 6

FULL_TRIPLE_PARTITIONS = [QUOTED_PARTITION, ASSERTED_LITERAL_PARTITION]

INTERNED_PREFIX = "kb_"

Any = None


# Stolen from Will Waites' py4s
def skolemise(statement):
    """Skolemise."""
    def _sk(x):
        if isinstance(x, BNode):
            return URIRef("bnode:%s" % x)
        return x
    return tuple(map(_sk, statement))


def deskolemise(statement):
    """Deskolemise."""
    def _dst(x):
        if isinstance(x, URIRef) and x.startswith("bnode:"):
            _unused, bnid = x.split(":", 1)
            return BNode(bnid)
        return x
    return tuple(map(_dst, statement))


def regexp(expr, item):
    """User-defined REGEXP operator."""
    r = re.compile(expr)
    return r.match(item) is not None


def query_analysis(query, store, connection):
    """
    Helper function.

    For executing EXPLAIN on all dispatched SQL statements -
    for the pupose of analyzing index usage.

    """
    res = connection.execute("explain " + query)
    rt = res.fetchall()[0]
    table, joinType, posKeys, _key, key_len, \
        comparedCol, rowsExamined, extra = rt
    if not _key:
        assert joinType == "ALL"
        if not hasattr(store, "queryOptMarks"):
            store.queryOptMarks = {}
        hits = store.queryOptMarks.get(("FULL SCAN", table), 0)
        store.queryOptMarks[("FULL SCAN", table)] = hits + 1

    if not hasattr(store, "queryOptMarks"):
        store.queryOptMarks = {}
    hits = store.queryOptMarks.get((_key, table), 0)
    store.queryOptMarks[(_key, table)] = hits + 1


def union_select(selectComponents, distinct=False, select_type=TRIPLE_SELECT):
    """
    Helper function for building union all select statement.

    Terms: u - uri refs  v - variables  b - bnodes l - literal f - formula

    Takes a list of:
     - table name
     - table alias
     - table type (literal, type, asserted, quoted)
     - where clause string
    """
    selects = []
    for table, whereClause, tableType in selectComponents:

        if select_type == COUNT_SELECT:
            selectClause = table.count(whereClause)
        elif select_type == CONTEXT_SELECT:
            selectClause = expression.select([table.c.context], whereClause)
        elif tableType in FULL_TRIPLE_PARTITIONS:
            selectClause = table.select(whereClause)
        elif tableType == ASSERTED_TYPE_PARTITION:
            selectClause = expression.select(
                [table.c.id.label("id"),
                 table.c.member.label("subject"),
                 expression.literal(text_type(RDF.type)).label("predicate"),
                 table.c.klass.label("object"),
                 table.c.context.label("context"),
                 table.c.termComb.label("termcomb"),
                 expression.literal_column("NULL").label("objlanguage"),
                 expression.literal_column("NULL").label("objdatatype")],
                whereClause)
        elif tableType == ASSERTED_NON_TYPE_PARTITION:
            selectClause = expression.select(
                [c for c in table.columns] +
                [expression.literal_column("NULL").label("objlanguage"),
                 expression.literal_column("NULL").label("objdatatype")],
                whereClause,
                from_obj=[table])

        selects.append(selectClause)

    order_statement = []
    if select_type == TRIPLE_SELECT:
        order_statement = [
            expression.literal_column("subject"),
            expression.literal_column("predicate"),
            expression.literal_column("object"),
        ]
    if distinct:
        return expression.union(*selects, **{"order_by": order_statement})
    else:
        return expression.union_all(*selects, **{"order_by": order_statement})


def extractTriple(tupleRt, store, hardCodedContext=None):
    """
    Extract a triple.

    Take a tuple which represents an entry in a result set and
    converts it to a tuple of terms using the termComb integer
    to interpret how to instantiate each term
    """
    try:
        id, subject, predicate, obj, rtContext, termComb, \
            objLanguage, objDatatype = tupleRt
        termCombString = REVERSE_TERM_COMBINATIONS[termComb]
        subjTerm, predTerm, objTerm, ctxTerm = termCombString
    except ValueError:
        id, subject, subjTerm, predicate, predTerm, obj, objTerm, \
            rtContext, ctxTerm, objLanguage, objDatatype = tupleRt

    context = rtContext is not None \
        and rtContext \
        or hardCodedContext.identifier
    s = createTerm(subject, subjTerm, store)
    p = createTerm(predicate, predTerm, store)
    o = createTerm(obj, objTerm, store, objLanguage, objDatatype)

    graphKlass, idKlass = construct_graph(ctxTerm)

    return id, s, p, o, (graphKlass, idKlass, context)


def createTerm(
        termString, termType, store, objLanguage=None, objDatatype=None):
    # TODO: Stuff
    """
    Take a term value, term type, and store instance and creates a term object.

    QuotedGraphs are instantiated differently
    """
    if termType == "L":
        cache = store.literalCache.get((termString, objLanguage, objDatatype))
        if cache is not None:
            # store.cacheHits += 1
            return cache
        else:
            # store.cacheMisses += 1
            # rt = Literal(termString, objLanguage, objDatatype)
            # store.literalCache[((termString, objLanguage, objDatatype))] = rt
            if objLanguage and not objDatatype:
                rt = Literal(termString, objLanguage)
                store.literalCache[((termString, objLanguage))] = rt
            elif objDatatype and not objLanguage:
                rt = Literal(termString, datatype=objDatatype)
                store.literalCache[((termString, objDatatype))] = rt
            elif not objLanguage and not objDatatype:
                rt = Literal(termString)
                store.literalCache[((termString))] = rt
            else:
                rt = Literal(termString, objDatatype)
                store.literalCache[((termString, objDatatype))] = rt
            return rt
    elif termType == "F":
        cache = store.otherCache.get((termType, termString))
        if cache is not None:
            # store.cacheHits += 1
            return cache
        else:
            # store.cacheMisses += 1
            rt = QuotedGraph(store, URIRef(termString))
            store.otherCache[(termType, termString)] = rt
            return rt
    elif termType == "B":
        cache = store.bnodeCache.get((termString))
        if cache is not None:
            # store.cacheHits += 1
            return cache
        else:
            # store.cacheMisses += 1
            rt = TERM_INSTANTIATION_DICT[termType](termString)
            store.bnodeCache[(termString)] = rt
            return rt
    elif termType == "U":
        cache = store.uriCache.get((termString))
        if cache is not None:
            # store.cacheHits += 1
            return cache
        else:
            # store.cacheMisses += 1
            rt = URIRef(termString)
            store.uriCache[(termString)] = rt
            return rt
    else:
        cache = store.otherCache.get((termType, termString))
        if cache is not None:
            # store.cacheHits += 1
            return cache
        else:
            # store.cacheMisses += 1
            rt = TERM_INSTANTIATION_DICT[termType](termString)
            store.otherCache[(termType, termString)] = rt
            return rt


class SQLGenerator(object):
    """SQL statement generator."""

    def _build_type_sql_command(self, member, klass, context):
        """Build an insert command for a type table."""
        # columns: member,klass,context
        rt = self.tables["type_statements"].insert()
        return rt, {
            "member": member,
            "klass": klass,
            "context": context.identifier,
            "termComb": int(type_to_term_combination(member, klass, context))}

    def _build_literal_triple_sql_command(self, subject, predicate, obj, context):
        """
        Build an insert command for literal triples.

        These triples correspond to RDF statements where the object is a Literal,
        e.g. `rdflib.Literal`.

        """
        triple_pattern = int(
            statement_to_term_combination(subject, predicate, obj, context)
        )
        command = self.tables["literal_statements"].insert()
        values = {
            "subject": subject,
            "predicate": predicate,
            "object": obj,
            "context": context.identifier,
            "termComb": triple_pattern,
            "objLanguage": isinstance(obj, Literal) and obj.language or None,
            "objDatatype": isinstance(obj, Literal) and obj.datatype or None,
        }
        return command, values

    def _build_triple_sql_command(self, subject, predicate, obj, context, quoted):
        """
        Build an insert command for regular triple table.

        """
        stmt_table = (quoted and
                      self.tables["quoted_statements"] or
                      self.tables["asserted_statements"])

        triple_pattern = statement_to_term_combination(
            subject,
            predicate,
            obj,
            context,
        )
        command = stmt_table.insert()

        if quoted:
            params = {
                "subject": subject,
                "predicate": predicate,
                "object": obj,
                "context": context.identifier,
                "termComb": triple_pattern,
                "objLanguage": isinstance(
                    obj, Literal) and obj.language or None,
                "objDatatype": isinstance(
                    obj, Literal) and obj.datatype or None
            }
        else:
            params = {
                "subject": subject,
                "predicate": predicate,
                "object": obj,
                "context": context.identifier,
                "termComb": triple_pattern,
            }
        return command, params

    def buildClause(
            self, table, subject, predicate, obj, context=None,
            typeTable=False):
        """Build WHERE clauses for the supplied terms and, context."""
        if typeTable:
            clauseList = [
                self.buildTypeMemberClause(subject, table),
                self.buildTypeClassClause(obj, table),
                self.buildContextClause(context, table)
            ]
        else:
            clauseList = [
                self.buildSubjClause(subject, table),
                self.buildPredClause(predicate, table),
                self.buildObjClause(obj, table),
                self.buildContextClause(context, table),
                self.buildLitDTypeClause(obj, table),
                self.buildLitLanguageClause(obj, table)
            ]

        clauseList = [clause for clause in clauseList if clause is not None]
        if clauseList:
            return expression.and_(*clauseList)
        else:
            return None

    def buildLitDTypeClause(self, obj, table):
        """Build Literal and datatype clause."""
        if isinstance(obj, Literal) and obj.datatype is not None:
            return table.c.objDatatype == obj.datatype
        else:
            return None

    def buildLitLanguageClause(self, obj, table):
        """Build Literal and language clause."""
        if isinstance(obj, Literal) and obj.language is not None:
            return table.c.objLanguage == obj.language
        else:
            return None

    # Where Clause  utility Functions
    # The predicate and object clause builders are modified in order
    # to optimize subjects and objects utility functions which can
    # take lists as their last argument (object, predicate -
    # respectively)
    def buildSubjClause(self, subject, table):
        """Build Subject clause."""
        if isinstance(subject, REGEXTerm):
            # TODO: this work only in mysql. Must adapt for postgres and sqlite
            return table.c.subject.op("REGEXP")(subject)
        elif isinstance(subject, list):
            # clauseStrings = [] --- unused
            return expression.or_(
                *[self.buildSubjClause(s, table) for s in subject if s])
        elif isinstance(subject, (QuotedGraph, Graph)):
            return table.c.subject == subject.identifier
        elif subject is not None:
            return table.c.subject == subject
        else:
            return None

    def buildPredClause(self, predicate, table):
        """
        Build Predicate clause.

        Capable of taking a list of predicates as well (in which case
        subclauses are joined with 'OR')
        """
        if isinstance(predicate, REGEXTerm):
            # TODO: this work only in mysql. Must adapt for postgres and sqlite
            return table.c.predicate.op("REGEXP")(predicate)
        elif isinstance(predicate, list):
            return expression.or_(
                *[self.buildPredClause(p, table) for p in predicate if p])
        elif predicate is not None:
            return table.c.predicate == predicate
        else:
            return None

    def buildObjClause(self, obj, table):
        """
        Build Object clause.

        Capable of taking a list of objects as well (in which case subclauses
        are joined with 'OR')
        """
        if isinstance(obj, REGEXTerm):
            # TODO: this work only in mysql. Must adapt for postgres and sqlite
            return table.c.object.op("REGEXP")(obj)
        elif isinstance(obj, list):
            return expression.or_(
                *[self.buildObjClause(o, table) for o in obj if o])
        elif isinstance(obj, (QuotedGraph, Graph)):
            return table.c.object == obj.identifier
        elif obj is not None:
            return table.c.object == obj
        else:
            return None

    def buildContextClause(self, context, table):
        """Build Context clause."""
        if isinstance(context, REGEXTerm):
            # TODO: this work only in mysql. Must adapt for postgres and sqlite
            return table.c.context.op("regexp")(context.identifier)
        elif context is not None and context.identifier is not None:
            return table.c.context == context.identifier
        else:
            return None

    def buildTypeMemberClause(self, subject, table):
        """Build Type Member clause."""
        if isinstance(subject, REGEXTerm):
            # TODO: this work only in mysql. Must adapt for postgres and sqlite
            return table.c.member.op("regexp")(subject)
        elif isinstance(subject, list):
            return expression.or_(
                *[self.buildTypeMemberClause(s, table) for s in subject if s])
        elif subject is not None:
            return table.c.member == subject
        else:
            return None

    def buildTypeClassClause(self, obj, table):
        """Build Type Class clause."""
        if isinstance(obj, REGEXTerm):
            # TODO: this work only in mysql. Must adapt for postgres and sqlite
            return table.c.klass.op("regexp")(obj)
        elif isinstance(obj, list):
            return expression.or_(
                *[self.buildTypeClassClause(o, table) for o in obj if o])
        elif obj is not None:
            return obj and table.c.klass == obj
        else:
            return None


class SQLAlchemy(Store, SQLGenerator):
    """
    SQL-92 formula-aware implementation of an rdflib Store.

    It stores its triples in the following partitions:

    - Asserted non rdf:type statements
    - Asserted literal statements
    - Asserted rdf:type statements (in a table which models Class membership)
        The motivation for this partition is primarily query speed and
        scalability as most graphs will always have more rdf:type statements
        than others
    - All Quoted statements

    In addition it persists namespace mappings in a separate table
    """

    context_aware = True
    formula_aware = True
    transaction_aware = True
    regex_matching = PYTHON_REGEX
    configuration = Literal("sqlite://")

    def __init__(self, identifier=None, configuration=None, engine=None):
        """
        Initialisation.

        Args:
            identifier (rdflib.URIRef): URIRef of the Store. Defaults to CWD.
            engine (sqlalchemy.engine.Engine, optional): a `SQLAlchemy.engine.Engine` instance

        """
        self.identifier = identifier and identifier or "hardcoded"
        self.engine = engine

        # Use only the first 10 bytes of the digest
        self._interned_id = "{prefix}{identifier_hash}".format(
            prefix=INTERNED_PREFIX,
            identifier_hash=hashlib.sha1(self.identifier.encode("utf8")).hexdigest()[:10],
        )

        # This parameter controls how exlusively the literal table is searched
        # If true, the Literal partition is searched *exclusively* if the
        # object term in a triple pattern is a Literal or a REGEXTerm.  Note,
        # the latter case prevents the matching of URIRef nodes as the objects
        # of a triple in the store.
        # If the object term is a wildcard (None)
        # Then the Literal paritition is searched in addition to the others
        # If this parameter is false, the literal partition is searched
        # regardless of what the object of the triple pattern is
        self.STRONGLY_TYPED_TERMS = False

        self.cacheHits = 0
        self.cacheMisses = 0
        self.literalCache = {}
        self.uriCache = {}
        self.bnodeCache = {}
        self.otherCache = {}
        self.__node_pickler = None

        self._create_table_definitions()

        # XXX For backward compatibility we still support getting the connection string in constructor
        # TODO: deprecate this once refactoring is more mature
        if configuration:
            self.open(configuration)

    @property
    def table_names(self):
        return [
            table_name_template.format(interned_id=self._interned_id)
            for table_name_template in TABLE_NAME_TEMPLATES
        ]

    def _get_node_pickler(self):
        if getattr(self, "_node_pickler", False) \
                or self._node_pickler is None:
            from rdflib.term import URIRef
            from rdflib.graph import GraphValue
            from rdflib.term import Variable
            from rdflib.term import Statement
            from rdflib.store import NodePickler
            self._node_pickler = np = NodePickler()
            np.register(self, "S")
            np.register(URIRef, "U")
            np.register(BNode, "B")
            np.register(Literal, "L")
            np.register(Graph, "G")
            np.register(QuotedGraph, "Q")
            np.register(Variable, "V")
            np.register(Statement, "s")
            np.register(GraphValue, "v")
        return self._node_pickler
    node_pickler = property(_get_node_pickler)

    def open(self, configuration, create=True):
        """
        Open the store specified by the configuration string.

        Args:
            create (bool): If create is True a store will be created if it does not already
                exist. If create is False and a store does not already exist
                an exception is raised. An exception is also raised if a store
                exists, but there is insufficient permissions to open the
                store.

        Returns:
            int: CORRUPTED_STORE (0) if database exists but is empty,
                 VALID_STORE (1) if database exists and tables are all there,
                 NO_STORE (-1) if nothing exists

        """
        # Close any existing engine connection
        self.close()

        self.engine = sqlalchemy.create_engine(configuration)
        with self.engine.connect():
            if create:
                # Create all of the database tables (idempotent)
                self.metadata.create_all(self.engine)

            ret_value = self.verify_store_exists()

        if ret_value != VALID_STORE and not create:
            raise RuntimeError("open() - create flag was set to False, but store was not created previously.")

        return ret_value

    def verify_store_exists(self):
        """
        Verify store (e.g. all tables) exist.

        """

        inspector = reflection.Inspector.from_engine(self.engine)
        existing_table_names = inspector.get_table_names()
        for table_name in self.table_names:
            if table_name not in existing_table_names:
                _logger.critical("create_all() - table %s Doesn't exist!", table_name)
                # The database exists, but one of the tables doesn't exist
                return CORRUPTED_STORE

        return VALID_STORE

    def close(self, commit_pending_transaction=False):
        """
        Close the current store engine connection if one is open.

        """
        self.engine = None

    def destroy(self, configuration):
        """
        Delete all tables and stored data associated with the store.

        """
        if self.engine is None:
            self.engine = self.open(configuration, create=False)

        with self.engine.connect() as connection:
            trans = connection.begin()
            try:
                self.metadata.drop_all(self.engine)
                trans.commit()
            except Exception:
                _logger.exception("unable to drop table.")
                trans.rollback()

    def _get_build_command(self, triple, context=None, quoted=False):
        """
        Assemble the SQL Query text for adding an RDF triple to store.

        :param triple {tuple} - tuple of (subject, predicate, object) objects to add
        :param context - a `rdflib.URIRef` identifier for the graph namespace
        :param quoted {bool} - whether should treat as a quoted statement

        :returns {tuple} of (command_type, add_command, params):
            command_type: which kind of statement it is: literal, type, other
            statement: the literal SQL statement to execute (with unbound variables)
            params: the parameters for the SQL statement (e.g the variables to bind)

        """
        subject, predicate, obj = triple
        command_type = None
        if quoted or predicate != RDF.type:
            # Quoted statement or non rdf:type predicate
            # check if object is a literal
            if isinstance(obj, Literal):
                statement, params = self._build_literal_triple_sql_command(
                    subject,
                    predicate,
                    obj,
                    context,
                )
                command_type = "literal"
            else:
                statement, params = self._build_triple_sql_command(
                    subject,
                    predicate,
                    obj,
                    context,
                    quoted,
                )
                command_type = "other"
        elif predicate == RDF.type:
            # asserted rdf:type statement
            statement, params = self._build_type_sql_command(
                subject,
                obj,
                context,
            )
            command_type = "type"
        return command_type, statement, params

    # Triple Methods

    def add(self, triple, context=None, quoted=False):
        """Add a triple to the store of triples."""
        subject, predicate, obj = triple
        _, statement, params = self._get_build_command(
            (subject, predicate, obj),
            context, quoted,
        )

        with self.engine.connect() as connection:
            try:
                connection.execute(statement, params)
            except Exception:
                _logger.exception(
                    "Add failed with statement: %s, params: %s",
                    str(statement), repr(params)
                )
                raise

    def addN(self, quads):
        """Add a list of triples in quads form."""
        commands_dict = {}
        for subject, predicate, obj, context in quads:
            command_type, statement, params = \
                self._get_build_command(
                    (subject, predicate, obj),
                    context,
                    isinstance(context, QuotedGraph),
                )

            command_dict = commands_dict.setdefault(command_type, {})
            command_dict.setdefault("statement", statement)
            command_dict.setdefault("params", []).append(params)

        with self.engine.connect() as connection:
            trans = connection.begin()
            try:
                for command in commands_dict.values():
                    connection.execute(command["statement"], command["params"])
                trans.commit()
            except Exception:
                _logger.exception("AddN failed.")
                trans.rollback()
                raise

    def remove(self, triple, context):
        """Remove a triple from the store."""
        subject, predicate, obj = triple

        if context is not None:
            if subject is None and predicate is None and object is None:
                self._remove_context(context)
                return

        quoted_table = self.tables["quoted_statements"]
        asserted_table = self.tables["asserted_statements"]
        asserted_type_table = self.tables["type_statements"]
        literal_table = self.tables["literal_statements"]

        with self.engine.connect() as connection:
            trans = connection.begin()
            try:
                if not predicate or predicate != RDF.type:
                    # Need to remove predicates other than rdf:type

                    if not self.STRONGLY_TYPED_TERMS \
                            or isinstance(obj, Literal):
                        # remove literal triple
                        clause = self.buildClause(
                            literal_table, subject, predicate, obj, context)
                        connection.execute(literal_table.delete(clause))

                    for table in [quoted_table, asserted_table]:
                        # If asserted non rdf:type table and obj is Literal,
                        # don't do anything (already taken care of)
                        if table == asserted_table \
                                and isinstance(obj, Literal):
                            continue
                        else:
                            clause = self.buildClause(
                                table, subject, predicate, obj, context)
                            connection.execute(table.delete(clause))

                if predicate == RDF.type or not predicate:
                    # Need to check rdf:type and quoted partitions (in addition
                    # perhaps)
                    clause = self.buildClause(
                        asserted_type_table, subject,
                        RDF.type, obj, context, True)
                    connection.execute(asserted_type_table.delete(clause))

                    clause = self.buildClause(
                        quoted_table, subject, predicate, obj, context)
                    connection.execute(quoted_table.delete(clause))

                trans.commit()
            except Exception:
                _logger.exception("Removal failed.")
                trans.rollback()

    def triples(self, triple, context=None):
        """
        A generator over all the triples matching pattern.

        Pattern can be any objects for comparing against nodes in
        the store, for example, RegExLiteral, Date? DateRange?

        quoted table:                <id>_quoted_statements
        asserted rdf:type table:     <id>_type_statements
        asserted non rdf:type table: <id>_asserted_statements

        triple columns:
            subject, predicate, object, context, termComb, objLanguage, objDatatype
        class membership columns:
            member, klass, context, termComb

        FIXME:  These union all selects *may* be further optimized by joins

        """
        subject, predicate, obj = triple

        quoted_table = self.tables["quoted_statements"]
        asserted_table = self.tables["asserted_statements"]
        asserted_type_table = self.tables["type_statements"]
        literal_table = self.tables["literal_statements"]

        if predicate == RDF.type:
            # select from asserted rdf:type partition and quoted table
            # (if a context is specified)
            typeTable = expression.alias(
                asserted_type_table, "typetable")
            clause = self.buildClause(
                typeTable, subject, RDF.type, obj, context, True)
            selects = [
                (typeTable,
                 clause,
                 ASSERTED_TYPE_PARTITION), ]

        elif isinstance(predicate, REGEXTerm) \
                and predicate.compiledExpr.match(RDF.type) \
                or not predicate:
            # Select from quoted partition (if context is specified),
            # Literal partition if (obj is Literal or None) and asserted
            # non rdf:type partition (if obj is URIRef or None)
            selects = []
            if not self.STRONGLY_TYPED_TERMS \
                    or isinstance(obj, Literal) \
                    or not obj \
                    or (self.STRONGLY_TYPED_TERMS and isinstance(obj, REGEXTerm)):
                literal = expression.alias(literal_table, "literal")
                clause = self.buildClause(
                    literal, subject, predicate, obj, context)
                selects.append((literal, clause, ASSERTED_LITERAL_PARTITION))

            if not isinstance(obj, Literal) \
                    and not (isinstance(obj, REGEXTerm) and self.STRONGLY_TYPED_TERMS) \
                    or not obj:
                asserted = expression.alias(asserted_table, "asserted")
                clause = self.buildClause(
                    asserted, subject, predicate, obj, context)
                selects.append((asserted, clause, ASSERTED_NON_TYPE_PARTITION))

            typeTable = expression.alias(asserted_type_table, "typetable")
            clause = self.buildClause(
                typeTable, subject, RDF.type, obj, context, True)
            selects.append((typeTable, clause, ASSERTED_TYPE_PARTITION))

        elif predicate:
            # select from asserted non rdf:type partition (optionally),
            # quoted partition (if context is specified), and literal
            # partition (optionally)
            selects = []
            if not self.STRONGLY_TYPED_TERMS \
                    or isinstance(obj, Literal) \
                    or not obj \
                    or (self.STRONGLY_TYPED_TERMS and isinstance(obj, REGEXTerm)):
                literal = expression.alias(literal_table, "literal")
                clause = self.buildClause(
                    literal, subject, predicate, obj, context)
                selects.append((literal, clause, ASSERTED_LITERAL_PARTITION))

            if not isinstance(obj, Literal) \
                    and not (isinstance(obj, REGEXTerm) and self.STRONGLY_TYPED_TERMS) \
                    or not obj:
                asserted = expression.alias(asserted_table, "asserted")
                clause = self.buildClause(
                    asserted, subject, predicate, obj, context)
                selects.append((asserted, clause, ASSERTED_NON_TYPE_PARTITION))

        if context is not None:
            quoted = expression.alias(quoted_table, "quoted")
            clause = self.buildClause(quoted, subject, predicate, obj, context)
            selects.append((quoted, clause, QUOTED_PARTITION))

        q = union_select(selects, select_type=TRIPLE_SELECT_NO_ORDER)
        with self.engine.connect() as connection:
            res = connection.execute(q)
            # TODO: False but it may have limitations on text column. Check
            # NOTE: SQLite does not support ORDER BY terms that aren't
            # integers, so the entire result set must be iterated in order
            # to be able to return a generator of contexts
            result = res.fetchall()
        tripleCoverage = {}
        for rt in result:
            id, s, p, o, (graphKlass, idKlass, graphId) = extractTriple(rt, self, context)
            contexts = tripleCoverage.get((s, p, o), [])
            contexts.append(graphKlass(self, idKlass(graphId)))
            tripleCoverage[(s, p, o)] = contexts

        for (s, p, o), contexts in tripleCoverage.items():
            yield (s, p, o), (c for c in contexts)

    def triples_choices(self, triple, context=None):
        """
        A variant of triples.

        Can take a list of terms instead of a single term in any slot.
        Stores can implement this to optimize the response time from the
        import default 'fallback' implementation, which will iterate over
        each term in the list and dispatch to triples.
        """
        subject, predicate, object_ = triple

        if isinstance(object_, list):
            assert not isinstance(
                subject, list), "object_ / subject are both lists"
            assert not isinstance(
                predicate, list), "object_ / predicate are both lists"
            if not object_:
                object_ = None
            for (s1, p1, o1), cg in self.triples(
                    (subject, predicate, object_), context):
                yield (s1, p1, o1), cg

        elif isinstance(subject, list):
            assert not isinstance(
                predicate, list), "subject / predicate are both lists"
            if not subject:
                subject = None
            for (s1, p1, o1), cg in self.triples(
                    (subject, predicate, object_), context):
                yield (s1, p1, o1), cg

        elif isinstance(predicate, list):
            assert not isinstance(
                subject, list), "predicate / subject are both lists"
            if not predicate:
                predicate = None
            for (s1, p1, o1), cg in self.triples(
                    (subject, predicate, object_), context):
                yield (s1, p1, o1), cg

    def __repr__(self):
        """Readable serialisation."""
        quoted_table = self.tables["quoted_statements"]
        asserted_table = self.tables["asserted_statements"]
        asserted_type_table = self.tables["type_statements"]
        literal_table = self.tables["literal_statements"]

        selects = [
            (expression.alias(asserted_type_table, "typetable"),
                None, ASSERTED_TYPE_PARTITION),
            (expression.alias(quoted_table, "quoted"),
                None, QUOTED_PARTITION),
            (expression.alias(asserted_table, "asserted"),
                None, ASSERTED_NON_TYPE_PARTITION),
            (expression.alias(literal_table, "literal"),
                None, ASSERTED_LITERAL_PARTITION), ]
        q = union_select(selects, distinct=False, select_type=COUNT_SELECT)
        if hasattr(self, "engine"):
            with self.engine.connect() as connection:
                res = connection.execute(q)
                rt = res.fetchall()
                typeLen, quotedLen, assertedLen, literalLen = [
                    rtTuple[0] for rtTuple in rt]
            try:
                return ("<Partitioned SQL N3 Store: %s " +
                        "contexts, %s classification assertions, " +
                        "%s quoted statements, %s property/value " +
                        "assertions, and %s other assertions>" % (
                            len([ctx for ctx in self.contexts()]),
                            typeLen, quotedLen, literalLen, assertedLen))
            except Exception:
                return "<Partitioned SQL N3 Store>"
        else:
            return "<Partitioned unopened SQL N3 Store>"

    def __len__(self, context=None):
        """Number of statements in the store."""
        quoted_table = self.tables["quoted_statements"]
        asserted_table = self.tables["asserted_statements"]
        asserted_type_table = self.tables["type_statements"]
        literal_table = self.tables["literal_statements"]

        typetable = expression.alias(asserted_type_table, "typetable")
        quoted = expression.alias(quoted_table, "quoted")
        asserted = expression.alias(asserted_table, "asserted")
        literal = expression.alias(literal_table, "literal")

        quotedContext = self.buildContextClause(context, quoted)
        assertedContext = self.buildContextClause(context, asserted)
        typeContext = self.buildContextClause(context, typetable)
        literalContext = self.buildContextClause(context, literal)

        if context is not None:
            selects = [
                (typetable, typeContext,
                 ASSERTED_TYPE_PARTITION),
                (quoted, quotedContext,
                 QUOTED_PARTITION),
                (asserted, assertedContext,
                 ASSERTED_NON_TYPE_PARTITION),
                (literal, literalContext,
                 ASSERTED_LITERAL_PARTITION), ]
            q = union_select(selects, distinct=True, select_type=COUNT_SELECT)
        else:
            selects = [
                (typetable, typeContext,
                 ASSERTED_TYPE_PARTITION),
                (asserted, assertedContext,
                 ASSERTED_NON_TYPE_PARTITION),
                (literal, literalContext,
                 ASSERTED_LITERAL_PARTITION), ]
            q = union_select(selects, distinct=False, select_type=COUNT_SELECT)

        with self.engine.connect() as connection:
            res = connection.execute(q)
            rt = res.fetchall()
            return reduce(lambda x, y: x + y, [rtTuple[0] for rtTuple in rt])

    def contexts(self, triple=None):
        """Contexts."""
        quoted_table = self.tables["quoted_statements"]
        asserted_table = self.tables["asserted_statements"]
        asserted_type_table = self.tables["type_statements"]
        literal_table = self.tables["literal_statements"]

        typetable = expression.alias(asserted_type_table, "typetable")
        quoted = expression.alias(quoted_table, "quoted")
        asserted = expression.alias(asserted_table, "asserted")
        literal = expression.alias(literal_table, "literal")

        if triple is not None:
            subject, predicate, obj = triple
            if predicate == RDF.type:
                # Select from asserted rdf:type partition and quoted table
                # (if a context is specified)
                clause = self.buildClause(
                    typetable, subject, RDF.type, obj, Any, True)
                selects = [(typetable, clause, ASSERTED_TYPE_PARTITION), ]

            elif isinstance(predicate, REGEXTerm) \
                    and predicate.compiledExpr.match(RDF.type) \
                    or not predicate:
                # Select from quoted partition (if context is specified),
                # literal partition if (obj is Literal or None) and
                # asserted non rdf:type partition (if obj is URIRef
                # or None)
                clause = self.buildClause(
                    typetable, subject, RDF.type, obj, Any, True)
                selects = [(typetable, clause, ASSERTED_TYPE_PARTITION), ]

                if (not self.STRONGLY_TYPED_TERMS or
                        isinstance(obj, Literal) or
                        not obj or
                        (self.STRONGLY_TYPED_TERMS and isinstance(obj, REGEXTerm))):
                    clause = self.buildClause(literal, subject, predicate, obj)
                    selects.append(
                        (literal, clause, ASSERTED_LITERAL_PARTITION))
                if not isinstance(obj, Literal) \
                        and not (isinstance(obj, REGEXTerm) and self.STRONGLY_TYPED_TERMS) \
                        or not obj:
                    clause = self.buildClause(
                        asserted, subject, predicate, obj)
                    selects.append(
                        (asserted, clause, ASSERTED_NON_TYPE_PARTITION))

            elif predicate:
                # select from asserted non rdf:type partition (optionally),
                # quoted partition (if context is speciied), and literal
                # partition (optionally)
                selects = []
                if (not self.STRONGLY_TYPED_TERMS or
                        isinstance(obj, Literal) or
                        not obj
                        or (self.STRONGLY_TYPED_TERMS and isinstance(obj, REGEXTerm))):
                    clause = self.buildClause(
                        literal, subject, predicate, obj)
                    selects.append(
                        (literal, clause, ASSERTED_LITERAL_PARTITION))
                if not isinstance(obj, Literal) \
                        and not (isinstance(obj, REGEXTerm) and self.STRONGLY_TYPED_TERMS) \
                        or not obj:
                    clause = self.buildClause(
                        asserted, subject, predicate, obj)
                    selects.append(
                        (asserted, clause, ASSERTED_NON_TYPE_PARTITION))

            clause = self.buildClause(quoted, subject, predicate, obj)
            selects.append((quoted, clause, QUOTED_PARTITION))
            q = union_select(selects, distinct=True, select_type=CONTEXT_SELECT)
        else:
            selects = [
                (typetable, None, ASSERTED_TYPE_PARTITION),
                (quoted, None, QUOTED_PARTITION),
                (asserted, None, ASSERTED_NON_TYPE_PARTITION),
                (literal, None, ASSERTED_LITERAL_PARTITION), ]
            q = union_select(selects, distinct=True, select_type=CONTEXT_SELECT)

        with self.engine.connect() as connection:
            res = connection.execute(q)
            rt = res.fetchall()
        for context in [rtTuple[0] for rtTuple in rt]:
            yield URIRef(context)

    def _remove_context(self, identifier):
        """Remove context."""
        assert identifier
        quoted_table = self.tables["quoted_statements"]
        asserted_table = self.tables["asserted_statements"]
        asserted_type_table = self.tables["type_statements"]
        literal_table = self.tables["literal_statements"]

        with self.engine.connect() as connection:
            trans = connection.begin()
            try:
                for table in [quoted_table, asserted_table,
                              asserted_type_table, literal_table]:
                    clause = self.buildContextClause(identifier, table)
                    connection.execute(table.delete(clause))
                trans.commit()
            except Exception:
                _logger.exception("Context removal failed.")
                trans.rollback()

    # Optional Namespace methods

    # Placeholder optimized interfaces (those needed in order to port Versa)
    def subjects(self, predicate=None, obj=None):
        """A generator of subjects with the given predicate and object."""
        raise Exception("Not implemented")

    # Capable of taking a list of predicate terms instead of a single term
    def objects(self, subject=None, predicate=None):
        """A generator of objects with the given subject and predicate."""
        raise Exception("Not implemented")

    # Optimized interfaces (others)
    def predicate_objects(self, subject=None):
        """A generator of (predicate, object) tuples for the given subject."""
        raise Exception("Not implemented")

    def subject_objects(self, predicate=None):
        """A generator of (subject, object) tuples for the given predicate."""
        raise Exception("Not implemented")

    def subject_predicates(self, object=None):
        """A generator of (subject, predicate) tuples for the given object."""
        raise Exception("Not implemented")

    def value(self, subject,
              predicate=u"http://www.w3.org/1999/02/22-rdf-syntax-ns#value",
              object=None, default=None, any=False):
        """
        Get a value.

        For a subject/predicate, predicate/object, or
        subject/object pair -- exactly one of subject, predicate,
        object must be None. Useful if one knows that there may only
        be one value.

        It is one of those situations that occur a lot, hence this
        'macro' like utility

        :param subject:
        :param predicate:
        :param object:  -- exactly one must be None
        :param default: -- value to be returned if no values found
        :param any: -- if true, return any value in the case there is more
                       than one, else raise a UniquenessError
        """
        raise Exception("Not implemented")

    # Namespace persistence interface implementation

    def bind(self, prefix, namespace):
        """Bind prefix for namespace."""
        with self.engine.connect() as connection:
            try:
                ins = self.tables["namespace_binds"].insert().values(
                    prefix=prefix, uri=namespace)
                connection.execute(ins)
            except Exception:
                _logger.exception("Namespace binding failed.")

    def prefix(self, namespace):
        """Prefix."""
        with self.engine.connect() as connection:
            nb_table = self.tables["namespace_binds"]
            namespace = text_type(namespace)
            s = select([nb_table.c.prefix]).where(nb_table.c.uri == namespace)
            res = connection.execute(s)
            rt = [rtTuple[0] for rtTuple in res.fetchall()]
            res.close()
            return rt and rt[0] or None

    def namespace(self, prefix):
        """Namespace."""
        res = None
        prefix_val = text_type(prefix)
        try:
            with self.engine.connect() as connection:
                nb_table = self.tables["namespace_binds"]
                s = select([nb_table.c.uri]).where(nb_table.c.prefix == prefix_val)
                res = connection.execute(s)
                rt = [rtTuple[0] for rtTuple in res.fetchall()]
                res.close()
                # return rt and rt[0] or None
                from rdflib import URIRef
                return rt and URIRef(rt[0]) or None
        except:
            return None

    def namespaces(self):
        """Namespaces."""
        with self.engine.connect() as connection:
            res = connection.execute(self.tables["namespace_binds"].select())
            for prefix, uri in res.fetchall():
                yield prefix, uri

    def _create_table_definitions(self):
        self.metadata = MetaData()
        self.tables = {
            "asserted_statements": create_asserted_statements_table(self._interned_id, self.metadata),
            "type_statements": create_type_statements_table(self._interned_id, self.metadata),
            "literal_statements": create_literal_statements_table(self._interned_id, self.metadata),
            "quoted_statements": create_quoted_statements_table(self._interned_id, self.metadata),
            "namespace_binds": create_namespace_binds_table(self._interned_id, self.metadata),
        }
