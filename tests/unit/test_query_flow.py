"""Unit tests for plan_or_execute_query."""

from unittest.mock import MagicMock, patch

import pytest

from sqlserver_semantic_mcp.config import get_config, reset_config
from sqlserver_semantic_mcp.services.policy_service import PolicyService
from sqlserver_semantic_mcp.services.query_service import QueryService
from sqlserver_semantic_mcp.workflows.query_flow import plan_or_execute_query


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_DATABASE", "x")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_PASSWORD", "p")
    reset_config()
    cfg = get_config()
    policy = PolicyService(cfg)
    policy.load()
    query = QueryService(policy, cfg)
    return {"cfg": cfg, "policy": policy, "query": query}


def test_validate_mode(ctx):
    env = plan_or_execute_query(
        "SELECT * FROM dbo.T",
        policy=ctx["policy"],
        query_service=ctx["query"],
        mode="validate",
        cfg=ctx["cfg"],
    )
    assert env["kind"] == "plan_or_execute_query"
    assert env["data"]["path"] == "direct_validate"
    assert env["data"]["executed"] is False
    assert env["data"]["allowed"] is True


def test_validate_mode_includes_risk(ctx):
    env = plan_or_execute_query(
        "SELECT * FROM dbo.BigTable",
        policy=ctx["policy"],
        query_service=ctx["query"],
        mode="validate",
        cfg=ctx["cfg"],
    )
    d = env["data"]
    assert d["path"] == "direct_validate"
    assert d["risk_level"] == "medium"
    assert any(r["kind"] == "payload_risk" for r in d["risks"])


@patch("sqlserver_semantic_mcp.services.query_service.open_connection")
def test_validate_only_alias_removed(mock_open, ctx):
    # Old mode name no longer short-circuits — falls through to auto routing.
    conn = MagicMock()
    cursor = MagicMock()
    cursor.description = [("id",)]
    cursor.fetchmany.return_value = [(1,)]
    conn.cursor.return_value = cursor
    mock_open.return_value.__enter__.return_value = conn

    env = plan_or_execute_query(
        "SELECT 1",
        policy=ctx["policy"],
        query_service=ctx["query"],
        mode="validate_only",
        cfg=ctx["cfg"],
    )
    assert env["data"].get("path") != "direct_validate"


def test_disallowed_query_returns_validate_envelope(ctx):
    env = plan_or_execute_query(
        "DROP TABLE dbo.T",
        policy=ctx["policy"],
        query_service=ctx["query"],
        cfg=ctx["cfg"],
    )
    assert env["data"]["path"] == "direct_validate"
    assert env["next_action"] == "revise_query"
    assert env["data"]["executed"] is False


def test_dry_run_mode(ctx):
    env = plan_or_execute_query(
        "SELECT * FROM dbo.T",
        policy=ctx["policy"],
        query_service=ctx["query"],
        mode="dry_run",
        cfg=ctx["cfg"],
    )
    assert env["data"]["path"] == "dry_run"
    assert env["data"]["executed"] is False
    assert env["data"]["operation"] == "SELECT"


def test_natural_language_routes_to_discovery(ctx):
    env = plan_or_execute_query(
        "please show me customers who bought stuff",
        policy=ctx["policy"],
        query_service=ctx["query"],
        cfg=ctx["cfg"],
    )
    assert env["data"]["path"] == "discovery"
    assert env["recommended_tool"] in (
        "discover_relevant_tables",
        "describe_table",
        "find_join_path",
    )


@patch("sqlserver_semantic_mcp.services.query_service.open_connection")
def test_direct_execute_happy_path(mock_open, ctx):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.description = [("id",), ("name",)]
    cursor.fetchmany.return_value = [(1, "A"), (2, "B")]
    conn.cursor.return_value = cursor
    mock_open.return_value.__enter__.return_value = conn

    env = plan_or_execute_query(
        "SELECT * FROM dbo.T",
        policy=ctx["policy"],
        query_service=ctx["query"],
        cfg=ctx["cfg"],
    )
    assert env["data"]["path"] == "direct_execute"
    assert env["data"]["executed"] is True
    assert env["data"]["columns"] == ["id", "name"]
    assert env["data"]["row_count"] == 2
    assert env["data"]["rows"] == [[1, "A"], [2, "B"]]
    assert env["next_action"] == "done"
    assert "validation" not in env["data"]


@patch("sqlserver_semantic_mcp.services.query_service.open_connection")
def test_small_result_is_complete_and_exact(mock_open, ctx):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.description = [("id",)]
    cursor.fetchmany.return_value = [(1,), (2,)]
    conn.cursor.return_value = cursor
    mock_open.return_value.__enter__.return_value = conn

    env = plan_or_execute_query(
        "SELECT * FROM dbo.T",
        policy=ctx["policy"],
        query_service=ctx["query"],
        cfg=ctx["cfg"],
    )
    data = env["data"]
    assert data["executed"] is True
    assert data["rows"] == [[1], [2]]
    assert data["row_count"] == 2
    assert data["total_row_count"] == 2
    assert data["result_complete"] is True
    assert data["columns"] == ["id"]


@patch("sqlserver_semantic_mcp.services.query_service.open_connection")
def test_row_limit_truncates_and_reports_reason(mock_open, ctx):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.description = [("id",)]
    cursor.fetchmany.return_value = [(n,) for n in range(4)]
    conn.cursor.return_value = cursor
    mock_open.return_value.__enter__.return_value = conn

    env = plan_or_execute_query(
        "SELECT * FROM dbo.T",
        policy=ctx["policy"],
        query_service=ctx["query"],
        max_rows=3,
        cfg=ctx["cfg"],
    )
    data = env["data"]
    assert data["rows"] == [[0], [1], [2]]
    assert data["truncated"] is True
    assert data["truncation_reason"] == "row_limit"
    assert data["total_row_count"] is None
    assert "sample_rows" not in data
