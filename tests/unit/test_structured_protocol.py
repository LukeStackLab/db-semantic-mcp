import json

import aiosqlite
import pytest
from mcp.types import Tool

from sqlserver_semantic_mcp.config import Config
from sqlserver_semantic_mcp.infrastructure.cache.store import init_store
from sqlserver_semantic_mcp.server import app as server


@pytest.fixture
async def cfg(tmp_path, monkeypatch):
    cfg = Config.model_construct(mssql_server="fake", mssql_database="db", cache_enabled=False,
                                 cache_path=str(tmp_path / "protocol.db"), metrics_enabled=True)
    await init_store(cfg.cache_path)
    monkeypatch.setattr(server, "get_config", lambda: cfg)
    monkeypatch.setattr(server, "_TOOL_REGISTRY", {})
    return cfg


@pytest.mark.asyncio
async def test_structured_output_preserves_unknown_count_and_empty_rows(cfg):
    async def handler(args):
        return {"executed": True, "rows": [], "total_row_count": None, "result_complete": False}
    server.register_tool(Tool(name="sample", inputSchema={"type": "object"}), handler)
    result = await server._call_tool_result("sample", {})
    assert result.isError is False
    assert result.structuredContent == json.loads(result.content[0].text)
    assert result.structuredContent["total_row_count"] is None
    assert result.structuredContent["rows"] == []
    assert result.structuredContent["result_complete"] is False
    assert server._TOOL_REGISTRY["sample"][0].outputSchema["type"] == "object"


@pytest.mark.asyncio
async def test_tool_failure_marks_error_and_records_category(cfg):
    async def handler(args):
        return {"data": {"executed": False, "validation": {"allowed": False}, "error": "denied"}}
    server.register_tool(Tool(name="denied", inputSchema={"type": "object"}), handler)
    result = await server._call_tool_result("denied", {})
    assert result.isError is True
    async with aiosqlite.connect(cfg.cache_path) as db:
        row = await (await db.execute("SELECT error_category,elapsed_ms FROM tool_metrics")).fetchone()
    assert row[0] is not None
    assert row[1] >= 0


@pytest.mark.asyncio
async def test_effective_modes_recorded_from_arguments(cfg):
    async def handler(args):
        return {"kind": "plan_or_execute_query", "detail": "brief", "data": {"path": "direct_execute", "executed": True}}
    server.register_tool(Tool(name="plan_or_execute_query", inputSchema={"type": "object"}), handler)
    await server._call_tool_result("plan_or_execute_query", {"return_mode": "sample", "token_budget_hint": "tiny"})
    async with aiosqlite.connect(cfg.cache_path) as db:
        row = await (await db.execute("SELECT route_type,response_mode,token_budget_hint FROM tool_metrics")).fetchone()
    assert row == ("direct_execute", "sample", "tiny")


@pytest.mark.asyncio
async def test_freshness_failure_does_not_hide_committed_write(cfg, monkeypatch):
    from unittest.mock import AsyncMock
    cfg.cache_enabled = True
    monkeypatch.setattr(server.revalidation, "maybe_revalidate", AsyncMock())
    monkeypatch.setattr(server.revalidation, "cache_freshness", AsyncMock(side_effect=RuntimeError("cache unavailable")))
    async def handler(args):
        return {"executed": True, "rows_affected": 1}
    server.register_tool(Tool(name="committed", inputSchema={"type": "object"}), handler)
    result = await server._call_tool_result("committed", {})
    assert result.isError is False
    assert result.structuredContent["executed"] is True
    assert result.structuredContent["rows_affected"] == 1
    assert result.structuredContent["cache"]["freshness_unknown"] is True


@pytest.mark.asyncio
async def test_metrics_write_does_not_run_schema_migration(cfg, monkeypatch):
    from sqlserver_semantic_mcp.services import metrics_service
    async def migration(*args):
        raise AssertionError("per-call migration")
    monkeypatch.setattr(metrics_service, "migrate_metrics", migration)
    await metrics_service.record_metric(cfg.cache_path, "fast", response_bytes=1, elapsed_ms=2)


@pytest.mark.asyncio
async def test_mcp_memory_transport_validates_structured_results(cfg):
    import anyio
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    async def success(args):
        return [{"table": "dbo.Public"}]
    async def failure(args):
        raise ValueError("invalid fixture argument")
    server.register_tool(Tool(name="get_tables", inputSchema={"type": "object"}), success)
    server.register_tool(Tool(name="fail", inputSchema={"type": "object"}), failure)
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(server.app.run, *server_streams, server.app.create_initialization_options())
            async with ClientSession(*client_streams) as client:
                await client.initialize()
                listed = await client.list_tools()
                assert all(tool.outputSchema["type"] == "object" for tool in listed.tools)
                result = await client.call_tool("get_tables", {})
                assert result.structuredContent == {"data": [{"table": "dbo.Public"}]}
                assert result.isError is False
                failed = await client.call_tool("fail", {})
                assert failed.isError is True
                assert "invalid fixture argument" in failed.structuredContent["error"]
            tasks.cancel_scope.cancel()
