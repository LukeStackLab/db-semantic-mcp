"""Exact result-count semantics and opt-in metadata preflight."""
from unittest.mock import MagicMock, patch

import pytest

from sqlserver_semantic_mcp.config import Config
from sqlserver_semantic_mcp.policy.models import PolicyProfile
from sqlserver_semantic_mcp.services.query_service import QueryService


@pytest.fixture
def setup_query():
    cfg = Config(_env_file=None, mssql_server="fake", mssql_database="db", mssql_user="u", mssql_password="password")
    policy = MagicMock()
    policy.current_policy.return_value = PolicyProfile(profile_name="readonly")
    policy.validate.return_value = {"allowed": True, "intent": {"primary_operation": "SELECT"}}
    with patch("sqlserver_semantic_mcp.services.query_service.open_connection") as opened:
        conn = opened.return_value.__enter__.return_value
        cursor = conn.cursor.return_value
        cursor.description = [("value",)]
        yield QueryService(policy, cfg), policy, opened, conn, cursor


def test_summary_preserves_count_big_value(setup_query):
    query, _, _, _, cursor = setup_query
    cursor.fetchmany.return_value = [(9876543210,)]
    result = query.execute_query("SELECT COUNT_BIG(*) FROM dbo.T")
    assert result["rows"] == [[9876543210]]
    assert result["returned_row_count"] == result["total_row_count"] == 1
    assert result["row_count_scope"] == "fetched"
    assert result["result_complete"] is True
    cursor.fetchmany.assert_called_once_with(4)


def test_summary_preview_is_bounded_and_total_unknown(setup_query):
    query, _, _, _, cursor = setup_query
    cursor.fetchmany.return_value = [(n,) for n in range(4)]
    result = query.execute_query("SELECT * FROM dbo.T", max_rows=100)
    assert result["rows"] == [[0], [1], [2]]
    assert result["row_count"] == result["returned_row_count"] == 3
    assert result["total_row_count"] is None
    assert result["truncated"] is result["rows_omitted"] is True
    assert result["result_complete"] is False
    assert result["max_rows_applied"] == 100
    assert result["fetch_limit_applied"] == 3


@pytest.mark.parametrize("mode", ["rows", "count_only"])
def test_bounded_count_never_claims_total(setup_query, mode):
    query, _, _, _, cursor = setup_query
    cursor.fetchmany.return_value = [(1,), (2,), (3,)]
    result = query.execute_query("SELECT * FROM dbo.T", response_mode=mode, max_rows=2)
    assert result["row_count"] == 2
    assert result["returned_row_count"] == (2 if mode == "rows" else 0)
    assert result["total_row_count"] is None
    assert result["truncated"] is True


def test_count_only_small_result_has_exact_total_but_returns_no_rows(setup_query):
    query, _, _, _, cursor = setup_query
    cursor.fetchmany.return_value = [(1,), (2,)]
    result = query.execute_query("SELECT * FROM dbo.T", response_mode="count_only")
    assert result["row_count"] == result["total_row_count"] == 2
    assert result["returned_row_count"] == 0
    assert "rows" not in result


def test_legacy_server_strict_cannot_be_downgraded(setup_query):
    query, policy, _, conn, cursor = setup_query
    policy.validate.return_value["intent"]["primary_operation"] = "UPDATE"
    cursor.rowcount = 101
    result = query.execute_query("UPDATE dbo.T SET value=1 WHERE id=1", affected_rows_policy="report")
    assert result["executed"] is False
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


def test_metadata_preflight_is_parameterized_and_precedes_user_sql(setup_query):
    query, _, _, _, cursor = setup_query
    sql = "SELECT value FROM dbo.T WHERE value='quoted'"
    cursor.fetchall.return_value = [(1, "value", "int", False, None, None)]
    cursor.fetchmany.return_value = [(7,)]
    result = query.execute_query(sql, validate_result_schema=True)
    calls = cursor.execute.call_args_list
    assert "sys.dm_exec_describe_first_result_set(%s, NULL, 0)" in calls[0].args[0]
    assert calls[0].args[1] == (sql,)
    assert sql not in calls[0].args[0]
    assert calls[1].args == (sql,)
    assert result["result_schema"][0]["type"] == "int"


def test_preflight_failure_stops_before_user_execution(setup_query):
    query, _, _, conn, cursor = setup_query
    cursor.fetchall.return_value = [(None, None, None, None, 11526, "metadata unavailable")]
    result = query.execute_query("SELECT * FROM dbo.T", validate_result_schema=True)
    assert result["executed"] is False
    assert result["failure_category"] == "result_schema_validation"
    cursor.execute.assert_called_once()
    cursor.fetchmany.assert_not_called()
    conn.rollback.assert_called_once()
    cursor.close.assert_called_once()


def test_denied_sql_does_not_even_run_metadata_preflight(setup_query):
    query, policy, opened, _, _ = setup_query
    policy.validate.return_value.update(allowed=False, reason="denied")
    result = query.execute_query("EXEC dangerous", validate_result_schema=True)
    assert result["executed"] is False
    opened.assert_not_called()


def test_multi_statement_never_executes_even_if_legacy_policy_allows_it(setup_query):
    query, policy, opened, _, _ = setup_query
    policy.validate.return_value["intent"].update(is_multi_statement=True, statement_count=2)
    result = query.execute_query("SELECT 1; SELECT 2", validate_result_schema=True)
    assert result["executed"] is False
    assert result["failure_category"] == "unsupported_batch"
    assert "whole-batch" in result["error"]
    opened.assert_not_called()


@pytest.mark.asyncio
async def test_tool_runs_complete_query_lifecycle_in_one_worker(monkeypatch):
    from contextlib import contextmanager
    from threading import get_ident
    from types import SimpleNamespace
    from sqlserver_semantic_mcp.services.policy_service import PolicyService
    from sqlserver_semantic_mcp.workflows.facade import WorkflowFacade
    from sqlserver_semantic_mcp.server.tools import query as query_tool

    cfg = Config(_env_file=None, mssql_server="fake", mssql_database="db", mssql_user="u", mssql_password="p")
    policy = PolicyService(cfg)
    policy.load()
    service = QueryService(policy, cfg)
    events = []
    loop_thread = get_ident()

    class Cursor:
        description = [("value",)]

        def execute(self, *args):
            events.append(("execute", get_ident()))

        def fetchmany(self, limit):
            events.append(("fetch", get_ident()))
            return [(1,)]

        def close(self):
            events.append(("cursor_close", get_ident()))

    class Connection:
        def cursor(self):
            events.append(("cursor", get_ident()))
            return Cursor()

    @contextmanager
    def opened(_cfg):
        events.append(("connect", get_ident()))
        try:
            yield Connection()
        finally:
            events.append(("connection_close", get_ident()))

    monkeypatch.setattr("sqlserver_semantic_mcp.services.query_service.open_connection", opened)
    monkeypatch.setattr(query_tool, "get_context", lambda: SimpleNamespace(
        cfg=cfg, workflow=WorkflowFacade(cfg, policy, service)))
    result = await query_tool._plan_or_execute({"query": "SELECT 1"})
    assert result["data"]["rows"] == [[1]]
    assert [name for name, _ in events] == ["connect", "cursor", "execute", "fetch", "cursor_close", "connection_close"]
    assert len({thread for _, thread in events}) == 1
    assert events[0][1] != loop_thread


def test_committed_write_survives_connection_cleanup_failure(setup_query):
    query, policy, opened, conn, cursor = setup_query
    policy.validate.return_value["intent"]["primary_operation"] = "UPDATE"
    cursor.rowcount = 1
    opened.return_value.__exit__.side_effect = OSError("connection close failed password")
    result = query.execute_query("UPDATE dbo.T SET value=1 WHERE id=1")
    assert result["executed"] is True
    assert result["transaction_state"] == "committed"
    assert result["rows_affected"] == 1
    assert result["next_action"] == "done"
    assert result["cleanup_failed"] is True
    assert result["automatic_retry_allowed"] is False
    assert result["cleanup_errors"][0]["component"] == "connection"
    assert "password" not in str(result)
    assert "Do not retry" in result["warning"]
    assert "error" not in result
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_committed_write_survives_cursor_cleanup_failure(setup_query):
    query, policy, _, conn, cursor = setup_query
    policy.validate.return_value["intent"]["primary_operation"] = "UPDATE"
    cursor.rowcount = 1
    cursor.close.side_effect = OSError("cursor close failed")
    result = query.execute_query("UPDATE dbo.T SET value=1 WHERE id=1")
    assert result["executed"] is True
    assert result["transaction_state"] == "committed"
    assert result["cleanup_errors"][0]["component"] == "cursor"
    assert result["automatic_retry_allowed"] is False
    conn.commit.assert_called_once()


def test_lost_commit_ack_is_unknown_and_must_not_be_retried(setup_query):
    query, policy, _, conn, cursor = setup_query
    policy.validate.return_value["intent"]["primary_operation"] = "UPDATE"
    cursor.rowcount = 1
    conn.commit.side_effect = OSError("commit acknowledgement lost password")
    result = query.execute_query("UPDATE dbo.T SET value=1 WHERE id=1")
    assert result["executed"] is False
    assert result["transaction_state"] == "unknown"
    assert result["failure_category"] == "commit_outcome_unknown"
    assert result["automatic_retry_allowed"] is False
    assert result["next_action"] == "verify_transaction"
    assert "rolled back" not in result["error"]
    assert "password" not in result["error"]
    conn.rollback.assert_not_called()
    cursor.close.assert_called_once()


@pytest.mark.parametrize("affected", [-1, 101])
def test_failed_cap_rollback_never_claims_transaction_rolled_back(setup_query, affected):
    query, policy, _, conn, cursor = setup_query
    policy.validate.return_value["intent"]["primary_operation"] = "UPDATE"
    cursor.rowcount = affected
    conn.rollback.side_effect = OSError("rollback acknowledgement lost password")
    result = query.execute_query("UPDATE dbo.T SET value=1 WHERE id=1")
    assert result["executed"] is False
    assert result["transaction_state"] == "unknown"
    assert result["failure_category"] == "rollback_outcome_unknown"
    assert result["automatic_retry_allowed"] is False
    assert result["next_action"] == "verify_transaction"
    assert "rolled back" not in result["error"]
    assert "password" not in result["error"]
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()


def test_confirmed_cap_rollback_survives_connection_close_failure(setup_query):
    query, policy, opened, conn, cursor = setup_query
    policy.validate.return_value["intent"]["primary_operation"] = "UPDATE"
    cursor.rowcount = 101
    opened.return_value.__exit__.side_effect = OSError("close failed")
    result = query.execute_query("UPDATE dbo.T SET value=1 WHERE id=1")
    assert result["transaction_state"] == "rolled_back"
    assert result["failure_category"] == "affected_rows_cap"
    assert "transaction rolled back" in result["error"]
    assert result["cleanup_failed"] is True
    conn.rollback.assert_called_once()


def test_unknown_commit_survives_secondary_close_failure(setup_query):
    query, policy, opened, conn, cursor = setup_query
    policy.validate.return_value["intent"]["primary_operation"] = "UPDATE"
    cursor.rowcount = 1
    conn.commit.side_effect = OSError("commit ack lost")
    opened.return_value.__exit__.side_effect = OSError("close also failed")
    result = query.execute_query("UPDATE dbo.T SET value=1 WHERE id=1")
    assert result["transaction_state"] == "unknown"
    assert result["failure_category"] == "commit_outcome_unknown"
    assert "commit ack lost" in result["error"]
    assert result["cleanup_errors"][0]["message"] == "close also failed"
    assert result["next_action"] == "verify_transaction"


def test_write_execution_error_is_not_assumed_rolled_back(setup_query):
    query, policy, _, conn, cursor = setup_query
    policy.validate.return_value["intent"]["primary_operation"] = "UPDATE"
    cursor.execute.side_effect = OSError("write ack lost")
    result = query.execute_query("UPDATE dbo.T SET value=1 WHERE id=1")
    assert result["transaction_state"] == "unknown"
    assert result["automatic_retry_allowed"] is False
    assert result["next_action"] == "verify_transaction"
    conn.commit.assert_not_called()
