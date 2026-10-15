import re
from pathlib import Path

from ..config import Config, get_config
from ..policy.analyzer import SqlIntent
from ..policy.enforcer import enforce
from ..policy.intents import get_analyzer
from ..policy.intents.ast_analyzer import AstIntentAnalyzer
from ..policy.loader import apply_env_overrides, load_active_policy
from ..policy.models import PolicyFile, PolicyProfile, PolicyScope
from ..policy.readonly_guard import (
    is_select_only,
    keyword_hits_in_masked,
    keywords_in_masked,
    mask_literals,
)
from ..policy.scope import object_allowed, oracle_object_allowed
from ..setup.files import read_snapshot


class PolicyService:
    def __init__(self, cfg: Config | None = None) -> None:
        self._cfg = cfg or get_config()
        self._policy: PolicyProfile | None = None
        self._analyzer = get_analyzer(self._cfg)
        self._authorization_analyzer = (
            get_analyzer(self._cfg) if self._cfg.database_type == "oracle" else AstIntentAnalyzer()
        )

    def load(self) -> None:
        if not self._cfg.connection_name:
            self._policy = load_active_policy(self._cfg)
            return
        try:
            pf = PolicyFile.model_validate_json(read_snapshot(Path(self._cfg.policy_file)))
            policy = pf.profiles[self._cfg.policy_profile]
            permitted = {"select"}
            if self._cfg.policy_profile == "read_write_safe":
                permitted.update({"insert", "update"})
            if any(value and name not in permitted for name, value in policy.operations.model_dump().items()):
                raise ValueError("Policy exceeds fixed profile permissions")
            if policy.constraints.allow_multi_statement or not policy.constraints.require_where_for_update:
                raise ValueError("Unsafe fixed-profile constraints")
            policy = apply_env_overrides(policy, self._cfg)
        except Exception:  # noqa: BLE001 (controlled error replacement)
            raise ValueError("Fixed connection policy is missing or invalid; refusing to load") from None
        self._policy = policy

    def reload(self) -> None:
        self.load()
        self._analyzer = get_analyzer(self._cfg)

    def current_policy(self) -> PolicyProfile:
        if self._policy is None:
            self.load()
        assert self._policy is not None
        return self._policy

    def analyze(self, sql: str) -> SqlIntent:
        return self._analyzer.analyze(sql)

    def effective_scope(self):
        """Metadata scope for the selected source; route selection is not a grant."""
        policy = self.current_policy()
        if self._cfg.source_id == "local":
            return policy.scope
        if policy.scope.allowed_databases and self._cfg.database not in policy.scope.allowed_databases:
            raise PermissionError("Database is not enabled by the global policy")
        source = policy.scope.sources.get(self._cfg.source_id)
        if source is None or not source.operations.select or not policy.operations.select:
            raise PermissionError("Remote source metadata requires explicit SELECT policy")
        return source

    def object_allowed(self, schema: str, name: str, database: str = "") -> bool:
        if self._cfg.database_type == "oracle":
            return oracle_object_allowed(self.effective_scope(), database or self._cfg.database, schema, name)
        return object_allowed(self.current_policy().scope, database or self._cfg.database, schema, name)

    def authorize_object(self, schema: str, name: str, database: str = "") -> None:
        if not self.object_allowed(schema, name, database):
            raise PermissionError(f"Object '{schema}.{name}' is denied by policy scope")

    def validate(
        self, sql: str, database: str = "", intent: SqlIntent | None = None, *, conn=None, resolve_sources=True
    ) -> dict:
        if self._cfg.database_type == "oracle":
            return self._validate_oracle(sql, database, conn=conn, resolve_sources=resolve_sources)
        policy = self.current_policy()
        if intent is None or not isinstance(self._analyzer, AstIntentAnalyzer):
            # Routing reuses the strict AST analysis only when the routing
            # analyzer is the AST analyzer; otherwise authorize independently.
            intent = self._authorization_analyzer.analyze(sql)
        result = enforce(intent, policy, database=database or self._cfg.database)
        allowed, reason = result.allowed, result.reason
        denial = None if allowed else _denial("policy", result.rule, reason, result.detail)
        masked = None
        hits = None
        keywords = None
        if (allowed and self._cfg.connection_name) or is_select_only(policy):
            masked = mask_literals(sql)
            hits = keyword_hits_in_masked(masked)
            keywords = list(dict.fromkeys(h["keyword"] for h in hits))
        if allowed and self._cfg.connection_name:
            allowed, reason = self._check_fixed_query(intent, masked, keywords)
            if not allowed:
                denial = _denial(
                    "fixed_profile",
                    "fixed_profile_rule",
                    reason,
                    _keyword_detail(hits),
                )
        if is_select_only(policy):
            if keywords:
                allowed = False
                reason = f"Select-only policy: batch contains write/control keyword(s): {', '.join(keywords)}"
                denial = _denial(
                    "keyword_guard",
                    "write_keyword_in_select_only",
                    reason,
                    _keyword_detail(hits),
                )
        out = {
            "allowed": allowed,
            "reason": reason,
            "intent": intent_to_dict(intent),
        }
        if denial:
            out["denial"] = denial
        return out

    def _validate_oracle(self, sql, database, *, conn=None, resolve_sources=True):
        from dataclasses import replace

        from ..infrastructure.backends.oracle_resolution import OracleResolver
        from ..infrastructure.connection import open_connection

        intent = self._authorization_analyzer.analyze(sql)
        policy = self.current_policy()
        # Operations and row constraints apply globally; live source resolution
        # applies local/remote object scopes separately to every alias and target.
        base = policy.model_copy(update={"scope": PolicyScope(allowed_databases=policy.scope.allowed_databases)})
        result = enforce(
            replace(intent, object_references=[], affected_tables=[]), base, database=database or self._cfg.database
        )
        allowed, reason, rule = result.allowed, result.reason, result.rule
        resolved = None
        if allowed and self._cfg.connection_name and intent.primary_operation.value != "SELECT":
            if self._cfg.policy_profile != "read_write_safe" or not intent.is_simple_write:
                allowed, reason, rule = False, "Fixed profile does not enable this write", "fixed_profile_rule"
        if allowed and resolve_sources:
            try:

                def authorize(connection):
                    resolver = OracleResolver(self._cfg.for_source("local"), connection, policy)
                    for ref in intent.object_references:
                        resolver.resolve(ref, intent.primary_operation.value)
                    from ..policy.intents.oracle_analyzer import parse_oracle

                    resolver.verify_columns(parse_oracle(sql)[0])
                    return resolver.result

                if conn is not None:
                    resolved = authorize(conn)
                else:
                    with open_connection(self._cfg.for_source("local")) as connection:
                        resolved = authorize(connection)
            except Exception:  # noqa: BLE001 (catalog/driver details can contain credentials)
                allowed, reason, rule = (
                    False,
                    "Oracle source, synonym target or operation cannot be authorized",
                    "object_scope",
                )
        out = {
            "allowed": allowed,
            "reason": reason,
            "intent": intent_to_dict(intent),
            "source_resolution": "verified_session" if resolved else "pending" if allowed else "denied",
        }
        if resolved:
            out["resolved_objects"] = [
                {"source": r.source, "schema": r.schema, "name": r.name} for r in resolved.references
            ]
            out["sources"] = sorted({r.source for r in resolved.references})
        if not allowed:
            hints = {
                "sql_not_authorizable": "Use one Oracle SELECT or simple INSERT VALUES / UPDATE WHERE. Custom functions, PL/SQL, NEXTVAL, FOR UPDATE and RETURNING are unsupported.",
                "top_required": "Add FETCH FIRST n ROWS ONLY to every outer SELECT branch.",
                "object_scope": "Use explicitly configured DB links and permit every synonym alias and physical target in its source scope. Quote case-sensitive Oracle names with double quotes.",
            }
            out["denial"] = _denial("oracle_policy", rule, reason, result.detail)
            out["denial"]["hint"] = hints.get(
                rule, "Use an Oracle operation and source explicitly enabled by the active policy."
            )
        return out

    def _check_fixed_query(self, intent: SqlIntent, masked: str, keywords: list[str] | None = None) -> tuple[bool, str]:
        op = intent.primary_operation.value
        if keywords is None:
            keywords = keywords_in_masked(masked)
        if op == "SELECT":
            if keywords:
                return False, "Fixed profile: SELECT contains write/control keywords"
            return True, "Query is allowed"
        if self._cfg.policy_profile != "read_write_safe" or op not in {"INSERT", "UPDATE"}:
            return False, "Fixed profile permits SELECT and explicitly enabled INSERT/UPDATE only"
        if not intent.is_simple_write:
            return (
                False,
                "Fixed profile writes require a single simple INSERT VALUES or UPDATE; subqueries/CTEs/OUTPUT are not supported",
            )
        allowed_keywords = {op, "INTO"} if op == "INSERT" else {op}
        verbs = re.findall(r"\b(?:SELECT|INSERT|UPDATE|DELETE|MERGE|OUTPUT|WITH)\b", masked, re.IGNORECASE)
        if set(keywords) - allowed_keywords or [v.upper() for v in verbs] != [op]:
            return (
                False,
                "Fixed profile writes require a single simple INSERT VALUES or UPDATE; subqueries/CTEs/OUTPUT are not supported",
            )
        if op == "UPDATE" and not re.search(r"\bWHERE\b", masked, re.IGNORECASE):
            return False, "UPDATE requires a WHERE clause"
        return True, "Query is allowed"


_HINTS = {
    "sql_not_authorizable": "The SQL could not be proven safe. Use plain single-statement T-SQL (optionally preceded by DECLARE/SET @variable statements) and avoid dynamic SQL, EXEC, temp tables and SELECT INTO.",
    "multi_statement": "Send one statement per call, or use DECLARE/SET @variable statements followed by one SELECT.",
    "database_scope": "The profile only allows the databases listed in policy scope.allowed_databases.",
    "operation_unknown": "The statement type was not recognized; check the SQL syntax.",
    "operation_not_allowed": "Enable this operation in the active policy profile (operations.<flag>) or use a profile that allows it.",
    "metadata_function_scope": "Metadata functions can reveal objects outside the allowed scope. Use describe_table / get_tables instead, or remove the scope restrictions.",
    "where_required": "Add a WHERE clause, or disable the matching constraints.require_where_* flag.",
    "top_required": "Add TOP to every outer SELECT branch, or disable constraints.require_top_for_select.",
    "object_scope": "Reference only objects permitted by scope.allowed_*/denied_tables and schema-qualify them (schema.table).",
    "object_scope_unknown": "Schema-qualify every table (schema.table).",
    "write_keyword_in_select_only": "A select-only profile rejects write/control keywords anywhere in the batch. See denial.matches for the exact position; quote an identifier with [brackets] if it is only a name.",
    "fixed_profile_rule": "Fixed connection profiles accept SELECT, plus simple INSERT VALUES / UPDATE ... WHERE when the profile enables writes.",
}


def _keyword_detail(hits: list[dict]) -> dict:
    return {"matches": hits[:10]} if hits else {}


def _denial(layer: str, rule: str, reason: str, detail: dict) -> dict:
    return {
        "layer": layer,
        "rule": rule or "unspecified",
        "message": reason,
        "hint": _HINTS.get(rule, ""),
        **detail,
    }


def intent_to_dict(intent: SqlIntent) -> dict:
    return {
        "primary_operation": intent.primary_operation.value,
        "has_where_clause": intent.has_where_clause,
        "has_top_clause": intent.has_top_clause,
        "affected_tables": intent.affected_tables,
        "risk_level": intent.risk_level.value,
        "is_multi_statement": intent.is_multi_statement,
        "statement_count": intent.statement_count,
        "is_sql_like": intent.is_sql_like,
        "confidence": intent.confidence,
        "requires_discovery": intent.requires_discovery,
        "has_unqualified_tables": intent.has_unqualified_tables,
        "contains_dynamic_sql": intent.contains_dynamic_sql,
        "contains_cte": intent.contains_cte,
        "variable_statements": intent.variable_statements,
    }
