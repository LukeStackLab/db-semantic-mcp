"""Unit tests for the get_execution_policy tool's reload flag (Task 9)."""

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
async def test_get_execution_policy_reload(env, monkeypatch):
    from sqlserver_semantic_mcp.server.tools import policy as policy_tool

    ctx = policy_tool.get_context()
    called = {"reload": 0}
    monkeypatch.setattr(
        ctx.policy,
        "reload",
        lambda: called.__setitem__("reload", called["reload"] + 1),
    )

    out = await policy_tool._get_policy({"reload": True})
    assert called["reload"] == 1
    assert out["reloaded"] is True
    assert out["profile"] == ctx.policy.current_policy().profile_name

    out2 = await policy_tool._get_policy({})
    assert called["reload"] == 1  # default does not reload
    assert "reloaded" not in out2


@pytest.mark.asyncio
async def test_get_execution_policy_default_returns_policy_dict(env):
    from sqlserver_semantic_mcp.server.tools import policy as policy_tool

    out = await policy_tool._get_policy({})
    assert "profile_name" in out
