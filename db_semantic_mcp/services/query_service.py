"""Query service — validation / preview / execution.

Query handling is split into three explicit phases (``validate_query``,
``preview_query``, ``execute_query``) so the workflow layer can route an
agent's request down the shortest safe path.
"""

from __future__ import annotations

import json
import logging
import os
from enum import Enum
from typing import Any

from ..config import Config, get_config
from ..infrastructure.backends import backend_for
from ..infrastructure.connection import open_connection
from ..policy.analyzer import SqlIntent
from .policy_service import PolicyService, intent_to_dict

logger = logging.getLogger(__name__)


class AffectedRowsPolicyMode(str, Enum):
    STRICT = "strict"
    REPORT = "report"


# ---- output safety ----------------------------------------------------------

_FETCH_CHUNK = 100


def _json_default(value: Any) -> str:
    return str(value)


def _cap_cell(value: Any, max_chars: int, *, length_known: bool = True) -> Any:
    """Bound one value; large text and binary are the unbounded-token vector."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        shown = raw[: max_chars // 2].hex()
        suffix = (
            "" if len(raw) <= max_chars // 2 else (f"...[{len(raw)} bytes total]" if length_known else "...[truncated]")
        )
        return f"0x{shown}{suffix}"
    if isinstance(value, str) and len(value) > max_chars:
        return (
            f"{value[:max_chars]}...[truncated, {len(value)} chars total]"
            if length_known
            else f"{value[:max_chars]}...[truncated]"
        )
    return value


def _row_bytes(row: list) -> int:
    # +1 for the separating comma; matches the compact JSON used on the wire.
    return len(json.dumps(row, ensure_ascii=False, default=_json_default, separators=(",", ":")).encode("utf-8")) + 1


# ---- service ----------------------------------------------------------------


class QueryService:
    def __init__(
        self,
        policy_service: PolicyService,
        cfg: Config | None = None,
    ) -> None:
        self._policy = policy_service
        self._cfg = cfg or get_config()
        self._backend = backend_for(self._cfg)

    # ------------------------------------------------------------------ 1. validate

    def validate_query(self, sql: str, database: str = "") -> dict:
        """Return validation + intent, agent-envelope friendly."""
        db = database or self._cfg.database
        validation = self._policy.validate(sql, database=db)
        intent = validation["intent"]
        next_action = "execute" if validation["allowed"] else "revise_query"
        return {
            "kind": "query_validation",
            "allowed": validation["allowed"],
            "reason": validation["reason"],
            "intent": intent,
            **({"denial": validation["denial"]} if "denial" in validation else {}),
            "risk": intent["risk_level"],
            "tables": intent["affected_tables"],
            "next_action": next_action,
        }

    # ------------------------------------------------------------------ 2. preview

    def preview_query(
        self,
        sql: str,
        *,
        max_rows: int | None = None,
        database: str = "",
    ) -> dict:
        """Cheap dry-run: return what WOULD happen, without side effects."""
        db = database or self._cfg.database
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
            **({"denial": validation["denial"]} if "denial" in validation else {}),
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
        max_rows: int | None = None,
        affected_rows_policy: str | None = None,
        validate_result_schema: bool = False,
        database: str = "",
    ) -> dict:
        """Execute SQL after policy validation.

        SELECT results stop at the first of: row limit, ``max_response_bytes``
        of serialized rows, or ``max_cell_chars`` per value. Any early stop is
        reported through ``truncated`` and ``truncation_reason``.

        Write outcomes use transaction_state: committed, rolled_back,
        not_started, or unknown. Unknown commit/rollback acknowledgements
        require verification before any retry; executed=False alone does not
        mean the server rolled the transaction back.
        """
        # Fixed connection profiles always roll back over-cap writes. Other
        # profiles only do so when asked; otherwise the overshoot is reported.
        strict_cap = bool(
            self._cfg.database_type == "oracle"
            or self._cfg.connection_name
            or self._cfg.strict_rows_affected_cap
            or affected_rows_policy == "strict"
        )

        db = database or self._cfg.database
        policy = self._policy.current_policy()
        limit = min(max_rows or policy.constraints.max_rows_returned, policy.constraints.max_rows_returned)

        validation = (
            self._policy.validate(sql, database=db, resolve_sources=False)
            if self._cfg.database_type == "oracle"
            else self._policy.validate(sql, database=db)
        )
        denial = self._execution_denial(validation)
        if denial is not None:
            return denial

        op = validation["intent"]["primary_operation"]
        return self._execute_guarded(sql, op, limit, strict_cap, validate_result_schema, validation)

    @staticmethod
    def _execution_denial(validation: dict) -> dict | None:
        """Reject denied or unsupported batches before touching the driver."""
        if not validation["allowed"]:
            return {
                "executed": False,
                "validation": validation,
                "error": validation["reason"],
                **({"denial": validation["denial"]} if "denial" in validation else {}),
                "next_action": "revise_query",
            }
        if validation["intent"].get("is_multi_statement") or validation["intent"].get("statement_count", 1) > 1:
            return {
                "executed": False,
                "validation": validation,
                "error": "Batch execution is unsupported: whole-batch result and transaction caps cannot be guaranteed",
                "failure_category": "unsupported_batch",
                "next_action": "revise_query",
            }
        return None

    def _execute_guarded(
        self,
        sql: str,
        op: str,
        limit: int,
        strict_cap: bool,
        validate_result_schema: bool,
        validation: dict,
    ) -> dict:
        result = None
        statement_started = False
        try:
            with open_connection(self._cfg) as conn:
                if self._cfg.database_type == "oracle":
                    conn.autocommit = False
                    validation = self._policy.validate(sql, conn=conn)
                    denial = self._execution_denial(validation)
                    if denial is not None:
                        return denial
                cursor = conn.cursor()
                try:
                    result_schema = None
                    if validate_result_schema:
                        try:
                            result_schema = self._describe_result_schema(cursor, sql)
                        except Exception as exc:  # noqa: BLE001 (preflight rollback boundary)
                            result = {
                                "executed": False,
                                "validation": validation,
                                "error": f"Result schema validation failed: {self._safe_message(exc)}",
                                "failure_category": "result_schema_validation",
                                "transaction_state": "not_started",
                                "next_action": "revise_query",
                            }
                            try:
                                conn.rollback()
                            except Exception as cleanup_exc:  # noqa: BLE001 (cleanup must not raise)
                                self._note_cleanup_failure(result, "preflight_rollback", cleanup_exc)
                            return result
                    statement_started = True
                    cursor.execute(sql)

                    if op == "SELECT":
                        result = self._shape_select(cursor, limit, validation)
                        if result_schema is not None:
                            self._attach_result_schema(result, result_schema)
                        return result

                    result = self._shape_non_select(
                        cursor,
                        conn,
                        self._policy.current_policy().constraints.max_rows_affected,
                        strict_cap,
                        validation,
                    )
                    return result
                finally:
                    try:
                        cursor.close()
                    except Exception as cleanup_exc:  # noqa: BLE001 (cleanup must not raise)
                        if result is not None:
                            self._note_cleanup_failure(result, "cursor", cleanup_exc)
                        logger.warning("Failed to close query cursor")
        except Exception as e:  # noqa: BLE001 (security boundary – never echo driver errors)
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
                failed.update(
                    transaction_state="unknown",
                    failure_category="write_outcome_unknown",
                    automatic_retry_allowed=False,
                    next_action="verify_transaction",
                )
                failed["error"] += "; write outcome is unknown. Verify the transaction before retrying"
            return failed

    # ------------------------------------------------------------------ helpers

    def _safe_message(self, exc: Exception) -> str:
        from ..infrastructure.backends.oracle import OracleResultTypeError

        if isinstance(exc, OracleResultTypeError):
            return str(exc)  # fixed diagnostic, no driver detail or caller SQL
        message = str(exc)
        if self._cfg.password:
            message = message.replace(self._cfg.password, "***")
        if self._cfg.oracle and self._cfg.oracle.wallet_password_env:
            secret = os.environ.get(self._cfg.oracle.wallet_password_env)
            if secret:
                message = message.replace(secret, "***")
        if self._cfg.database_type == "oracle":
            # Oracle errors can include connect descriptors, wallet paths and SQL.
            return "Oracle database operation failed"
        return message

    def _note_cleanup_failure(self, result: dict, component: str, exc: Exception) -> None:
        result["cleanup_failed"] = True
        result.setdefault("cleanup_errors", []).append(
            {
                "component": component,
                "message": self._safe_message(exc),
            }
        )
        if result.get("transaction_state") == "committed":
            result["automatic_retry_allowed"] = False
            result["warning"] = "Transaction committed; cleanup failed. Do not retry this write"

    def _describe_result_schema(self, cursor: Any, sql: str) -> list[dict]:
        return self._backend.describe_result_schema(cursor, sql)

    def _attach_result_schema(self, result, result_schema):
        payload = [result.get("columns", []), result.get("rows", []), result_schema]
        if (
            len(json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8"))
            > self._cfg.max_response_bytes
        ):
            result.update(
                result_schema_omitted=True, truncated=True, result_complete=False, next_action="refine_or_done"
            )
            result.setdefault("truncation_reason", "result_schema_metadata_limit")
        else:
            result["result_schema"] = result_schema

    def _shape_select(self, cursor: Any, limit: int, validation: dict) -> dict:
        columns = [d[0] for d in cursor.description]
        max_bytes = self._cfg.max_response_bytes
        max_chars = self._cfg.max_cell_chars
        # Budget starts with the column header; rows share what remains.
        used = len(json.dumps(columns, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 2  # rows []
        metadata_omitted = used > max_bytes
        column_count = len(columns)
        if metadata_omitted:
            columns = []
        rows: list[list] = []
        fetched = 0
        reason: str | None = "column_metadata_limit" if metadata_omitted else None
        while reason is None:
            want = min(_FETCH_CHUNK, limit + 1 - fetched)
            if want <= 0:
                break
            chunk = cursor.fetchmany(want)
            fetched += len(chunk)
            for raw in chunk:
                if len(rows) >= limit:
                    reason = "row_limit"
                    break
                row = [
                    _cap_cell(
                        self._backend.convert_value(v, self._cfg),
                        max_chars,
                        length_known=not (self._cfg.database_type == "oracle" and callable(getattr(v, "read", None))),
                    )
                    for v in raw
                ]
                size = _row_bytes(row)
                if used + size > max_bytes:
                    reason = "byte_limit"
                    break
                used += size
                rows.append(row)
            if len(chunk) < want:
                break
        truncated = reason is not None
        result = {
            "executed": True,
            "validation": validation,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "total_row_count": None if truncated else len(rows),
            "fetched_row_count": fetched,
            "max_rows_applied": limit,
            "max_response_bytes_applied": max_bytes,
            "truncated": truncated,
            "result_complete": not truncated,
            "next_action": "refine_or_done" if truncated else "done",
        }
        if metadata_omitted:
            result.update(columns_omitted=True, column_count=column_count)
            result["truncation_hint"] = (
                "Column metadata exceeds the response cap. Select fewer columns or shorter aliases."
            )
        if truncated:
            result["truncation_reason"] = reason
            if reason == "byte_limit":
                result["truncation_hint"] = (
                    "Output byte limit reached. Select fewer columns, add a WHERE or row limit, or aggregate to see the rest."
                )
        return result

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
                conn,
                validation,
                "Unable to determine affected rows",
                failure_category="affected_rows_unknown",
            )
        exceeded = affected > cap

        if strict_cap and exceeded:
            return self._rollback_rejected_write(
                conn,
                validation,
                f"Affected rows {affected} exceeds cap {cap} under strict rows-affected policy",
                failure_category="affected_rows_cap",
                rows_affected=affected,
                exceeded_cap=True,
            )

        try:
            conn.commit()
        except Exception as exc:  # noqa: BLE001 (commit outcome boundary)
            # A network failure can lose the acknowledgement after COMMIT
            # already succeeded on the server. Never claim a rollback here.
            return {
                "executed": False,
                "validation": validation,
                "rows_affected": affected,
                "exceeded_cap": exceeded,
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
        self,
        conn: Any,
        validation: dict,
        reason: str,
        *,
        failure_category: str,
        **details,
    ) -> dict:
        result = {"executed": False, "validation": validation, **details}
        try:
            conn.rollback()
        except Exception as exc:  # noqa: BLE001 (rollback outcome boundary)
            return {
                **result,
                "transaction_state": "unknown",
                "failure_category": "rollback_outcome_unknown",
                "automatic_retry_allowed": False,
                "error": f"{reason}; rollback outcome is unknown: {self._safe_message(exc)}. Verify the transaction before retrying",
                "next_action": "verify_transaction",
            }
        return {
            **result,
            "transaction_state": "rolled_back",
            "failure_category": failure_category,
            "error": f"{reason}; transaction rolled back",
            "next_action": "revise_query",
        }


__all__ = [
    "QueryService",
    "AffectedRowsPolicyMode",
    "intent_to_dict",
    "SqlIntent",
]
