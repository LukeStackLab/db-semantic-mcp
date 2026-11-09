"""Live, same-session source proof for explicitly configured SQL Server databases."""

import sqlglot
from sqlglot import exp

from ..policy.enforcer import enforce
from ..policy.intents.ast_analyzer import AstIntentAnalyzer
from ..policy.scope import object_allowed, parse_scope_entry


class DatabaseProofError(PermissionError):
    """A fixed diagnostic: never expose driver SQL, descriptors, or secrets."""


def _quote(value):
    return "[" + value.replace("]", "]]") + "]"


def _rows(conn, sql, params=()):
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params) if params else cursor.execute(sql)
        return cursor.fetchall()
    finally:
        cursor.close()


def authorize_cross_database_query(cfg, policy, conn, sql) -> list[str]:
    """Prove all SQL references on the session that will execute the query.

    Config and policy are immutable request snapshots. Unknown metadata is a
    denial, never permission. No caller SQL is run by this function.
    """
    try:
        return _Proof(cfg, policy, conn).authorize(sql)
    except DatabaseProofError:
        raise
    except Exception:  # noqa: BLE001 (driver/catalog details must not escape)
        raise DatabaseProofError("SQL Server source metadata cannot be verified") from None


class _Proof:
    def __init__(self, cfg, policy, conn):
        self.cfg, self.policy, self.conn = cfg, policy, conn
        self.configured = set(cfg.databases)
        self.databases = set()
        self.checked_databases = set()
        self.proven = set()
        self.visiting = set()
        self.default_schema = ""
        self.write_query = False

    def _allowed_database(self, database):
        if database not in self.configured or database not in self.policy.scope.allowed_databases:
            raise DatabaseProofError("Database is not configured or authorized")

    def _analyze(self, sql, database, *, read_only=False):
        intent = AstIntentAnalyzer().analyze(sql)
        result = enforce(intent, self.policy, database=database)
        if not result.allowed or intent.is_multi_statement:
            raise DatabaseProofError("SQL source or operation is outside the authorized scope")
        if read_only and intent.primary_operation.value != "SELECT":
            raise DatabaseProofError("View source is not a supported SELECT")
        for ref in intent.object_references:
            db = ref.database or database
            self._allowed_database(db)
            if intent.primary_operation.value != "SELECT" and db != self.cfg.database:
                raise DatabaseProofError("Cross-database writes are unsupported")
        return intent

    def authorize(self, sql):
        self._allowed_database(self.cfg.database)
        intent = self._analyze(sql, self.cfg.database)
        # Preserve the caller's write semantics across recursive source proof.
        # A synonym/view's SELECT definition cannot turn a write into a read.
        self.write_query = intent.primary_operation.value != "SELECT"
        rows = _rows(self.conn, "SELECT DB_NAME(), SCHEMA_NAME()")
        if len(rows) != 1 or rows[0][0] != self.cfg.database or not rows[0][1]:
            raise DatabaseProofError("Executing session does not match the selected Database")
        self.default_schema = rows[0][1]
        for ref in intent.object_references:
            self._resolve(ref, self.cfg.database)
        return sorted(self.databases)

    def _verify_database(self, database):
        self._allowed_database(database)
        if database in self.checked_databases:
            return
        rows = _rows(
            self.conn,
            "SELECT name, HAS_PERMS_BY_NAME(name, 'DATABASE', 'VIEW DEFINITION'), "
            "HAS_PERMS_BY_NAME(name, 'DATABASE', 'ALTER ANY SECURITY POLICY') "
            "FROM sys.databases WHERE name = %s AND state = 0 AND HAS_DBACCESS(name) = 1",
            (database,),
        )
        # Preserve case-sensitive identity rather than normalizing with casefold.
        if len(rows) != 1 or rows[0][0] != database or rows[0][1] != 1 or rows[0][2] != 1:
            raise DatabaseProofError("Database is unavailable or its identity cannot be verified")
        # VIEW DEFINITION can be denied on individual policies even when granted
        # on the database. ALTER ANY SECURITY POLICY independently establishes
        # visibility of all sys.security_predicates rows. Never grant it here.
        self.checked_databases.add(database)

    def _lookup(self, database, schema, name):
        db = _quote(database)
        return _rows(
            self.conn,
            f"""
SELECT o.object_id, RTRIM(o.type), s.name, o.name, sn.base_object_name, m.definition,
       CASE WHEN ISNULL(HAS_PERMS_BY_NAME(QUOTENAME(%s)+'.'+QUOTENAME(s.name)+'.'+QUOTENAME(o.name), 'OBJECT', 'VIEW DEFINITION'),0)<>1
         OR EXISTS (SELECT 1 FROM {db}.sys.external_tables e WHERE e.object_id=o.object_id)
         OR EXISTS (SELECT 1 FROM {db}.sys.columns c WHERE c.object_id=o.object_id
                    AND (c.is_computed=1 OR c.user_type_id<>c.system_type_id))
         OR EXISTS (SELECT 1 FROM {db}.sys.security_predicates p WHERE p.target_object_id=o.object_id)
         THEN 1 ELSE 0 END,
       CASE WHEN EXISTS (SELECT 1 FROM {db}.sys.triggers t
                         WHERE t.parent_id=o.object_id AND t.is_disabled=0)
         OR EXISTS (SELECT 1 FROM {db}.sys.columns c WHERE c.object_id=o.object_id
                    AND (c.default_object_id<>0 OR c.rule_object_id<>0))
         OR EXISTS (SELECT 1 FROM {db}.sys.check_constraints c WHERE c.parent_object_id=o.object_id)
         OR EXISTS (SELECT 1 FROM {db}.sys.foreign_keys f
                    WHERE f.parent_object_id=o.object_id OR f.referenced_object_id=o.object_id)
         OR EXISTS (SELECT 1 FROM {db}.sys.tables t WHERE t.object_id=o.object_id AND t.temporal_type<>0)
         THEN 1 ELSE 0 END AS unsafe_write
FROM {db}.sys.objects o
JOIN {db}.sys.schemas s ON s.schema_id=o.schema_id
LEFT JOIN {db}.sys.synonyms sn ON sn.object_id=o.object_id
LEFT JOIN {db}.sys.sql_modules m ON m.object_id=o.object_id
WHERE s.name=%s AND o.name=%s
""",
            (database, schema, name),
        )

    def _resolve(self, ref, current_database, depth=0):
        if depth > 64 or len(self.proven) + len(self.visiting) >= 1024:
            raise DatabaseProofError("SQL source dependency limit exceeded")
        database = ref.database or current_database
        self._verify_database(database)
        schemas = [ref.schema] if ref.schema else list(dict.fromkeys([self.default_schema, "dbo"]))
        row = None
        for schema in schemas:
            rows = self._lookup(database, schema, ref.name)
            if rows:
                if len(rows) != 1:
                    raise DatabaseProofError("Ambiguous source metadata")
                row = rows[0]
                break
        if row is None:
            raise DatabaseProofError("Source metadata is missing or inaccessible")
        object_id, kind, schema, name, synonym, definition, unsafe, unsafe_write = row
        if schema not in schemas or name != ref.name or not object_id:
            raise DatabaseProofError("Source identity cannot be verified")
        if not object_allowed(self.policy.scope, database, schema, name):
            raise DatabaseProofError("SQL source is outside the authorized object scope")
        if self.write_query and (database != self.cfg.database or kind != "U" or unsafe_write != 0):
            # Deliberately reject all defaults/checks/FKs, including harmless
            # expressions, until their complete implicit work can be proved.
            # Never evaluate stored expressions as part of authorization.
            raise DatabaseProofError("Write target or implicit dependencies cannot be verified")
        key = (database, schema, name)
        self.databases.add(database)
        if key in self.visiting:
            raise DatabaseProofError("Cyclic SQL source dependencies are unsupported")
        if key in self.proven:
            return
        self.visiting.add(key)
        try:
            if unsafe != 0:
                raise DatabaseProofError("Computed, custom-type, external or policy-driven sources cannot be verified")
            if kind == "U":
                pass
            elif kind == "SN" and synonym:
                self._resolve(parse_scope_entry(synonym), database, depth + 1)
            elif kind == "V" and definition:
                root = sqlglot.parse_one(definition, read="tsql", error_level=sqlglot.ErrorLevel.RAISE)
                if not isinstance(root, exp.Create) or str(root.args.get("kind", "")).upper() != "VIEW":
                    raise DatabaseProofError("View definition cannot be verified")
                query = root.expression
                if not isinstance(query, exp.Query):
                    raise DatabaseProofError("View body cannot be verified")
                # AST serialization is for inspecting the stored definition only;
                # caller SQL is always executed unchanged.
                intent = self._analyze(query.sql(dialect="tsql"), database, read_only=True)
                for dependency in intent.object_references:
                    # Definitions bind unqualified objects under the view schema;
                    # reject unresolved schema rather than assuming a caller default.
                    if not dependency.schema:
                        raise DatabaseProofError("View dependencies require explicit schema evidence")
                    self._resolve(dependency, database, depth + 1)
            else:
                raise DatabaseProofError("SQL source type or definition is unsupported")
        finally:
            self.visiting.remove(key)
        self.proven.add(key)
