"""Query service — validation / preview / execution.

v0.5 splits query handling into three explicit phases (``validate_query``,
``preview_query``, ``execute_query``) so the workflow layer can route an
agent's request down the shortest safe path.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Optional

from ..config import DEFAULT_TOKEN_BUDGET_HINT, Config, get_config
from ..infrastructure.connection import open_connection
from ..policy.analyzer import SqlIntent
from .policy_service import PolicyService, intent_to_dict

logger = logging.getLogger(__name__)


class AffectedRowsPolicyMode(str, Enum):
    STRICT = "strict"
    REPORT = "report"


# ---- response_mode helpers --------------------------------------------------

_VALID_RESPONSE_MODES = {"summary", "rows", "sample", "count_only"}


def _normalize_response_mode(value: Optional[str], default: str) -> str:
    if value is None:
        return default
    if value not in _VALID_RESPONSE_MODES:
        raise ValueError(
            f"invalid response_mode '{value}'; "
            f"expected one of {sorted(_VALID_RESPONSE_MODES)}"
        )
    return value


# ---- budget hint ------------------------------------------------------------

_BUDGET_SAMPLE_ROWS = {
    "tiny":   3,
    "low":    10,
    "medium": 50,
    "high":   200,
}


def sample_row_cap(budget: Optional[str]) -> int:
    return _BUDGET_SAMPLE_ROWS.get(
        budget or DEFAULT_TOKEN_BUDGET_HINT,
        _BUDGET_SAMPLE_ROWS[DEFAULT_TOKEN_BUDGET_HINT],
    )


# ---- service ----------------------------------------------------------------


class QueryService:
    def __init__(
        self,
        policy_service: PolicyService,
        cfg: Optional[Config] = None,
    ) -> None:
        self._policy = policy_service
        self._cfg = cfg or get_config()

    # ------------------------------------------------------------------ 1. validate

    def validate_query(self, sql: str, database: str = "") -> dict:
        """Return validation + intent, agent-envelope friendly."""
        db = database or self._cfg.mssql_database
        validation = self._policy.validate(sql, database=db)
        intent = validation["intent"]
        next_action = "execute" if validation["allowed"] else "revise_query"
        return {
            "kind": "query_validation",
            "allowed": validation["allowed"],
            "reason": validation["reason"],
            "intent": intent,
            "risk": intent["risk_level"],
            "tables": intent["affected_tables"],
            "next_action": next_action,
        }

    # ------------------------------------------------------------------ 2. preview

    def preview_query(
        self,
        sql: str,
        *,
        max_rows: Optional[int] = None,
        database: str = "",
    ) -> dict:
        """Cheap dry-run: return what WOULD happen, without side effects."""
        db = database or self._cfg.mssql_database
        policy = self._policy.current_policy()
        validation = self._policy.validate(sql, database=db)
        intent = validation["intent"]
        limit = max_rows or policy.constraints.max_rows_returned

        return {
            "kind": "query_preview",
            "operation": intent["primary_operation"],
            "tables": intent["affected_tables"],
            "allowed": validation["allowed"],
            "reason": validation["reason"],
            "risk": intent["risk_level"],
            "max_rows_applied": limit,
            "max_rows_affected": policy.constraints.max_rows_affected,
            "is_multi_statement": intent["is_multi_statement"],
            "has_where_clause": intent["has_where_clause"],
            "has_unqualified_tables": intent["has_unqualified_tables"],
            "contains_dynamic_sql": intent["contains_dynamic_sql"],
            "next_action": "execute" if validation["allowed"] else "revise_query",
        }

    # ------------------------------------------------------------------ 3. execute

    def execute_query(
        self,
        sql: str,
        *,
        max_rows: Optional[int] = None,
        response_mode: Optional[str] = None,
        token_budget_hint: Optional[str] = None,
        affected_rows_policy: Optional[str] = None,
        database: str = "",
    ) -> dict:
        """Execute SQL after policy validation.

        response_mode:
          summary    — columns + row_count only
          rows       — columns + rows (default when op=SELECT)
          sample     — columns + first N rows (N = budget-derived)
          count_only — row_count only
        """
        mode = _normalize_response_mode(
            response_mode, self._cfg.default_response_mode,
        )
        budget = token_budget_hint or self._cfg.default_token_budget_hint

        strict_cap = self._cfg.strict_rows_affected_cap
        if affected_rows_policy is not None:
            strict_cap = affected_rows_policy == "strict"

        db = database or self._cfg.mssql_database
        policy = self._policy.current_policy()
        limit = max_rows or policy.constraints.max_rows_returned

        validation = self._policy.validate(sql, database=db)
        if not validation["allowed"]:
            return {
                "executed": False,
                "validation": validation,
                "error": validation["reason"],
                "next_action": "revise_query",
            }

        op = validation["intent"]["primary_operation"]

        try:
            with open_connection(self._cfg) as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute(sql)

                    if op == "SELECT":
                        return self._shape_select(
                            cursor, limit, mode, budget, validation,
                        )

                    return self._shape_non_select(
                        cursor, conn, policy.constraints.max_rows_affected,
                        strict_cap, validation,
                    )
                finally:
                    try:
                        cursor.close()
                    except Exception:
                        logger.warning("Failed to close cursor", exc_info=True)
        except Exception as e:
            logger.exception("Query execution failed")
            return {
                "executed": False,
                "validation": validation,
                "error": str(e),
                "next_action": "revise_query",
            }

    # ------------------------------------------------------------------ helpers

    def _shape_select(
        self,
        cursor: Any,
        limit: int,
        mode: str,
        budget: Optional[str],
        validation: dict,
    ) -> dict:
        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchmany(limit + 1)
        truncated = len(rows) > limit
        rows = rows[:limit]

        if mode == "count_only":
            return {
                "executed": True,
                "validation": validation,
                "row_count": len(rows),
                "truncated": truncated,
                "next_action": "done",
            }

        if mode == "summary":
            return {
                "executed": True,
                "validation": validation,
                "columns": columns,
                "row_count": len(rows),
                "truncated": truncated,
                "next_action": "refine_or_done",
            }

        if mode == "sample":
            cap = min(sample_row_cap(budget), len(rows))
            return {
                "executed": True,
                "validation": validation,
                "columns": columns,
                "row_count": len(rows),
                "truncated": truncated,
                "sample_rows": [list(r) for r in rows[:cap]],
                "sample_size": cap,
                "next_action": "refine_or_done",
            }

        # default: rows
        return {
            "executed": True,
            "validation": validation,
            "columns": columns,
            "rows": [list(r) for r in rows],
            "row_count": len(rows),
            "truncated": truncated,
            "next_action": "done",
        }

    def _shape_non_select(
        self,
        cursor: Any,
        conn: Any,
        cap: int,
        strict_cap: bool,
        validation: dict,
    ) -> dict:
        affected = cursor.rowcount
        exceeded = affected > cap

        if strict_cap and exceeded:
            try:
                conn.rollback()
            except Exception:
                logger.warning("Rollback failed", exc_info=True)
            return {
                "executed": False,
                "validation": validation,
                "rows_affected": affected,
                "exceeded_cap": True,
                "error": (
                    f"Affected rows {affected} exceeds cap {cap} under "
                    f"strict rows-affected policy; transaction rolled back"
                ),
                "next_action": "revise_query",
            }

        conn.commit()
        return {
            "executed": True,
            "validation": validation,
            "rows_affected": affected,
            "exceeded_cap": exceeded,
            "next_action": "done",
        }


__all__ = [
    "QueryService",
    "AffectedRowsPolicyMode",
    "sample_row_cap",
    "intent_to_dict",
    "SqlIntent",
]
