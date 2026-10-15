"""End-to-end tests: _call_tool records metrics (P4)."""

import pytest


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_DATABASE", "testdb")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_PASSWORD", "p")
    monkeypatch.setenv("SEMANTIC_MCP_CACHE_PATH", str(tmp_path / "m.db"))
    # keep dispatcher tests hermetic: no background schema probe
    monkeypatch.setenv("SEMANTIC_MCP_CACHE_ENABLED", "false")
    from sqlserver_semantic_mcp.config import reset_config

    reset_config()


async def _call(name, args):
    from sqlserver_semantic_mcp.server.app import _call_tool

    return await _call_tool(name, args)


@pytest.mark.asyncio
async def test_successful_tool_call_records_metric(env):
    from mcp.types import Tool

    from sqlserver_semantic_mcp.config import get_config
    from sqlserver_semantic_mcp.infrastructure.cache.store import init_store
    from sqlserver_semantic_mcp.server.app import (
        _TOOL_REGISTRY,
        register_tool,
    )
    from sqlserver_semantic_mcp.services import metrics_service

    cfg = get_config()
    await init_store(cfg.cache_path)

    async def handler(args):
        return {"table": "dbo.X", "column_count": 5}

    _TOOL_REGISTRY.clear()
    register_tool(
        Tool(name="fake", description="x", inputSchema={"type": "object"}),
        handler,
    )

    await _call("fake", {})

    top = await metrics_service.query_top_tools(cfg.cache_path)
    assert len(top) == 1
    assert top[0]["tool_name"] == "fake"
    assert top[0]["call_count"] == 1
    assert top[0]["total_bytes"] > 0


@pytest.mark.asyncio
async def test_error_path_records_failure_metric(env):
    from mcp.types import Tool

    from sqlserver_semantic_mcp.config import get_config
    from sqlserver_semantic_mcp.infrastructure.cache.store import init_store
    from sqlserver_semantic_mcp.server.app import (
        _TOOL_REGISTRY,
        register_tool,
    )
    from sqlserver_semantic_mcp.services import metrics_service

    cfg = get_config()
    await init_store(cfg.cache_path)

    async def handler(args):
        raise RuntimeError("boom")

    _TOOL_REGISTRY.clear()
    register_tool(
        Tool(name="boom_tool", description="x", inputSchema={"type": "object"}),
        handler,
    )

    await _call("boom_tool", {})
    top = await metrics_service.query_top_tools(cfg.cache_path)
    assert len(top) == 1
    assert top[0]["error_count"] == 1


@pytest.mark.asyncio
async def test_metrics_disabled_skips_recording(env, monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_METRICS_ENABLED", "false")
    from sqlserver_semantic_mcp.config import reset_config

    reset_config()

    from mcp.types import Tool

    from sqlserver_semantic_mcp.config import get_config
    from sqlserver_semantic_mcp.infrastructure.cache.store import init_store
    from sqlserver_semantic_mcp.server.app import (
        _TOOL_REGISTRY,
        register_tool,
    )
    from sqlserver_semantic_mcp.services import metrics_service

    cfg = get_config()
    await init_store(cfg.cache_path)
    assert cfg.metrics_enabled is False

    async def handler(args):
        return {"ok": True}

    _TOOL_REGISTRY.clear()
    register_tool(
        Tool(name="silent", description="x", inputSchema={"type": "object"}),
        handler,
    )

    await _call("silent", {})
    top = await metrics_service.query_top_tools(cfg.cache_path)
    assert top == []


@pytest.mark.asyncio
async def test_list_response_captures_array_length(env):
    from mcp.types import Tool

    from sqlserver_semantic_mcp.config import get_config
    from sqlserver_semantic_mcp.infrastructure.cache.store import init_store
    from sqlserver_semantic_mcp.server.app import (
        _TOOL_REGISTRY,
        register_tool,
    )

    cfg = get_config()
    await init_store(cfg.cache_path)

    async def handler(args):
        return [{"name": f"c{i}"} for i in range(7)]

    _TOOL_REGISTRY.clear()
    register_tool(
        Tool(name="listy", description="x", inputSchema={"type": "object"}),
        handler,
    )
    await _call("listy", {})

    import aiosqlite

    async with aiosqlite.connect(cfg.cache_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM tool_metrics WHERE tool_name='listy'",
        )
        rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 1
    assert rows[0]["array_length"] == 7
    assert rows[0]["fields_returned"] == 1  # structured {data: [...]} envelope
