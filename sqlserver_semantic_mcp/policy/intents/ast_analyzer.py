"""Strict TSQL authorization evidence, without rewriting caller SQL.

SQLGlot accepts a broader grammar than SQL Server. Only supported statement
roots are authorized. Unknown/control constructs and opaque sources fail closed.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.dialects.tsql import TSQL

from ...domain.enums import RiskLevel, SqlOperation
from ..analyzer import SqlIntent, _compute_risk, _looks_sql_like
from ..readonly_guard import _WRITE_KEYWORDS
from ..scope import ObjectReference, table_reference


_ROOT_OPERATIONS = {
    exp.Select: SqlOperation.SELECT,
    exp.Union: SqlOperation.SELECT,
    exp.Intersect: SqlOperation.SELECT,
    exp.Except: SqlOperation.SELECT,
    exp.Subquery: SqlOperation.SELECT,
    exp.Insert: SqlOperation.INSERT,
    exp.Update: SqlOperation.UPDATE,
    exp.Delete: SqlOperation.DELETE,
    exp.Merge: SqlOperation.MERGE,
    exp.Create: SqlOperation.CREATE,
    exp.Alter: SqlOperation.ALTER,
    exp.Drop: SqlOperation.DROP,
    exp.TruncateTable: SqlOperation.TRUNCATE,
}
_FORBIDDEN_NODES = {
    "Command", "Execute", "ExecuteSql", "Transaction", "Commit", "Rollback",
    "Set", "Use", "Declare", "IfBlock", "WhileBlock", "Block", "Grant",
    "Revoke", "Kill", "Pragma", "Copy", "LoadData", "Export", "Attach",
    "Detach", "NextValueFor", "Returning",
}
_CONTROL_IDENTIFIER_KEYWORDS = set(_WRITE_KEYWORDS) | {
    "OUTPUT", "SET", "USE", "DECLARE", "IF", "ELSE", "WHILE", "PRINT",
    "RETURN", "WAITFOR", "GOTO", "GO", "CALL",
}
_METADATA_FUNCTIONS = {
    "ASSEMBLYPROPERTY", "COL_LENGTH", "COL_NAME", "COLUMNPROPERTY",
    "DATABASEPROPERTY", "DATABASEPROPERTYEX", "SERVERPROPERTY",
    "DB_ID", "DB_NAME", "FILE_ID", "FILE_IDEX", "FILE_NAME", "FILEGROUP_ID",
    "FILEGROUP_NAME", "FILEGROUPPROPERTY", "FILEPROPERTY",
    "FULLTEXTCATALOGPROPERTY", "FULLTEXTSERVICEPROPERTY", "INDEX_COL",
    "IDENT_CURRENT", "IDENT_SEED", "IDENT_INCR", "HAS_PERMS_BY_NAME",
    "INDEXKEY_PROPERTY", "INDEXPROPERTY", "OBJECT_DEFINITION", "OBJECT_ID",
    "OBJECT_NAME", "OBJECT_SCHEMA_NAME", "OBJECTPROPERTY", "OBJECTPROPERTYEX",
    "SCHEMA_ID", "SCHEMA_NAME", "TYPE_ID", "TYPE_NAME", "TYPEPROPERTY",
    # SQLGlot normalizes SCHEMA_NAME to CurrentSchema.
    "CURRENT_SCHEMA", "CURRENT_DATABASE", "SESSION_CONTEXT", "CONTEXT_INFO",
}


class _StrictTSQL(TSQL):
    class Parser(TSQL.Parser):
        def _warn_unsupported(self) -> None:
            # SQLGlot's Command fallback logs the complete caller SQL. Refuse
            # it before that warning rather than muting a process-wide logger.
            raise ValueError("Unsupported TSQL syntax")


def _root_has_top(node: exp.Expression) -> bool:
    if isinstance(node, exp.Subquery):
        return _root_has_top(node.this)
    if isinstance(node, exp.SetOperation):
        return _root_has_top(node.this) and _root_has_top(node.expression)
    return isinstance(node, exp.Select) and node.args.get("limit") is not None


def _direct_tables(node: exp.Expression | None) -> list[exp.Table]:
    """FROM aliases are local; aliases inside a subquery do not identify targets."""
    if node is None or isinstance(node, (exp.Query, exp.Subquery)):
        return []
    result = [node] if isinstance(node, exp.Table) else []
    for child in node.iter_expressions():
        result.extend(_direct_tables(child))
    return result


def _alias_targets(root: exp.Expression) -> set[int]:
    aliases: dict[str, list[exp.Table]] = {}
    if isinstance(root, exp.Update):
        sources = _direct_tables(root.args.get("from_"))
        targets = [root.this]
    elif isinstance(root, exp.Delete):
        sources = _direct_tables(root.this)
        targets = root.args.get("tables") or []
    else:
        return set()
    for table in sources:
        if table.alias:
            aliases.setdefault(table.alias, []).append(table)
    excluded = set()
    for target in targets:
        if not isinstance(target, exp.Table) or target.db or target.catalog:
            continue
        # Alias resolution cannot turn a temporary/dynamic target into a
        # verified persistent object merely because its spelling matches.
        table_reference(target)
        matches = aliases.get(target.name, [])
        if len(matches) > 1:
            raise ValueError("Ambiguous DML target alias")
        if matches:
            excluded.add(id(target))
    return excluded


def _references(root: exp.Expression) -> list[ObjectReference]:
    """Walk lexical CTE scopes and all physical sources, including DML targets.

    CTE bindings are visible only in their statement and nested queries. A
    schema-qualified reference always names a physical object. Exact spelling
    avoids treating a distinct case-sensitive table as a CTE or DML alias.
    """
    refs: list[ObjectReference] = []
    excluded = _alias_targets(root)

    def visit(node: exp.Expression, ctes: frozenset[str]) -> None:
        with_clause = node.args.get("with_")
        local = set(ctes)
        if with_clause is not None:
            declared = set()
            for cte in with_clause.expressions:
                if not isinstance(cte, exp.CTE) or not cte.alias:
                    raise ValueError("Unable to determine CTE scope")
                if cte.alias in declared:
                    raise ValueError("Duplicate CTE binding")
                declared.add(cte.alias)
                # A CTE can refer to earlier bindings and itself recursively.
                local.add(cte.alias)
                visit(cte.this, frozenset(local))
        visible = frozenset(local)
        if isinstance(node, exp.Table) and id(node) not in excluded:
            ref = table_reference(node)
            if ref.schema or ref.database or ref.name not in visible:
                refs.append(ref)
        for key, value in node.args.items():
            if key == "with_":
                continue
            children = value if isinstance(value, list) else [value]
            for child in children:
                if isinstance(child, exp.Expression):
                    visit(child, visible)

    visit(root, frozenset())
    return list(dict.fromkeys(refs))


def _check_tree(root: exp.Expression) -> list[SqlOperation]:
    op = _ROOT_OPERATIONS.get(type(root))
    if op is None:
        raise ValueError(f"Unsupported SQL statement: {type(root).__name__}")
    if isinstance(root, (exp.Create, exp.Drop, exp.Alter)):
        if str(root.args.get("kind", "")).upper() not in {"TABLE", "VIEW"}:
            raise ValueError("Only TABLE/VIEW DDL can be scoped safely")
    required = [op]
    if isinstance(root, exp.Create) and root.args.get("replace"):
        # TSQL CREATE OR ALTER can modify an existing object. CREATE permission
        # alone cannot authorize the ALTER branch without knowing DB state.
        required.append(SqlOperation.ALTER)
    for node in root.walk():
        if (isinstance(node, exp.Identifier) and not node.args.get("quoted")
                and node.name.upper() in _CONTROL_IDENTIFIER_KEYWORDS):
            # The permissive parser can absorb DELETE/COMMIT as SELECT aliases
            # even though SQL Server treats them as another statement. Such
            # tokens cannot stand for identifiers in authorization evidence.
            raise ValueError("Unquoted write/control keyword cannot be authorized as an identifier")
        if type(node).__name__ in _FORBIDDEN_NODES:
            raise ValueError(f"Unsupported SQL construct: {type(node).__name__}")
        if isinstance(node, exp.Into):
            raise ValueError("SELECT INTO is not supported")
        if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func):
            raise ValueError("User-defined function scope cannot be verified")
        if node is not root and isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Merge)):
            if not isinstance(root, exp.Merge) or not isinstance(node.parent, exp.When):
                raise ValueError("Nested writes are not supported")
            required.append(_ROOT_OPERATIONS[type(node)])
    return list(dict.fromkeys(required))


def _statement_intent(root: exp.Expression) -> SqlIntent:
    required = _check_tree(root)
    refs = _references(root)
    op = _ROOT_OPERATIONS[type(root)]
    has_where = root.args.get("where") is not None
    contains_cte = root.find(exp.CTE) is not None
    metadata_functions = sorted({
        name for node in root.find_all(exp.Func)
        if (name := (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).upper())
        in _METADATA_FUNCTIONS
    })
    simple_write = (
        not contains_cte and root.args.get("returning") is None
        and not any(isinstance(n, exp.Query) for n in root.walk() if n is not root)
        and (
            isinstance(root, exp.Insert) and isinstance(root.expression, exp.Values)
            or isinstance(root, exp.Update) and root.args.get("from_") is None
        )
    )
    return SqlIntent(
        primary_operation=op, has_where_clause=has_where,
        has_top_clause=_root_has_top(root),
        affected_tables=[ref.display_name for ref in refs], object_references=refs,
        required_operations=required, is_simple_write=bool(simple_write),
        metadata_functions=metadata_functions,
        risk_level=_compute_risk(op, has_where, False),
        has_unqualified_tables=any(not ref.schema for ref in refs),
        contains_cte=contains_cte, confidence=1.0,
    )


class AstIntentAnalyzer:
    name = "ast"

    def analyze(self, sql: str) -> SqlIntent:
        try:
            roots = [root for root in _StrictTSQL().parse(
                sql, error_level=sqlglot.ErrorLevel.RAISE,
            ) if root is not None and not isinstance(root, exp.Semicolon)]
            if not roots:
                raise ValueError("No SQL statement was found")
            statements = [_statement_intent(root) for root in roots]
        except Exception as exc:
            # Parser errors include SQL fragments. Keep only our controlled
            # messages in public responses; never echo parser exception text.
            reason = str(exc) if type(exc) is ValueError else "SQL could not be parsed safely"
            return SqlIntent(
                primary_operation=SqlOperation.UNKNOWN, has_where_clause=False,
                has_top_clause=False, statement_count=0,
                risk_level=RiskLevel.HIGH, is_sql_like=_looks_sql_like(sql),
                confidence=0.0, requires_discovery=True, authorization_error=reason,
            )
        first = statements[0]
        refs = list(dict.fromkeys(ref for item in statements for ref in item.object_references))
        return SqlIntent(
            primary_operation=first.primary_operation,
            has_where_clause=first.has_where_clause, has_top_clause=first.has_top_clause,
            affected_tables=[ref.display_name for ref in refs], object_references=refs,
            risk_level=max(
                [item.risk_level for item in statements]
                + [_compute_risk(first.primary_operation, first.has_where_clause, len(statements) > 1)],
                key=lambda risk: list(RiskLevel).index(risk),
            ),
            is_multi_statement=len(statements) > 1, statement_count=len(statements),
            confidence=1.0, contains_cte=any(item.contains_cte for item in statements),
            has_unqualified_tables=any(not ref.schema for ref in refs),
            statement_intents=statements, is_simple_write=first.is_simple_write,
        )
