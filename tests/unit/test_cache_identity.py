"""All cache entrypoints bind authority before using persisted metadata."""

from unittest.mock import AsyncMock

import pytest

from db_semantic_mcp.config import Config, get_config, reset_config
from db_semantic_mcp.infrastructure.cache.store import init_store
from db_semantic_mcp.infrastructure.cache.structural import StructuralSnapshot, write_structural_snapshot
from db_semantic_mcp.server.app import Context, reset_context
from db_semantic_mcp.server.source_context import source_context
from db_semantic_mcp.services.relationship_service import get_table_relationships


def config(tmp_path, **changes):
    return Config.from_fixed(
        **dict(
            database_type="oracle",
            server="a",
            user="A",
            database="SAME",
            oracle={"service_name": "SAME"},
            cache_path=str(tmp_path / "shared.db"),
            sources={"remote": {"db_link": "REMOTE"}},
        )
        | changes
    )


@pytest.mark.parametrize(
    "change",
    [
        {"server": "b"},
        {"user": "B"},
        {"port": 1522},
        {"oracle": {"service_name": "OTHER"}},
        {"oracle": {"service_name": "SAME", "driver_mode": "thick"}},
        {"oracle": {"service_name": "SAME", "current_schema": "OTHER"}},
        {"encrypt": True},
        {"database_type": "sqlserver", "oracle": None, "sources": {}},
    ],
)
@pytest.mark.asyncio
async def test_local_context_identity_changes_with_authority(tmp_path, change):
    first = Context(config(tmp_path))
    second = Context(config(tmp_path, **change))
    a, b = await source_context(first), await source_context(second)
    assert a.cfg.cache_path != b.cfg.cache_path
    assert first.cfg._base_cache_path == str(tmp_path / "shared.db")
    assert a.cfg.for_source("local").cache_path == a.cfg.cache_path
    assert a.cfg.for_source("remote").for_source("local").cache_path == a.cfg.cache_path
    same = await source_context(Context(config(tmp_path)))
    assert same.cfg.cache_path == a.cfg.cache_path


def test_execution_recording_flag_does_not_change_cache_identity(tmp_path):
    off = Context(config(tmp_path, execution_recording_enabled=False))
    on = Context(config(tmp_path, execution_recording_enabled=True))
    assert off.cfg.cache_path == on.cfg.cache_path
    assert off.cfg.tool_profile == on.cfg.tool_profile
    assert off.policy.current_policy().model_dump() == on.policy.current_policy().model_dump()


@pytest.mark.asyncio
async def test_policy_reload_new_namespace_and_inflight_snapshot(tmp_path, monkeypatch):
    from db_semantic_mcp.server import source_context as sources

    base = Context(config(tmp_path, cache_enabled=False))
    old = await source_context(base)
    before = old.cfg.cache_path
    later = base.policy.current_policy().model_copy(deep=True)
    later.scope.allowed_schemas = ["PUBLIC_ONLY"]
    monkeypatch.setattr("db_semantic_mcp.services.policy_service.load_active_policy", lambda cfg: later)
    base.policy.reload()
    initialized = AsyncMock(side_effect=lambda ctx: ctx)
    monkeypatch.setattr(sources, "_initialize", initialized)
    after = await source_context(base)
    assert after.cfg.cache_path != before
    assert old.cfg.for_source("local").cache_path == before
    assert old.policy.current_policy().scope.allowed_schemas == []
    initialized.assert_awaited_once()
    assert (await source_context(base)).cfg.cache_path == after.cfg.cache_path
    assert initialized.await_count == 1


@pytest.mark.asyncio
async def test_env_startup_scopes_before_init_and_cache_first(tmp_path, monkeypatch):
    from db_semantic_mcp import main

    monkeypatch.setenv("SEMANTIC_MCP_DATABASE_TYPE", "oracle")
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "a")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "SAME")
    monkeypatch.setenv("SEMANTIC_MCP_ORACLE", '{"service_name":"SAME"}')
    monkeypatch.setenv("SEMANTIC_MCP_CACHE_PATH", str(tmp_path / "shared.db"))
    monkeypatch.setattr(main, "register_all", lambda: None)
    monkeypatch.setattr(main, "register_prompts", lambda: None)
    monkeypatch.setattr(main, "enqueue_all_tables", AsyncMock())
    monkeypatch.setattr(main, "background_fill_loop", AsyncMock())
    seen = []

    async def initialize(path):
        seen.append(path)
        assert path != str(tmp_path / "shared.db")
        await init_store(path)

    monkeypatch.setattr(main, "init_store", initialize)
    warmup = AsyncMock()
    monkeypatch.setattr(main, "warmup_structural_cache", warmup)
    # The old unscoped cache exists, but may never authorize cache_first reuse.
    await init_store(str(tmp_path / "shared.db"))
    await write_structural_snapshot(str(tmp_path / "shared.db"), "SAME", StructuralSnapshot([], [], [], [], [], [], []))
    try:
        for user in ("A", "B", "A"):
            monkeypatch.setenv("SEMANTIC_MCP_USER", user)
            reset_config()
            reset_context()
            cfg = get_config()
            task = await main._startup()
            await task
            await write_structural_snapshot(
                cfg.cache_path, cfg.database, StructuralSnapshot([], [], [], [], [], [], [])
            )
        assert seen[0] == seen[2] and seen[0] != seen[1]
        assert warmup.await_count == 2
        assert (tmp_path / "shared.db").exists()
    finally:
        reset_context()
        reset_config()


@pytest.mark.asyncio
async def test_relationship_reader_cannot_reuse_other_principal_cache(tmp_path):
    first, second = Context(config(tmp_path)), Context(config(tmp_path, user="B"))
    for ctx in (first, second):
        await init_store(ctx.cfg.cache_path)
    snapshot = StructuralSnapshot(
        [("APP", "PARENT"), ("APP", "CHILD")],
        [],
        [],
        [("APP", "CHILD", "PID", "APP", "PARENT", "ID", "APP.FK", "FK", 1, 0, 0)],
        [],
        [],
        [],
    )
    await write_structural_snapshot(first.cfg.cache_path, "SAME", snapshot)
    a = await get_table_relationships(first.cfg.cache_path, "SAME", "APP", "CHILD", policy=first.policy)
    b = await get_table_relationships(second.cfg.cache_path, "SAME", "APP", "CHILD", policy=second.policy)
    assert a and b == []
