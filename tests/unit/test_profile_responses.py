import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.types import Tool
from pydantic import AnyUrl

from sqlserver_semantic_mcp.config import Config
from sqlserver_semantic_mcp.services.query_service import QueryService
from sqlserver_semantic_mcp.policy.models import PolicyProfile


@pytest.fixture
def cfg():
    return Config(
        _env_file=None, mssql_server="s", mssql_database="ERP",
        mssql_password="NEVER_ECHO", connection_name="erp",
        metrics_enabled=False, cache_enabled=False,
    )


@pytest.mark.asyncio
async def test_tool_result_identifies_source_and_keeps_false_status(cfg, monkeypatch):
    from sqlserver_semantic_mcp.server import app as mod
    monkeypatch.setattr(mod, "get_config", lambda: cfg)
    tool = Tool(name="test_source", inputSchema={"type": "object"})
    async def handler(args):
        return {"executed": False, "allowed": False}
    monkeypatch.setitem(mod._TOOL_REGISTRY, tool.name, (tool, handler))
    result = json.loads((await mod._call_tool(tool.name, {}))[0].text)
    assert result["source"] == {"connection": "erp", "database": "ERP"}
    assert result["executed"] is False
    assert result["allowed"] is False


@pytest.mark.asyncio
async def test_tool_error_redacts_password_and_identifies_source(cfg, monkeypatch, caplog):
    from sqlserver_semantic_mcp.server import app as mod
    monkeypatch.setattr(mod, "get_config", lambda: cfg)
    tool = Tool(name="test_error_source", inputSchema={"type": "object"})
    async def handler(args):
        raise RuntimeError("driver failure NEVER_ECHO")
    monkeypatch.setitem(mod._TOOL_REGISTRY, tool.name, (tool, handler))
    result = json.loads((await mod._call_tool(tool.name, {}))[0].text)
    assert result["source"]["connection"] == "erp"
    assert "NEVER_ECHO" not in json.dumps(result) + caplog.text


@pytest.mark.asyncio
async def test_resource_identifies_source(cfg, monkeypatch):
    from sqlserver_semantic_mcp.server.resources import schema
    monkeypatch.setattr(schema, "get_context", lambda: SimpleNamespace(cfg=cfg))
    monkeypatch.setattr(schema.metadata_service, "database_summary", AsyncMock(return_value={"table_count": 2}))
    result = json.loads(await schema.read_resource(AnyUrl("semantic://summary/database")))
    assert result["source"]["connection"] == "erp"
    assert result["data"]["table_count"] == 2


def test_query_row_request_cannot_exceed_policy_cap(cfg, monkeypatch):
    policy = MagicMock()
    policy.current_policy.return_value = PolicyProfile(
        profile_name="readonly", constraints={"max_rows_returned": 2},
    )
    policy.validate.return_value = {"allowed": True, "intent": {"primary_operation": "SELECT"}}
    opened = MagicMock()
    cursor = opened.return_value.__enter__.return_value.cursor.return_value
    cursor.description = [("id",)]
    cursor.fetchmany.return_value = [(1,), (2,), (3,)]
    monkeypatch.setattr("sqlserver_semantic_mcp.services.query_service.open_connection", opened)
    result = QueryService(policy, cfg).execute_query("SELECT id FROM dbo.T", max_rows=999, response_mode="rows")
    cursor.fetchmany.assert_called_once_with(3)
    assert result["rows"] == [[1], [2]]
    assert result["truncated"] is True


def test_profile_write_cannot_disable_strict_cap(cfg, monkeypatch):
    policy = MagicMock()
    policy.current_policy.return_value = PolicyProfile(
        profile_name="read_write_safe", constraints={"max_rows_affected": 2},
    )
    policy.validate.return_value = {"allowed": True, "intent": {"primary_operation": "UPDATE"}}
    opened = MagicMock()
    conn = opened.return_value.__enter__.return_value
    conn.cursor.return_value.rowcount = 3
    monkeypatch.setattr("sqlserver_semantic_mcp.services.query_service.open_connection", opened)
    result = QueryService(policy, cfg).execute_query("UPDATE dbo.T SET x=1 WHERE id=1", affected_rows_policy="report")
    assert result["executed"] is False
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()
