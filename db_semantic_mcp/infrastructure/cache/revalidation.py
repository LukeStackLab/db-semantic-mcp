"""Single-flight schema probes with stale-while-revalidate snapshot freshness."""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from ...config import Config
from ..workers import run_db_work
from .probe import diff_fingerprints, fetch_fingerprints, read_fingerprints
from .structural import read_schema_version, warmup_structural_cache

logger = logging.getLogger(__name__)


@dataclass
class _ProbeState:
    last_checked: float = 0.0
    task: asyncio.Task | None = None
    refresh_failed: bool = False
    manual: bool = False


_STATES: dict[tuple[str, str], _ProbeState] = {}
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _trim_states() -> None:
    # Retargeted sources get new namespaces. Keep diagnostics bounded without
    # evicting a running single-flight operation.
    for key in list(_STATES)[:-1]:
        if len(_STATES) <= 256:
            break
        state = _STATES[key]
        if state.task is None or state.task.done():
            _STATES.pop(key, None)


def _state(cfg: Config) -> _ProbeState:
    key = (cfg.cache_path, cfg.database)
    if key not in _STATES:
        _STATES[key] = _ProbeState()
    value = _STATES.pop(key)
    _STATES[key] = value
    _trim_states()
    return value


def reset_state() -> None:
    """Test helper; callers must finish outstanding work before resetting."""
    _STATES.clear()


def note_refreshed(cfg: Config) -> None:
    state = _state(cfg)
    state.last_checked = time.monotonic()
    state.refresh_failed = False


def is_due(cfg: Config) -> bool:
    state = _state(cfg)
    interval = min(cfg.probe_interval_s, cfg.schema_cache_ttl_s)
    return state.last_checked == 0.0 or time.monotonic() - state.last_checked >= interval


def snapshot_age(captured_at) -> float | None:
    """Seconds since ``captured_at`` (ISO timestamp, naive treated as UTC)."""
    if not captured_at:
        return None
    try:
        captured = datetime.fromisoformat(captured_at)
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=UTC)
        return max(0.0, (datetime.now(UTC) - captured).total_seconds())
    except (TypeError, ValueError):
        return None


def snapshot_freshness(captured_at, ttl: float, *, hit: bool | None = None) -> dict:
    age = snapshot_age(captured_at)
    payload = {
        "captured_at": captured_at,
        "age_seconds": round(age, 3) if age is not None else None,
        "stale": age is None or age >= ttl,
    }
    if hit is not None:
        payload["hit"] = hit
    return payload


_snapshot_freshness = snapshot_freshness


def merge_cache_metadata(cfg: Config, metadata: dict, freshness: dict) -> dict:
    """Separate payload snapshot identity from latest persisted diagnostics."""
    # compact() can omit an unknown capture time, but hit still identifies
    # metadata tied to the snapshot read by the handler.
    if "captured_at" in metadata or metadata.get("hit"):
        merged = {**metadata, **freshness}
        merged.update(_snapshot_freshness(metadata.get("captured_at"), cfg.schema_cache_ttl_s))
        if "stale" in metadata:
            merged["stale"] = metadata["stale"]
        return merged
    # A current cache capture cannot identify content already read by a
    # handler that did not return its own snapshot metadata.
    merged = {key: value for key, value in metadata.items() if key not in {"captured_at", "age_seconds", "stale"}}
    merged.update(
        snapshot_unknown=True,
        latest_snapshot={key: value for key, value in freshness.items() if key not in {"refreshing", "refresh_failed"}},
    )
    for key in ("refreshing", "refresh_failed", "freshness_unknown"):
        if key in freshness:
            merged[key] = freshness[key]
    return merged


async def cache_freshness(cfg: Config) -> dict:
    """Report persisted snapshot age, including DB-offline stale responses."""
    version = await read_schema_version(cfg.cache_path, cfg.database)
    captured_at = version.get("captured_at") if version else None
    state = _state(cfg)
    return {
        **_snapshot_freshness(captured_at, cfg.schema_cache_ttl_s),
        "refreshing": state.task is not None and not state.task.done(),
        "refresh_failed": state.refresh_failed,
    }


async def _run_probe(cfg: Config, state: _ProbeState) -> dict | None:
    try:
        fresh = await run_db_work(cfg, fetch_fingerprints, cfg)
        stored_tables, stored_objects = await read_fingerprints(cfg.cache_path, cfg.database)
        baseline_missing = not stored_tables and not stored_objects
        diff = diff_fingerprints(stored_tables, stored_objects, fresh)
        freshness = await cache_freshness(cfg)
        refreshed = False
        objects_marked = 0
        if diff.has_changes or baseline_missing or freshness["stale"]:
            snapshot = await warmup_structural_cache(cfg, fingerprints=fresh)
            refreshed = True
            objects_marked = snapshot.get("objects_marked_dirty", 0)
        state.refresh_failed = False
        return {
            "probed": True,
            "refreshed": refreshed,
            "baseline_created": baseline_missing,
            "snapshot_expired": freshness["stale"],
            "objects_marked_dirty": objects_marked,
            **diff.summary(),
        }
    except Exception:  # noqa: BLE001 (cache diagnostics must not fail operations)
        # Neither a failed probe nor a failed snapshot advances captured_at.
        state.refresh_failed = True
        logger.warning("Schema revalidation failed; keeping cache")
        return None
    finally:
        state.last_checked = time.monotonic()


def _track_task(state: _ProbeState, task: asyncio.Task, *, manual: bool = False) -> asyncio.Task:
    state.task = task
    state.manual = manual
    _BACKGROUND_TASKS.add(task)

    def finished(done: asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(done)
        if state.task is done:
            state.task = None
            state.manual = False
        if not done.cancelled():
            done.exception()
        _trim_states()

    task.add_done_callback(finished)
    return task


def _start_probe(cfg: Config, state: _ProbeState, *, detach: bool = False) -> asyncio.Task:
    async def _go():
        if detach:
            from ..recording.context import detach_recording

            detach_recording()
        return await _run_probe(cfg, state)

    return _track_task(state, asyncio.create_task(_go()))


async def _run_manual_refresh(
    cfg: Config,
    state: _ProbeState,
    previous: asyncio.Task | None,
) -> dict:
    try:
        if previous is not None:
            # The old task may still be fetching/writing a snapshot. A manual
            # request reserves the successor task before waiting for it, so
            # other manual/automatic callers cannot start competing work.
            await asyncio.shield(previous)
        result = await warmup_structural_cache(cfg)
        note_refreshed(cfg)
        return result
    except Exception:  # noqa: BLE001 (cache diagnostics must not fail operations)
        state.refresh_failed = True
        raise
    finally:
        state.last_checked = time.monotonic()


async def refresh_now(cfg: Config) -> dict:
    """Join one explicit full refresh, serialized after any in-flight probe.

    Explicit requests fetch a complete snapshot even when fingerprints and
    freshness have not changed. Cancelling a caller only cancels its wait.
    """
    state = _state(cfg)
    pending = state.task if state.task is not None and not state.task.done() else None
    if pending is not None and state.manual:
        return await asyncio.shield(pending)
    task = _track_task(
        state,
        asyncio.create_task(_run_manual_refresh(cfg, state, pending)),
        manual=True,
    )
    return await asyncio.shield(task)


async def revalidate_if_stale(cfg: Config, *, force: bool = False) -> dict | None:
    """Join one existing probe, or start one when due; cancellation is local."""
    state = _state(cfg)
    if state.task is not None and not state.task.done():
        return await asyncio.shield(state.task)
    if not force and not is_due(cfg):
        return None
    return await asyncio.shield(_start_probe(cfg, state))


async def maybe_revalidate(cfg: Config) -> None:
    """Schedule at most one revalidation task, without waiting for DB work."""
    try:
        state = _state(cfg)
        if not cfg.cache_enabled or not is_due(cfg):
            return
        if state.task is None or state.task.done():
            _start_probe(cfg, state, detach=True)
    except Exception:  # noqa: BLE001 (cache diagnostics must not fail operations)
        logger.exception("maybe_revalidate failed")
