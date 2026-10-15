from datetime import UTC

import pytest

from db_semantic_mcp.config import get_config, reset_config
from db_semantic_mcp.infrastructure.background import (
    run_background_fill_once,
)
from db_semantic_mcp.infrastructure.cache.semantic import (
    enqueue_all_tables,
    get_table_analysis,
    list_pending_table_analyses,
)
from db_semantic_mcp.infrastructure.cache.store import init_store
from db_semantic_mcp.infrastructure.cache.structural import (
    StructuralSnapshot,
    write_structural_snapshot,
)


@pytest.mark.asyncio
async def test_background_processes_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "testdb")
    monkeypatch.setenv("SEMANTIC_MCP_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_PASSWORD", "p")
    monkeypatch.setenv("SEMANTIC_MCP_CACHE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("SEMANTIC_MCP_BACKGROUND_BATCH_SIZE", "10")
    reset_config()
    cfg = get_config()

    await init_store(cfg.cache_path)
    snap = StructuralSnapshot(
        tables=[("dbo", "Users"), ("dbo", "Orders")],
        columns=[
            ("dbo", "Users", "Id", "int", None, 0, None, 1),
            ("dbo", "Users", "Name", "nvarchar", 50, 0, None, 2),
            ("dbo", "Orders", "Id", "int", None, 0, None, 1),
        ],
        primary_keys=[("dbo", "Users", "Id"), ("dbo", "Orders", "Id")],
        foreign_keys=[],
        indexes=[],
        objects=[],
        comments=[],
    )
    await write_structural_snapshot(cfg.cache_path, "testdb", snap)
    await enqueue_all_tables(cfg.cache_path, "testdb")

    processed = await run_background_fill_once(cfg)
    assert processed >= 2

    for s, t in [("dbo", "Users"), ("dbo", "Orders")]:
        r = await get_table_analysis(cfg.cache_path, "testdb", s, t)
        assert r["status"] == "ready"

    pending = await list_pending_table_analyses(cfg.cache_path, "testdb", 10)
    assert pending == []


@pytest.mark.asyncio
async def test_failed_item_retry_does_not_starve_unattempted_tables(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    import aiosqlite

    from db_semantic_mcp.config import Config
    from db_semantic_mcp.infrastructure import background

    cfg = Config(
        _env_file=None,
        server="fake",
        database="db",
        user="u",
        password="p",
        cache_path=str(tmp_path / "retry.db"),
        background_batch_size=1,
    )
    await init_store(cfg.cache_path)
    await write_structural_snapshot(
        cfg.cache_path,
        "db",
        StructuralSnapshot(
            tables=[("dbo", "A"), ("dbo", "B"), ("dbo", "C")],
            columns=[],
            primary_keys=[],
            foreign_keys=[],
            indexes=[],
            objects=[],
            comments=[],
        ),
    )
    await enqueue_all_tables(cfg.cache_path, "db")
    original = background.semantic_service.classify_table
    visited = []

    async def classify(db_path, database, schema, table, **kwargs):
        visited.append(table)
        if table == "A":
            raise RuntimeError("permanently unavailable")
        return await original(db_path, database, schema, table, **kwargs)

    monkeypatch.setattr(background.semantic_service, "classify_table", classify)
    assert await run_background_fill_once(cfg) == 1
    failure = await get_table_analysis(cfg.cache_path, "db", "dbo", "A")
    assert failure["status"] == "error"
    assert failure["attempt_count"] == 1
    assert failure["next_retry_at"] > failure["last_attempt_at"]
    async with aiosqlite.connect(cfg.cache_path) as db:
        await db.execute(
            "UPDATE sem_table_analysis SET next_retry_at=? WHERE table_name='A'",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
        )
        await db.commit()
    await run_background_fill_once(cfg)
    await run_background_fill_once(cfg)
    assert visited == ["A", "B", "C"]
    await run_background_fill_once(cfg)
    assert visited == ["A", "B", "C", "A"]
    failure = await get_table_analysis(cfg.cache_path, "db", "dbo", "A")
    assert failure["attempt_count"] == 2
    retry = datetime.fromisoformat(failure["next_retry_at"]) - datetime.fromisoformat(failure["last_attempt_at"])
    assert retry.total_seconds() == 2
    monkeypatch.setattr(background.semantic_service, "classify_table", original)
    async with aiosqlite.connect(cfg.cache_path) as db:
        await db.execute(
            "UPDATE sem_table_analysis SET next_retry_at=? WHERE table_name='A'",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
        )
        await db.commit()
    await run_background_fill_once(cfg)
    recovered = await get_table_analysis(cfg.cache_path, "db", "dbo", "A")
    assert recovered["status"] == "ready"
    assert recovered["attempt_count"] == 0
    assert recovered["next_retry_at"] is None
