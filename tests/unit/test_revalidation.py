import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from db_semantic_mcp.config import get_config, reset_config
from db_semantic_mcp.infrastructure.cache import revalidation
from db_semantic_mcp.infrastructure.cache.probe import SchemaFingerprints
from db_semantic_mcp.infrastructure.cache.semantic import (
    get_object_definition,
    upsert_object_definition,
)
from db_semantic_mcp.infrastructure.cache.store import init_store
from db_semantic_mcp.infrastructure.cache.structural import (
    StructuralSnapshot,
    compute_object_hash,
)

MOD = "db_semantic_mcp.infrastructure.cache.revalidation"


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "testdb")
    monkeypatch.setenv("SEMANTIC_MCP_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_PASSWORD", "p")
    monkeypatch.setenv("SEMANTIC_MCP_CACHE_PATH", str(tmp_path / "t.db"))
    reset_config()
    revalidation.reset_state()
    yield get_config()
    revalidation.reset_state()
    reset_config()


def _fps(tables=None, objects=None, dependents=None) -> SchemaFingerprints:
    return SchemaFingerprints(
        tables=tables or {},
        objects=objects or {},
        table_dependents=dependents or {},
    )


async def _seed_baseline(cfg, fps: SchemaFingerprints) -> None:
    from db_semantic_mcp.infrastructure.cache.probe import (
        write_fingerprints,
    )

    await init_store(cfg.cache_path)
    async with aiosqlite.connect(cfg.cache_path) as db:
        await write_fingerprints(db, cfg.database, fps)
        await db.execute(
            "INSERT OR REPLACE INTO schema_version VALUES (?,?,?,?,?)",
            (cfg.database, "h", "o", "c", datetime.now(UTC).isoformat()),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_no_drift_skips_warmup(cfg):
    fps = _fps(tables={("dbo", "Users"): "fp1"})
    await _seed_baseline(cfg, fps)

    with (
        patch(f"{MOD}.fetch_fingerprints", return_value=fps),
        patch(f"{MOD}.warmup_structural_cache", AsyncMock(return_value={})) as warmup,
    ):
        result = await revalidation.revalidate_if_stale(cfg)

    warmup.assert_not_awaited()
    assert result["probed"] is True
    assert result["refreshed"] is False


@pytest.mark.asyncio
async def test_drift_triggers_warmup_and_dependency_cascade(cfg):
    objects = [("dbo", "vw_Users", "VIEW")]
    await _seed_baseline(
        cfg,
        _fps(
            tables={("dbo", "Users"): "fp-old"},
            objects={("dbo", "vw_Users", "V"): "fp-v"},
        ),
    )
    # vw_Users analysis is currently ready; Users will change underneath it
    await upsert_object_definition(
        cfg.cache_path,
        "testdb",
        "dbo",
        "vw_Users",
        "VIEW",
        object_hash=compute_object_hash(objects),
        status="ready",
        definition="CREATE VIEW ...",
    )

    fresh = _fps(
        tables={("dbo", "Users"): "fp-new"},
        objects={("dbo", "vw_Users", "V"): "fp-v"},
        dependents={("dbo", "Users"): [("dbo", "vw_Users")]},
    )
    snapshot = StructuralSnapshot(
        tables=[("dbo", "Users")],
        columns=[],
        primary_keys=[],
        foreign_keys=[],
        indexes=[],
        objects=objects,
        comments=[],
    )
    with (
        patch(f"{MOD}.fetch_fingerprints", return_value=fresh),
        patch(
            "db_semantic_mcp.infrastructure.cache.structural.fetch_snapshot_from_server", return_value=snapshot
        ),
        patch(f"{MOD}.warmup_structural_cache", wraps=revalidation.warmup_structural_cache) as warmup,
    ):
        result = await revalidation.revalidate_if_stale(cfg)

    warmup.assert_awaited_once_with(cfg, fingerprints=fresh)
    assert result["refreshed"] is True
    assert result["tables_changed"] == 1
    assert result["objects_marked_dirty"] == 1
    obj = await get_object_definition(
        cfg.cache_path,
        "testdb",
        "dbo",
        "vw_Users",
        "VIEW",
    )
    assert obj["status"] == "dirty"


@pytest.mark.asyncio
async def test_changed_view_body_marks_object_dirty(cfg):
    objects = [("dbo", "vw_X", "VIEW")]
    await _seed_baseline(
        cfg,
        _fps(
            objects={("dbo", "vw_X", "V"): "old-def"},
        ),
    )
    await upsert_object_definition(
        cfg.cache_path,
        "testdb",
        "dbo",
        "vw_X",
        "VIEW",
        object_hash=compute_object_hash(objects),
        status="ready",
    )
    fresh = _fps(objects={("dbo", "vw_X", "V"): "new-def"})
    snapshot = StructuralSnapshot(
        tables=[], columns=[], primary_keys=[], foreign_keys=[], indexes=[], objects=objects, comments=[]
    )
    with (
        patch(f"{MOD}.fetch_fingerprints", return_value=fresh),
        patch(
            "db_semantic_mcp.infrastructure.cache.structural.fetch_snapshot_from_server", return_value=snapshot
        ),
    ):
        result = await revalidation.revalidate_if_stale(cfg)

    assert result["objects_changed"] == 1
    assert result["objects_marked_dirty"] == 1
    obj = await get_object_definition(
        cfg.cache_path,
        "testdb",
        "dbo",
        "vw_X",
        "VIEW",
    )
    assert obj["status"] == "dirty"


@pytest.mark.asyncio
async def test_missing_baseline_warms_up_without_dirty_marks(cfg):
    await init_store(cfg.cache_path)
    fresh = _fps(tables={("dbo", "Users"): "fp"})
    with (
        patch(f"{MOD}.fetch_fingerprints", return_value=fresh),
        patch(f"{MOD}.warmup_structural_cache", AsyncMock(return_value={})) as warmup,
    ):
        result = await revalidation.revalidate_if_stale(cfg)

    warmup.assert_awaited_once()
    assert result["baseline_created"] is True
    assert result["objects_marked_dirty"] == 0


@pytest.mark.asyncio
async def test_throttle_skips_until_interval_elapses(cfg):
    fps = _fps(tables={("dbo", "Users"): "fp"})
    await _seed_baseline(cfg, fps)

    with (
        patch(f"{MOD}.fetch_fingerprints", return_value=fps) as fetch,
        patch(f"{MOD}.warmup_structural_cache", AsyncMock(return_value={})),
    ):
        first = await revalidation.revalidate_if_stale(cfg)
        second = await revalidation.revalidate_if_stale(cfg)

    assert first is not None
    assert second is None  # within probe_interval_s window
    assert fetch.call_count == 1


@pytest.mark.asyncio
async def test_probe_failure_is_swallowed_and_throttled(cfg):
    await _seed_baseline(cfg, _fps(tables={("dbo", "Users"): "fp"}))

    def boom(_cfg):
        raise ConnectionError("db down")

    with (
        patch(f"{MOD}.fetch_fingerprints", side_effect=boom) as fetch,
        patch(f"{MOD}.warmup_structural_cache", AsyncMock(return_value={})) as warmup,
    ):
        result = await revalidation.revalidate_if_stale(cfg)
        again = await revalidation.revalidate_if_stale(cfg)

    assert result is None
    assert again is None
    assert fetch.call_count == 1  # failure also pushes the throttle window
    warmup.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_revalidate_cache_disabled_noop(cfg, monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_CACHE_ENABLED", "false")
    reset_config()
    cfg = get_config()

    with patch(f"{MOD}.fetch_fingerprints") as fetch:
        await revalidation.maybe_revalidate(cfg)
        await asyncio.sleep(0)

    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_revalidate_runs_in_background(cfg):
    fps = _fps(tables={("dbo", "Users"): "fp"})
    await _seed_baseline(cfg, fps)

    with patch(f"{MOD}.fetch_fingerprints", return_value=fps) as fetch:
        await revalidation.maybe_revalidate(cfg)
        pending = list(revalidation._BACKGROUND_TASKS)
        assert len(pending) == 1
        await asyncio.gather(*pending)

    fetch.assert_called_once()
    assert not revalidation.is_due(cfg)


@pytest.mark.asyncio
async def test_note_refreshed_pushes_window(cfg):
    assert revalidation.is_due(cfg)
    revalidation.note_refreshed(cfg)
    assert not revalidation.is_due(cfg)


@pytest.mark.parametrize("captured_at", [None, "invalid capture time"])
def test_merge_preserves_unknown_snapshot_identity(cfg, captured_at):
    metadata = {"hit": True, "captured_at": captured_at, "stale": True}
    current = {
        "captured_at": datetime.now(UTC).isoformat(),
        "age_seconds": 0,
        "stale": False,
        "refreshing": True,
        "refresh_failed": True,
    }
    merged = revalidation.merge_cache_metadata(cfg, metadata, current)
    assert merged["captured_at"] == captured_at
    assert merged["age_seconds"] is None
    assert merged["stale"] is True
    assert merged["refreshing"] is True
    assert merged["refresh_failed"] is True


def test_merge_preserves_compacted_unknown_snapshot_identity(cfg):
    merged = revalidation.merge_cache_metadata(
        cfg,
        {"hit": True, "stale": True},
        {"captured_at": datetime.now(UTC).isoformat(), "age_seconds": 0, "stale": False},
    )
    assert merged["captured_at"] is None
    assert merged["age_seconds"] is None
    assert merged["stale"] is True


def test_merge_recovers_compacted_fresh_snapshot_flag(cfg):
    captured_at = datetime.now(UTC).isoformat()
    merged = revalidation.merge_cache_metadata(
        cfg,
        {"hit": True, "captured_at": captured_at},
        {"captured_at": "older unrelated snapshot", "age_seconds": None, "stale": True},
    )
    assert merged["captured_at"] == captured_at
    assert merged["stale"] is False
    assert merged["age_seconds"] is not None


@pytest.mark.asyncio
async def test_many_dispatches_share_exactly_one_background_task(cfg):
    from threading import Event

    entered, release = Event(), Event()
    fps = _fps(tables={("dbo", "Users"): "fp"})
    await _seed_baseline(cfg, fps)

    def probe(_cfg):
        entered.set()
        assert release.wait(2)
        return fps

    with patch(f"{MOD}.fetch_fingerprints", side_effect=probe) as fetch:
        try:
            for _ in range(30):
                await revalidation.maybe_revalidate(cfg)
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.001)
            assert entered.is_set()
            assert len(revalidation._BACKGROUND_TASKS) == 1
            waiter = asyncio.create_task(revalidation.revalidate_if_stale(cfg, force=True))
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert len(revalidation._BACKGROUND_TASKS) == 1
        finally:
            release.set()
            await asyncio.gather(*list(revalidation._BACKGROUND_TASKS))
    fetch.assert_called_once()


@pytest.mark.asyncio
async def test_comments_only_snapshot_expiry_forces_refresh(cfg):
    fps = _fps(tables={("dbo", "Users"): "same-schema"})
    await _seed_baseline(cfg, fps)
    old = (datetime.now(UTC) - timedelta(seconds=cfg.schema_cache_ttl_s + 1)).isoformat()
    async with aiosqlite.connect(cfg.cache_path) as db:
        await db.execute("UPDATE schema_version SET captured_at=?", (old,))
        await db.commit()
    with (
        patch(f"{MOD}.fetch_fingerprints", return_value=fps),
        patch(f"{MOD}.warmup_structural_cache", AsyncMock(return_value={})) as warmup,
    ):
        result = await revalidation.revalidate_if_stale(cfg)
    warmup.assert_awaited_once_with(cfg, fingerprints=fps)
    assert result["tables_changed"] == 0
    assert result["snapshot_expired"] is True
    assert result["refreshed"] is True


@pytest.mark.asyncio
async def test_failed_expired_snapshot_stays_stale_and_keeps_capture_time(cfg):
    fps = _fps(tables={("dbo", "Users"): "same"})
    await _seed_baseline(cfg, fps)
    old = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    async with aiosqlite.connect(cfg.cache_path) as db:
        await db.execute("UPDATE schema_version SET captured_at=?", (old,))
        await db.commit()
    with (
        patch(f"{MOD}.fetch_fingerprints", return_value=fps),
        patch(f"{MOD}.warmup_structural_cache", AsyncMock(side_effect=ConnectionError("down"))),
    ):
        assert await revalidation.revalidate_if_stale(cfg) is None
    freshness = await revalidation.cache_freshness(cfg)
    assert freshness["captured_at"] == old
    assert freshness["stale"] is True
    assert freshness["refresh_failed"] is True
    assert freshness["refreshing"] is False


@pytest.mark.asyncio
async def test_comments_only_expiry_updates_persisted_descriptions(cfg):
    from db_semantic_mcp.infrastructure.cache.structural import StructuralSnapshot, write_structural_snapshot

    fps = _fps(tables={("dbo", "Users"): "unchanged"})
    await init_store(cfg.cache_path)
    base = dict(tables=[("dbo", "Users")], columns=[], primary_keys=[], foreign_keys=[], indexes=[], objects=[])
    await write_structural_snapshot(
        cfg.cache_path,
        "testdb",
        StructuralSnapshot(**base, comments=[("dbo", "Users", "", "old description")]),
        fingerprints=fps,
    )
    old = (datetime.now(UTC) - timedelta(seconds=cfg.schema_cache_ttl_s + 1)).isoformat()
    async with aiosqlite.connect(cfg.cache_path) as db:
        await db.execute("UPDATE schema_version SET captured_at=?", (old,))
        await db.commit()
    new_snapshot = StructuralSnapshot(**base, comments=[("dbo", "Users", "", "new description")])
    with (
        patch(f"{MOD}.fetch_fingerprints", return_value=fps),
        patch(
            "db_semantic_mcp.infrastructure.cache.structural.fetch_snapshot_from_server",
            return_value=new_snapshot,
        ),
    ):
        result = await revalidation.revalidate_if_stale(cfg)
    assert result["snapshot_expired"] is True
    assert result["tables_changed"] == 0
    async with aiosqlite.connect(cfg.cache_path) as db:
        cursor = await db.execute("SELECT description FROM sc_comments")
        assert (await cursor.fetchone())[0] == "new description"
    freshness = await revalidation.cache_freshness(cfg)
    assert freshness["stale"] is False
    assert freshness["captured_at"] != old


@pytest.mark.asyncio
async def test_manual_refresh_serializes_after_probe_and_coalesces_callers(cfg, monkeypatch):
    assert hasattr(revalidation, "refresh_now")
    old = _fps(tables={("dbo", "Users"): "old"})
    fresh = _fps(tables={("dbo", "Users"): "new"})
    await _seed_baseline(cfg, old)
    probe_started, release_probe = asyncio.Event(), asyncio.Event()
    manual_started, release_manual = asyncio.Event(), asyncio.Event()
    active, maximum, events = 0, 0, []

    async def warmup(_cfg, fingerprints=None):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        phase = "probe" if fingerprints is not None else "manual"
        events.append(phase + "_start")
        try:
            if phase == "probe":
                probe_started.set()
                await release_probe.wait()
            else:
                manual_started.set()
                await release_manual.wait()
            return {"structural_hash": phase}
        finally:
            events.append(phase + "_end")
            active -= 1

    monkeypatch.setattr(revalidation, "fetch_fingerprints", lambda _cfg: fresh)
    monkeypatch.setattr(revalidation, "warmup_structural_cache", warmup)
    probe = asyncio.create_task(revalidation.revalidate_if_stale(cfg, force=True))
    manual_calls = []
    try:
        await asyncio.wait_for(probe_started.wait(), 2)
        manual_calls = [asyncio.create_task(revalidation.refresh_now(cfg)) for _ in range(8)]
        await asyncio.sleep(0)
        assert not manual_started.is_set()
        assert (await revalidation.cache_freshness(cfg))["refreshing"]
        release_probe.set()
        await asyncio.wait_for(manual_started.wait(), 2)
        # The old probe's completion callback must not clear its successor.
        assert revalidation._state(cfg).task is not None
        assert revalidation._state(cfg).manual is True
        monkeypatch.setattr(revalidation, "is_due", lambda _cfg: True)
        await revalidation.maybe_revalidate(cfg)
        assert events == ["probe_start", "probe_end", "manual_start"]
        release_manual.set()
        assert await asyncio.gather(*manual_calls) == [{"structural_hash": "manual"}] * 8
        assert maximum == 1
        assert events == ["probe_start", "probe_end", "manual_start", "manual_end"]
        await probe
        assert revalidation._state(cfg).task is None
        assert not revalidation._state(cfg).manual
        assert not revalidation._BACKGROUND_TASKS
    finally:
        release_probe.set()
        release_manual.set()
        await asyncio.gather(probe, *manual_calls, return_exceptions=True)


@pytest.mark.asyncio
async def test_explicit_refresh_always_fetches_full_snapshot_without_drift(cfg, monkeypatch):
    assert hasattr(revalidation, "refresh_now")
    fps = _fps(tables={("dbo", "Users"): "same"})
    await _seed_baseline(cfg, fps)
    warmup = AsyncMock(return_value={"structural_hash": "full"})
    monkeypatch.setattr(revalidation, "warmup_structural_cache", warmup)
    monkeypatch.setattr(revalidation, "fetch_fingerprints", lambda _cfg: fps)
    assert not (await revalidation.revalidate_if_stale(cfg))["refreshed"]
    assert not revalidation.is_due(cfg)
    assert await revalidation.refresh_now(cfg) == {"structural_hash": "full"}
    warmup.assert_awaited_once_with(cfg)


@pytest.mark.asyncio
async def test_manual_refresh_failure_propagates_and_keeps_capture_time(cfg, monkeypatch):
    assert hasattr(revalidation, "refresh_now")
    await _seed_baseline(cfg, _fps(tables={("dbo", "Users"): "same"}))
    before = await revalidation.cache_freshness(cfg)
    monkeypatch.setattr(revalidation, "warmup_structural_cache", AsyncMock(side_effect=ConnectionError("offline")))
    with pytest.raises(ConnectionError, match="offline"):
        await revalidation.refresh_now(cfg)
    after = await revalidation.cache_freshness(cfg)
    assert after["captured_at"] == before["captured_at"]
    assert after["refresh_failed"] is True
    assert after["refreshing"] is False
    assert not revalidation._BACKGROUND_TASKS


@pytest.mark.asyncio
async def test_cancelled_manual_waiter_does_not_cancel_shared_snapshot(cfg, monkeypatch):
    assert hasattr(revalidation, "refresh_now")
    started, release = asyncio.Event(), asyncio.Event()

    async def warmup(_cfg):
        started.set()
        await release.wait()
        return {"structural_hash": "complete"}

    monkeypatch.setattr(revalidation, "warmup_structural_cache", warmup)
    first = asyncio.create_task(revalidation.refresh_now(cfg))
    second = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        shared = revalidation._state(cfg).task
        second = asyncio.create_task(revalidation.refresh_now(cfg))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not shared.cancelled()
        assert shared in revalidation._BACKGROUND_TASKS
        release.set()
        assert await second == {"structural_hash": "complete"}
        assert not revalidation._BACKGROUND_TASKS
    finally:
        release.set()
        await asyncio.gather(*([first, second] if second else [first]), return_exceptions=True)


@pytest.mark.asyncio
async def test_cache_refresh_tool_uses_shared_manual_entry_point(cfg, monkeypatch):
    from types import SimpleNamespace

    from db_semantic_mcp.server.tools import cache

    refresh = AsyncMock(return_value={"structural_hash": "shared"})
    direct = AsyncMock(return_value={"structural_hash": "bypassed"})
    monkeypatch.setattr(revalidation, "refresh_now", refresh, raising=False)
    monkeypatch.setattr(cache, "get_context", lambda: SimpleNamespace(cfg=cfg))
    monkeypatch.setattr(cache, "warmup_structural_cache", direct, raising=False)
    assert await cache._refresh({}) == {"refreshed": True, "structural_hash": "shared"}
    refresh.assert_awaited_once_with(cfg)
    direct.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_manual_waiter_keeps_refresh_queued_after_probe(cfg, monkeypatch):
    probe_started, release_probe = asyncio.Event(), asyncio.Event()

    async def probe(_cfg, _state):
        probe_started.set()
        await release_probe.wait()
        return {"probed": True}

    warmup = AsyncMock(return_value={"structural_hash": "manual"})
    monkeypatch.setattr(revalidation, "_run_probe", probe)
    monkeypatch.setattr(revalidation, "warmup_structural_cache", warmup)
    pending_probe = revalidation._start_probe(cfg, revalidation._state(cfg))
    callers = []
    try:
        await asyncio.wait_for(probe_started.wait(), 2)
        first = asyncio.create_task(revalidation.refresh_now(cfg))
        callers.append(first)
        await asyncio.sleep(0)
        manual = revalidation._state(cfg).task
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not pending_probe.cancelled()
        assert not manual.cancelled()
        warmup.assert_not_awaited()
        second = asyncio.create_task(revalidation.refresh_now(cfg))
        callers.append(second)
        await asyncio.sleep(0)
        assert revalidation._state(cfg).task is manual
        release_probe.set()
        assert await second == {"structural_hash": "manual"}
        warmup.assert_awaited_once_with(cfg)
        assert not revalidation._BACKGROUND_TASKS
    finally:
        release_probe.set()
        await asyncio.gather(pending_probe, *callers, return_exceptions=True)
