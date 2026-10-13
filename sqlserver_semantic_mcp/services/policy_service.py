from typing import Optional

from ..config import Config, get_config
from ..policy.analyzer import SqlIntent
from ..policy.enforcer import enforce
from ..policy.intents import get_analyzer
from ..policy.loader import load_active_policy
from ..policy.models import PolicyProfile
from ..policy.readonly_guard import find_write_keywords, is_select_only


class PolicyService:
    def __init__(self, cfg: Optional[Config] = None) -> None:
        self._cfg = cfg or get_config()
        self._policy: Optional[PolicyProfile] = None
        self._analyzer = get_analyzer(self._cfg)

    def load(self) -> None:
        self._policy = load_active_policy(self._cfg)

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
