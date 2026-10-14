"""Single-flight schema probes with stale-while-revalidate snapshot freshness."""
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ...config import Config
from ..workers import run_db_work
from .probe import diff_fingerprints, fetch_fingerprints, read_fingerprints
from .semantic import mark_objects_dirty
from .structural import read_schema_version, warmup_structural_cache

logger = logging.getLogger(__name__)


@dataclass
class _ProbeState:
    last_checked: float = 0.0
    task: Optional[asyncio.Task] = None
    refresh_failed: bool = False
    manual: bool = False


_STATES: dict[tuple[str, str], _ProbeState] = {}
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _state(cfg: Config) -> _ProbeState:
    key = (cfg.cache_path, cfg.mssql_database)
    if key not in _STATES:
        _STATES[key] = _ProbeState()
    return _STATES[key]


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


async def cache_freshness(cfg: Config) -> dict:
    """Report persisted snapshot age, including DB-offline stale responses."""
    version = await read_schema_version(cfg.cache_path, cfg.mssql_database)
    captured_at = version.get("captured_at") if version else None
    age = None
    if captured_at:
        try:
            captured = datetime.fromisoformat(captured_at)
            if captured.tzinfo is None:
                captured = captured.replace(tzinfo=timezone.utc)
            age = max(0.0, (datetime.now(timezone.utc) - captured).total_seconds())
        except (TypeError, ValueError):
            pass
    stale = age is None or age >= cfg.schema_cache_ttl_s
    state = _state(cfg)
    return {
        "captured_at": captured_at,
        "age_seconds": round(age, 3) if age is not None else None,
        "stale": stale,
        "refreshing": state.task is not None and not state.task.done(),
        "refresh_failed": state.refresh_failed,
    }


async def _run_probe(cfg: Config, state: _ProbeState) -> Optional[dict]:
    try:
        fresh = await run_db_work(cfg, fetch_fingerprints, cfg)
        stored_tables, stored_objects = await read_fingerprints(cfg.cache_path, cfg.mssql_database)
        baseline_missing = not stored_tables and not stored_objects
        diff = diff_fingerprints(stored_tables, stored_objects, fresh)
        freshness = await cache_freshness(cfg)
        refreshed = False
        objects_marked = 0
        if diff.has_changes or baseline_missing or freshness["stale"]:
            await warmup_structural_cache(cfg, fingerprints=fresh)
            refreshed = True
            if not baseline_missing:
                keys = {(s, n) for (s, n, _t) in diff.changed_objects}
                for tkey in (*diff.changed_tables, *diff.removed_tables):
                    keys.update(fresh.table_dependents.get(tkey, []))
                objects_marked = await mark_objects_dirty(cfg.cache_path, cfg.mssql_database, keys)
        state.refresh_failed = False
        return {
            "probed": True, "refreshed": refreshed,
            "baseline_created": baseline_missing,
            "snapshot_expired": freshness["stale"],
            "objects_marked_dirty": objects_marked,
            **diff.summary(),
        }
    except Exception:
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

    task.add_done_callback(finished)
    return task


def _start_probe(cfg: Config, state: _ProbeState) -> asyncio.Task:
    return _track_task(state, asyncio.create_task(_run_probe(cfg, state)))


async def _run_manual_refresh(
    cfg: Config, state: _ProbeState, previous: Optional[asyncio.Task],
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
    except Exception:
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
        state, asyncio.create_task(_run_manual_refresh(cfg, state, pending)), manual=True,
    )
    return await asyncio.shield(task)


async def revalidate_if_stale(cfg: Config, *, force: bool = False) -> Optional[dict]:
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
            _start_probe(cfg, state)
    except Exception:
        logger.exception("maybe_revalidate failed")
