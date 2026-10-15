"""Unit tests for the consolidated tool_metrics(action) tool (Task 10)."""

import pytest


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_DATABASE", "testdb")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_PASSWORD", "p")
    from sqlserver_semantic_mcp.config import reset_config

    reset_config()
    from sqlserver_semantic_mcp.server.app import reset_context

    reset_context()


@pytest.mark.asyncio
async def test_tool_metrics_action(env, monkeypatch):
    from sqlserver_semantic_mcp.server.tools import metrics as m

    async def fake_top(cache_path, limit):
        return [{"tool": "x", "total_bytes": 1}]

    async def fake_clear(cache_path):
        return 3

    monkeypatch.setattr(m.metrics_service, "query_top_tools", fake_top)
    monkeypatch.setattr(m.metrics_service, "clear_metrics", fake_clear)

    got = await m._metrics({"action": "get"})
    assert got[0]["tool"] == "x"

    reset = await m._metrics({"action": "reset"})
    assert reset == {"deleted": 3}

    default = await m._metrics({})
    assert default[0]["tool"] == "x"  # default action is "get"


@pytest.mark.asyncio
async def test_tool_metrics_passes_limit(env, monkeypatch):
    from sqlserver_semantic_mcp.server.tools import metrics as m

    seen = {}

    async def fake_top(cache_path, limit):
        seen["limit"] = limit
        return []

    monkeypatch.setattr(m.metrics_service, "query_top_tools", fake_top)
    await m._metrics({"action": "get", "limit": 5})
    assert seen["limit"] == 5
