from dataclasses import dataclass

from ..domain.enums import SqlOperation
from .analyzer import SqlIntent
from .models import PolicyProfile
from .scope import database_allowed, object_allowed, parse_scope_entry


@dataclass
class EnforcementResult:
    allowed: bool
    reason: str


_OP_FIELD = {
    SqlOperation.SELECT:   "select",
    SqlOperation.INSERT:   "insert",
    SqlOperation.UPDATE:   "update",
    SqlOperation.DELETE:   "delete",
    SqlOperation.TRUNCATE: "truncate",
    SqlOperation.CREATE:   "create",
    SqlOperation.ALTER:    "alter",
    SqlOperation.DROP:     "drop",
    SqlOperation.EXEC:     "execute",
    SqlOperation.EXECUTE:  "execute",
    SqlOperation.MERGE:    "merge",
}


def enforce(
    intent: SqlIntent, policy: PolicyProfile, database: str = "",
) -> EnforcementResult:
    if intent.authorization_error:
        return EnforcementResult(False, intent.authorization_error)
    if intent.is_multi_statement and not policy.constraints.allow_multi_statement:
        return EnforcementResult(False, "Multi-statement queries are not allowed")
    if not database_allowed(policy.scope, database):
        return EnforcementResult(False, f"Database '{database}' is not allowed by policy")
    # AST evidence carries one intent per complete statement. Legacy regex
    # intents remain usable by direct callers; execution always supplies AST.
    statements = intent.statement_intents or [intent]
    for statement in statements:
        result = _enforce_statement(statement, policy, database)
        if not result.allowed:
            return result
    return EnforcementResult(True, "Query is allowed")


def _enforce_statement(intent: SqlIntent, policy: PolicyProfile, database: str) -> EnforcementResult:
    if intent.authorization_error:
        return EnforcementResult(False, intent.authorization_error)
    if intent.primary_operation == SqlOperation.UNKNOWN:
        return EnforcementResult(False, "Unable to determine SQL operation")
    for op in intent.required_operations or [intent.primary_operation]:
        field = _OP_FIELD.get(op)
        if field is None or not getattr(policy.operations, field, False):
            return EnforcementResult(False, f"Operation {op.value} is not allowed by policy")

    scope = policy.scope
    if intent.metadata_functions and any((scope.allowed_databases, scope.allowed_schemas,
                                          scope.allowed_tables, scope.denied_tables)):
        # IDs, dynamic object arguments, and scalar metadata functions can
        # reveal objects absent from FROM/JOIN evidence. Restricted profiles
        # must use the authorized metadata tools instead.
        return EnforcementResult(False, "Metadata/context functions cannot be authorized within a restricted object scope")

    op = intent.primary_operation
    constraints = policy.constraints
    if op == SqlOperation.UPDATE and constraints.require_where_for_update and not intent.has_where_clause:
        return EnforcementResult(False, "UPDATE requires a WHERE clause")
    if op == SqlOperation.DELETE and constraints.require_where_for_delete and not intent.has_where_clause:
        return EnforcementResult(False, "DELETE requires a WHERE clause")
    if op == SqlOperation.SELECT and constraints.require_top_for_select and not intent.has_top_clause:
        return EnforcementResult(False, "SELECT requires a TOP clause on every outer query branch")

    refs = intent.object_references
    if not refs and intent.affected_tables:
        try:
            refs = [parse_scope_entry(table) for table in intent.affected_tables]
        except ValueError:
            return EnforcementResult(False, "Unable to determine object scope")
    for ref in refs:
        db = ref.database or database
        if not object_allowed(policy.scope, db, ref.schema, ref.name):
            return EnforcementResult(
                False, f"Object '{ref.display_name}' is denied or outside the allowed policy scope; "
                "schema-qualified names are required when schemas are restricted",
            )
    return EnforcementResult(True, "Query is allowed")
