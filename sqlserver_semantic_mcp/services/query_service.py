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
        limit = min(max_rows or policy.constraints.max_rows_returned, policy.constraints.max_rows_returned)

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
        validate_result_schema: bool = False,
        database: str = "",
    ) -> dict:
        """Execute SQL after policy validation.

        response_mode:
          summary    — columns + at most 3 rows
          rows       — columns + rows (default when op=SELECT)
          sample     — columns + first N rows (N = budget-derived)
          count_only — row_count only

        Write outcomes use transaction_state: committed, rolled_back,
        not_started, or unknown. Unknown commit/rollback acknowledgements
        require verification before any retry; executed=False alone does not
        mean the server rolled the transaction back.
        """
        mode = _normalize_response_mode(
            response_mode, self._cfg.default_response_mode,
        )
        budget = token_budget_hint or self._cfg.default_token_budget_hint

        strict_cap = self._cfg.strict_rows_affected_cap or affected_rows_policy == "strict"

        db = database or self._cfg.mssql_database
        policy = self._policy.current_policy()
        limit = min(max_rows or policy.constraints.max_rows_returned, policy.constraints.max_rows_returned)

        validation = self._policy.validate(sql, database=db)
        if not validation["allowed"]:
            return {
                "executed": False,
                "validation": validation,
                "error": validation["reason"],
                "next_action": "revise_query",
            }

        op = validation["intent"]["primary_operation"]
        if validation["intent"].get("is_multi_statement") or validation["intent"].get("statement_count", 1) > 1:
            return {
                "executed": False, "validation": validation,
                "error": "Batch execution is unsupported: whole-batch result and transaction caps cannot be guaranteed",
                "failure_category": "unsupported_batch",
                "next_action": "revise_query",
            }

        result = None
        statement_started = False
        try:
            with open_connection(self._cfg) as conn:
                cursor = conn.cursor()
                try:
                    result_schema = None
                    if validate_result_schema:
                        try:
                            result_schema = self._describe_result_schema(cursor, sql)
                        except Exception as exc:
                            result = {
                                "executed": False, "validation": validation,
                                "error": f"Result schema validation failed: {self._safe_message(exc)}",
                                "failure_category": "result_schema_validation",
                                "transaction_state": "not_started",
                                "next_action": "revise_query",
                            }
                            try:
                                conn.rollback()
                            except Exception as cleanup_exc:
                                self._note_cleanup_failure(result, "preflight_rollback", cleanup_exc)
                            return result
                    statement_started = True
                    cursor.execute(sql)

                    if op == "SELECT":
                        result = self._shape_select(
                            cursor, limit, mode, budget, validation,
                        )
                        if result_schema is not None:
                            result["result_schema"] = result_schema
                        return result

                    result = self._shape_non_select(
                        cursor, conn, policy.constraints.max_rows_affected,
                        strict_cap, validation,
                    )
                    return result
                finally:
                    try:
                        cursor.close()
                    except Exception as cleanup_exc:
                        if result is not None:
                            self._note_cleanup_failure(result, "cursor", cleanup_exc)
                        logger.warning("Failed to close query cursor")
        except Exception as e:
            if result is not None:
                # A known COMMIT/ROLLBACK acknowledgement survives failures
                # from the connection context manager's close operation.
                self._note_cleanup_failure(result, "connection", e)
                logger.warning("Failed to close query connection after a known result")
                return result
            message = self._safe_message(e)
            if self._cfg.connection_name:
                logger.error("Query failed for connection %s: %s", self._cfg.connection_name, message)
            else:
                logger.error("Query execution failed: %s", message)
            failed = {
                "executed": False,
                "validation": validation,
                "error": message,
                "failure_category": "database",
                "transaction_state": "not_started" if not statement_started else "read_only",
                "next_action": "revise_query",
            }
            if statement_started and op != "SELECT":
                failed.update(transaction_state="unknown", failure_category="write_outcome_unknown",
                              automatic_retry_allowed=False, next_action="verify_transaction")
                failed["error"] += "; write outcome is unknown. Verify the transaction before retrying"
            return failed

    # ------------------------------------------------------------------ helpers

    def _safe_message(self, exc: Exception) -> str:
        message = str(exc)
        if self._cfg.mssql_password:
            message = message.replace(self._cfg.mssql_password, "***")
        return message

    def _note_cleanup_failure(self, result: dict, component: str, exc: Exception) -> None:
        result["cleanup_failed"] = True
        result.setdefault("cleanup_errors", []).append({
            "component": component, "message": self._safe_message(exc),
        })
        if result.get("transaction_state") == "committed":
            result["automatic_retry_allowed"] = False
            result["warning"] = "Transaction committed; cleanup failed. Do not retry this write"

    @staticmethod
    def _describe_result_schema(cursor: Any, sql: str) -> list[dict]:
        cursor.execute(
            "SELECT column_ordinal, name, system_type_name, is_nullable, "
            "error_number, error_message FROM "
            "sys.dm_exec_describe_first_result_set(%s, NULL, 0)",
            (sql,),
        )
        columns = []
        for ordinal, name, type_name, nullable, error_number, error_message in cursor.fetchall():
            if error_number is not None or error_message:
                raise ValueError(error_message or f"SQL Server metadata error {error_number}")
            columns.append({"ordinal": ordinal, "name": name,
                            "type": type_name, "is_nullable": bool(nullable)})
        return columns

    def _shape_select(
        self,
        cursor: Any,
        limit: int,
        mode: str,
        budget: Optional[str],
        validation: dict,
    ) -> dict:
        columns = [d[0] for d in cursor.description]
        fetch_limit = limit
        if mode == "summary":
            fetch_limit = min(limit, 3)
        elif mode == "sample":
            fetch_limit = min(limit, sample_row_cap(budget))
        fetched = cursor.fetchmany(fetch_limit + 1)
        truncated = len(fetched) > fetch_limit
        rows = fetched[:fetch_limit]
        common = {
            "executed": True,
            "validation": validation,
            "response_mode": mode,
            "token_budget_hint": budget,
            "row_count": len(rows),
            "row_count_scope": "fetched",
            "returned_row_count": 0 if mode == "count_only" else len(rows),
            "total_row_count": None if truncated else len(rows),
            "fetched_row_count": len(fetched),
            "fetch_limit_applied": fetch_limit,
            "max_rows_applied": limit,
            "truncated": truncated,
            "result_complete": not truncated,
            "rows_omitted": truncated or (mode == "count_only" and bool(rows)),
        }

        if mode == "count_only":
            return {
                **common,
                "next_action": "done",
            }

        if mode == "summary":
            return {
                **common,
                "columns": columns,
                "rows": [list(r) for r in rows],
                "next_action": "refine_or_done",
            }

        if mode == "sample":
            return {
                **common,
                "columns": columns,
                "sample_rows": [list(r) for r in rows],
                "sample_size": len(rows),
                "next_action": "refine_or_done",
            }

        # default: rows
        return {
            **common,
            "columns": columns,
            "rows": [list(r) for r in rows],
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
        if strict_cap and affected < 0:
            return self._rollback_rejected_write(
                conn, validation, "Unable to determine affected rows",
                failure_category="affected_rows_unknown",
            )
        exceeded = affected > cap

        if strict_cap and exceeded:
            return self._rollback_rejected_write(
                conn, validation,
                f"Affected rows {affected} exceeds cap {cap} under strict rows-affected policy",
                failure_category="affected_rows_cap", rows_affected=affected, exceeded_cap=True,
            )

        try:
            conn.commit()
        except Exception as exc:
            # A network failure can lose the acknowledgement after COMMIT
            # already succeeded on the server. Never claim a rollback here.
            return {
                "executed": False, "validation": validation,
                "rows_affected": affected, "exceeded_cap": exceeded,
                "transaction_state": "unknown",
                "failure_category": "commit_outcome_unknown",
                "automatic_retry_allowed": False,
                "error": f"Commit outcome is unknown: {self._safe_message(exc)}. Verify the transaction before retrying",
                "next_action": "verify_transaction",
            }
        return {
            "executed": True,
            "validation": validation,
            "rows_affected": affected,
            "exceeded_cap": exceeded,
            "transaction_state": "committed",
            "next_action": "done",
        }

    def _rollback_rejected_write(
        self, conn: Any, validation: dict, reason: str, *, failure_category: str, **details,
    ) -> dict:
        result = {"executed": False, "validation": validation, **details}
        try:
            conn.rollback()
        except Exception as exc:
            return {
                **result, "transaction_state": "unknown",
                "failure_category": "rollback_outcome_unknown",
                "automatic_retry_allowed": False,
                "error": f"{reason}; rollback outcome is unknown: {self._safe_message(exc)}. Verify the transaction before retrying",
                "next_action": "verify_transaction",
            }
        return {
            **result, "transaction_state": "rolled_back",
            "failure_category": failure_category,
            "error": f"{reason}; transaction rolled back",
            "next_action": "revise_query",
        }


__all__ = [
    "QueryService",
    "AffectedRowsPolicyMode",
    "sample_row_cap",
    "intent_to_dict",
    "SqlIntent",
]
