"""Bound synchronous DB work without tying its lifetime to the awaiting task."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from time import perf_counter
from typing import Callable, TypeVar
from weakref import WeakKeyDictionary

from ..config import Config

T = TypeVar("T")


class _Workers:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.executor = ThreadPoolExecutor(max_workers=limit, thread_name_prefix="mssql")
        self.slots = asyncio.Semaphore(limit)
        self.closing = False
        self.shutdown_task: asyncio.Task | None = None


# A server runs one event loop with one fixed Config. Per-loop state also keeps
# test/event-loop lifetimes from sharing an asyncio semaphore.
_WORKERS: WeakKeyDictionary = WeakKeyDictionary()
_TIMINGS: ContextVar[dict | None] = ContextVar("db_worker_timings", default=None)


def take_worker_timings() -> dict | None:
    """Read and clear timings in the current tool task; never contains SQL/data."""
    value = _TIMINGS.get()
    _TIMINGS.set(None)
    return value


async def run_db_work(cfg: Config, fn: Callable[..., T], *args, **kwargs) -> T:
    """Run an entire synchronous connection lifecycle in a bounded worker.

    Cancelling a caller stops its wait, not the driver. A slot remains occupied
    until the actual worker finishes, including cursor/connection cleanup.
    """
    loop = asyncio.get_running_loop()
    limit = cfg.db_worker_limit
    workers = _WORKERS.get(loop)
    if workers is None:
        workers = _WORKERS[loop] = _Workers(limit)
    elif workers.limit != limit:
        raise ValueError("DB worker limit cannot change within a running server")
    if workers.closing:
        raise RuntimeError("DB workers are shutting down")

    waiting = perf_counter()
    await workers.slots.acquire()
    if workers.closing:
        workers.slots.release()
        raise RuntimeError("DB workers are shutting down")
    wait_ms = (perf_counter() - waiting) * 1000
    timing = {"worker_wait_ms": wait_ms, "db_duration_ms": None}

    def run() -> T:
        started = perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            timing["db_duration_ms"] = (perf_counter() - started) * 1000

    try:
        pending = workers.executor.submit(run)
    except BaseException:
        workers.slots.release()
        raise

    def finished(_future) -> None:
        if not loop.is_closed():
            loop.call_soon_threadsafe(workers.slots.release)

    pending.add_done_callback(finished)
    future = asyncio.wrap_future(pending, loop=loop)
    # If the waiter is cancelled, still consume a later worker exception.
    future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
    try:
        return await asyncio.shield(future)
    finally:
        _TIMINGS.set(timing)


async def shutdown_db_workers() -> None:
    """Drain sync lifecycles and stop their threads before the server loop exits.

    Call after stopping background producers. Shutdown runs outside the event
    loop and never cancels a queued/running synchronous DB operation.
    """
    loop = asyncio.get_running_loop()
    workers = _WORKERS.get(loop)
    if workers is None:
        return
    if workers.shutdown_task is None:
        workers.closing = True
        workers.shutdown_task = asyncio.create_task(asyncio.to_thread(
            workers.executor.shutdown, wait=True, cancel_futures=False,
        ))

        def finished(_task: asyncio.Task) -> None:
            if _WORKERS.get(loop) is workers:
                del _WORKERS[loop]

        workers.shutdown_task.add_done_callback(finished)
    await asyncio.shield(workers.shutdown_task)
