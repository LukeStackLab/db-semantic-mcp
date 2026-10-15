import json
from datetime import UTC

import aiosqlite
import pytest
from mcp.types import Tool

from db_semantic_mcp.config import Config
from db_semantic_mcp.infrastructure.cache.store import init_store
from db_semantic_mcp.server import app as server


@pytest.fixture
async def cfg(tmp_path, monkeypatch):
    cfg = Config.model_construct(
        server="fake",
        database="db",
        cache_enabled=False,
        cache_path=str(tmp_path / "protocol.db"),
        metrics_enabled=True,
    )
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


async def test_advertised_output_schema_matches_server_validation(cfg, monkeypatch):
    from db_semantic_mcp import config
    from db_semantic_mcp.server.tools import register_all

    monkeypatch.setattr(config, "get_config", lambda: cfg)
    register_all()
    for name, (tool, _) in server._TOOL_REGISTRY.items():
        assert tool.outputSchema == server._OUTPUT_VALIDATORS[name].schema
    listed = server._TOOL_REGISTRY["get_tables"][0].outputSchema
    assert listed["required"] == ["data"]
    assert listed["properties"]["data"] == {"type": "array", "items": {"type": "object"}}
    workflow = server._TOOL_REGISTRY["plan_or_execute_query"][0].outputSchema
    assert workflow["required"] == ["kind", "data"]
    assert workflow["properties"]["kind"] == {"const": "plan_or_execute_query"}


async def test_explicit_output_schema_is_preserved(cfg):
    async def handler(args):
        return {"answer": 42}

    schema = {"type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"]}
    tool = Tool(name="custom", inputSchema={"type": "object"}, outputSchema=schema)
    server.register_tool(tool, handler)
    result = await server._call_tool_result("custom", {})
    assert result.structuredContent == {"answer": 42}
    assert tool.outputSchema == schema == server._OUTPUT_VALIDATORS["custom"].schema


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
async def test_route_type_recorded_from_response(cfg):
    async def handler(args):
        return {"kind": "plan_or_execute_query", "data": {"path": "direct_execute", "executed": True}}

    server.register_tool(Tool(name="plan_or_execute_query", inputSchema={"type": "object"}), handler)
    await server._call_tool_result("plan_or_execute_query", {})
    async with aiosqlite.connect(cfg.cache_path) as db:
        row = await (await db.execute("SELECT route_type,was_direct_execute FROM tool_metrics")).fetchone()
    assert row == ("direct_execute", 1)


@pytest.mark.asyncio
async def test_freshness_failure_does_not_hide_committed_write(cfg, monkeypatch):
    from unittest.mock import AsyncMock

    cfg.cache_enabled = True
    monkeypatch.setattr(server.revalidation, "maybe_revalidate", AsyncMock())
    monkeypatch.setattr(
        server.revalidation, "cache_freshness", AsyncMock(side_effect=RuntimeError("cache unavailable"))
    )

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
    from db_semantic_mcp.services import metrics_service

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


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["tool", "resource"])
async def test_interleaved_snapshot_retains_payload_freshness(cfg, monkeypatch, endpoint):
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from pydantic import AnyUrl

    from db_semantic_mcp.infrastructure.cache import revalidation, structural
    from db_semantic_mcp.server.resources import schema as resources
    from db_semantic_mcp.services import metadata_service

    cfg.cache_enabled = True
    cfg.connection_name = None
    old = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    snap = structural.StructuralSnapshot(
        tables=[("dbo", "Users")],
        columns=[("dbo", "Users", "Id", "int", None, 0, None, 1)],
        primary_keys=[],
        foreign_keys=[],
        indexes=[],
        objects=[],
        comments=[],
    )
    await structural.write_structural_snapshot(cfg.cache_path, "db", snap)
    async with aiosqlite.connect(cfg.cache_path) as db:
        await db.execute("UPDATE schema_version SET captured_at=?", (old,))
        await db.commit()
    original_freshness = revalidation.cache_freshness

    async def interleave(_cfg):
        # Payload has already read the old columns and snapshot identity.
        snap.columns = [("dbo", "Users", "Id", "bigint", None, 0, None, 1)]
        await structural.write_structural_snapshot(cfg.cache_path, "db", snap)
        return await original_freshness(_cfg)

    monkeypatch.setattr(revalidation, "maybe_revalidate", AsyncMock())
    monkeypatch.setattr(revalidation, "cache_freshness", interleave)
    monkeypatch.setattr(resources, "get_context", lambda: SimpleNamespace(cfg=cfg, policy=None))
    if endpoint == "tool":

        async def handler(args):
            return await metadata_service.describe_table(cfg.cache_path, "db", "dbo", "Users")

        server.register_tool(Tool(name="sample", inputSchema={"type": "object"}), handler)
        result = await server._call_tool_result("sample", {})
        assert result.isError is False
        payload = result.structuredContent
        assert payload == json.loads(result.content[0].text)
    else:
        payload = json.loads(await resources.read_resource(AnyUrl("semantic://schema/tables/dbo.Users")))
    assert payload["columns"][0]["data_type"] == "int"
    assert payload["cache"]["captured_at"] == old
    assert payload["cache"]["stale"] is True
    assert payload["cache"]["age_seconds"] >= 86400
    assert payload["cache"]["refreshing"] is False


@pytest.mark.asyncio
async def test_snapshot_stale_flag_survives_compaction_and_elapsed_ttl(cfg, monkeypatch):
    from datetime import datetime, timedelta
    from unittest.mock import AsyncMock

    cfg.cache_enabled = True
    captured_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    monkeypatch.setattr(server.revalidation, "maybe_revalidate", AsyncMock())
    monkeypatch.setattr(
        server.revalidation,
        "cache_freshness",
        AsyncMock(
            return_value={
                "captured_at": datetime.now(UTC).isoformat(),
                "stale": False,
                "age_seconds": 0,
                "refreshing": True,
                "refresh_failed": False,
            }
        ),
    )

    async def handler(args):
        # This flag records the read. A delayed response can cross its TTL.
        return {"columns": [{"data_type": "int"}], "cache": {"hit": True, "captured_at": captured_at, "stale": False}}

    server.register_tool(Tool(name="sample", inputSchema={"type": "object"}), handler)
    result = await server._call_tool_result("sample", {})
    assert result.isError is False
    assert result.structuredContent["cache"]["captured_at"] == captured_at
    assert result.structuredContent["cache"]["stale"] is False
    assert result.structuredContent["cache"]["age_seconds"] >= 86400
    assert result.structuredContent["cache"]["refreshing"] is True


@pytest.mark.asyncio
async def test_classification_resource_without_identity_does_not_claim_latest_snapshot(cfg, monkeypatch):
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from pydantic import AnyUrl

    from db_semantic_mcp.infrastructure.cache import revalidation, structural
    from db_semantic_mcp.server.resources import schema as resources
    from db_semantic_mcp.services import semantic_service

    cfg.cache_enabled = True
    cfg.connection_name = None
    old_capture = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    snap = structural.StructuralSnapshot(
        tables=[("dbo", "T")],
        columns=[("dbo", "T", name, "nvarchar", 20, 0, None, i) for i, name in enumerate(("Code", "Name"), 1)],
        primary_keys=[],
        foreign_keys=[],
        indexes=[],
        objects=[],
        comments=[],
    )
    await structural.write_structural_snapshot(cfg.cache_path, "db", snap)
    async with aiosqlite.connect(cfg.cache_path) as db:
        await db.execute("UPDATE schema_version SET captured_at=?", (old_capture,))
        await db.commit()
    original_freshness = revalidation.cache_freshness

    async def interleave(_cfg):
        snap.columns = [("dbo", "T", name, "int", None, 0, None, i) for i, name in enumerate(("X", "Y", "Z"), 1)]
        await structural.write_structural_snapshot(cfg.cache_path, "db", snap)
        return await original_freshness(_cfg)

    monkeypatch.setattr(revalidation, "maybe_revalidate", AsyncMock())
    monkeypatch.setattr(revalidation, "cache_freshness", interleave)
    monkeypatch.setattr(resources, "get_context", lambda: SimpleNamespace(cfg=cfg, policy=None))
    payload = json.loads(await resources.read_resource(AnyUrl("semantic://analysis/classification/dbo.T")))
    current = await semantic_service.classify_table(cfg.cache_path, "db", "dbo", "T")
    assert payload["type"] == "lookup"
    assert current["type"] == "dimension"
    assert payload["cache"]["snapshot_unknown"] is True
    assert {"captured_at", "age_seconds", "stale"}.isdisjoint(payload["cache"])
    assert payload["cache"]["latest_snapshot"]["captured_at"] != old_capture
    assert payload["cache"]["latest_snapshot"]["stale"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["tool", "resource"])
async def test_metadata_list_array_without_identity_uses_latest_snapshot_envelope(cfg, monkeypatch, endpoint):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from pydantic import AnyUrl

    from db_semantic_mcp.infrastructure.cache import revalidation, structural
    from db_semantic_mcp.server.resources import schema as resources
    from db_semantic_mcp.services import metadata_service

    cfg.cache_enabled = True
    cfg.connection_name = None
    snap = structural.StructuralSnapshot(
        tables=[("dbo", "Old")], columns=[], primary_keys=[], foreign_keys=[], indexes=[], objects=[], comments=[]
    )
    await structural.write_structural_snapshot(cfg.cache_path, "db", snap)
    original_freshness = revalidation.cache_freshness

    async def interleave(_cfg):
        snap.tables = [("dbo", "New")]
        await structural.write_structural_snapshot(cfg.cache_path, "db", snap)
        return await original_freshness(_cfg)

    monkeypatch.setattr(revalidation, "maybe_revalidate", AsyncMock())
    monkeypatch.setattr(revalidation, "cache_freshness", interleave)
    monkeypatch.setattr(resources, "get_context", lambda: SimpleNamespace(cfg=cfg, policy=None))
    if endpoint == "tool":

        async def handler(args):
            return await metadata_service.list_tables(cfg.cache_path, "db")

        server.register_tool(Tool(name="sample", inputSchema={"type": "object"}), handler)
        result = await server._call_tool_result("sample", {})
        assert result.isError is False
        payload = result.structuredContent
        assert payload == json.loads(result.content[0].text)
    else:
        payload = json.loads(await resources.read_resource(AnyUrl("semantic://schema/tables")))
    expected_tables = [{"table": "dbo.Old"}] if endpoint == "tool" else [{"schema_name": "dbo", "table_name": "Old"}]
    assert payload["data"] == expected_tables
    assert await metadata_service.list_tables(cfg.cache_path, "db") == [{"schema_name": "dbo", "table_name": "New"}]
    assert payload["cache"]["snapshot_unknown"] is True
    assert {"captured_at", "age_seconds", "stale"}.isdisjoint(payload["cache"])
    assert payload["cache"]["latest_snapshot"]["stale"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["tool", "resource"])
@pytest.mark.parametrize("data", [{"executed": True, "rows_affected": 1}, [{"table": "dbo.T"}]])
async def test_identity_free_payload_survives_freshness_failure(cfg, monkeypatch, endpoint, data):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from pydantic import AnyUrl

    from db_semantic_mcp.server.resources import schema as resources

    cfg.cache_enabled = True
    cfg.connection_name = None
    monkeypatch.setattr(server.revalidation, "maybe_revalidate", AsyncMock())
    monkeypatch.setattr(
        server.revalidation, "cache_freshness", AsyncMock(side_effect=RuntimeError("cache unavailable"))
    )
    if endpoint == "tool":

        async def handler(args):
            return data

        server.register_tool(Tool(name="sample", inputSchema={"type": "object"}), handler)
        result = await server._call_tool_result("sample", {})
        assert result.isError is False
        payload = result.structuredContent
        assert payload == json.loads(result.content[0].text)
    else:
        monkeypatch.setattr(resources, "get_context", lambda: SimpleNamespace(cfg=cfg))
        monkeypatch.setattr(resources, "_read_resource_payload", AsyncMock(return_value=json.dumps(data)))
        payload = json.loads(await resources.read_resource(AnyUrl("semantic://summary/database")))
    expected_data = payload["data"] if isinstance(data, list) else {key: payload[key] for key in data}
    assert expected_data == data
    assert payload["cache"]["snapshot_unknown"] is True
    assert payload["cache"]["freshness_unknown"] is True
    assert payload["cache"]["latest_snapshot"] == {"freshness_unknown": True}
    assert {"captured_at", "age_seconds", "stale"}.isdisjoint(payload["cache"])
