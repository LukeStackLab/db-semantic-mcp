from typing import Optional
from pathlib import Path
import re

from ..config import Config, get_config
from ..policy.analyzer import SqlIntent
from ..policy.enforcer import enforce
from ..policy.intents import get_analyzer
from ..policy.loader import apply_env_overrides, load_active_policy
from ..policy.models import PolicyFile, PolicyProfile
from ..policy.readonly_guard import find_write_keywords, is_select_only, mask_literals
from ..setup.files import read_snapshot


class PolicyService:
    def __init__(self, cfg: Optional[Config] = None) -> None:
        self._cfg = cfg or get_config()
        self._policy: Optional[PolicyProfile] = None
        self._analyzer = get_analyzer(self._cfg)

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
        except Exception:
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

    def validate(self, sql: str, database: str = "") -> dict:
        policy = self.current_policy()
        intent = self._analyzer.analyze(sql)
        result = enforce(intent, policy, database=database)
        allowed, reason = result.allowed, result.reason
        if allowed and self._cfg.connection_name:
            allowed, reason = self._check_fixed_query(sql, intent)
        if allowed and is_select_only(policy):
            hits = find_write_keywords(sql)
            if hits:
                allowed = False
                reason = (
                    "Select-only policy: batch contains write/control "
                    f"keyword(s): {', '.join(hits)}"
                )
        return {
            "allowed": allowed,
            "reason": reason,
            "intent": intent_to_dict(intent),
        }

    def _check_fixed_query(self, sql: str, intent: SqlIntent) -> tuple[bool, str]:
        op = intent.primary_operation.value
        hits = find_write_keywords(sql)
        if op == "SELECT":
            if hits:
                return False, "Fixed profile: SELECT contains write/control keywords"
            return True, "Query is allowed"
        if self._cfg.policy_profile != "read_write_safe" or op not in {"INSERT", "UPDATE"}:
            return False, "Fixed profile permits SELECT and explicitly enabled INSERT/UPDATE only"
        allowed_keywords = {op, "INTO"} if op == "INSERT" else {op}
        clean = mask_literals(sql)
        verbs = re.findall(r"\b(?:SELECT|INSERT|UPDATE|DELETE|MERGE|OUTPUT|WITH)\b", clean, re.IGNORECASE)
        if set(hits) - allowed_keywords or [v.upper() for v in verbs] != [op]:
            return False, "Fixed profile writes require a single simple INSERT VALUES or UPDATE; subqueries/CTEs/OUTPUT are not supported"
        if op == "UPDATE" and not re.search(r"\bWHERE\b", clean, re.IGNORECASE):
            return False, "UPDATE requires a WHERE clause"
        return True, "Query is allowed"


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
    }
