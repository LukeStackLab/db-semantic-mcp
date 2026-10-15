"""Hermetic checks for the synchronous DB worker's concurrency/lifetime."""

import asyncio
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from db_semantic_mcp.infrastructure.workers import run_db_work, take_worker_timings


@pytest.mark.asyncio
async def test_worker_keeps_event_loop_responsive():
    cfg = SimpleNamespace(db_worker_limit=1)
    entered, release = Event(), Event()

    def blocking():
        entered.set()
        assert release.wait(2)
        return "closed"

    task = asyncio.create_task(run_db_work(cfg, blocking))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert entered.is_set()
        ticks = 0
        for _ in range(5):
            await asyncio.sleep(0)
            ticks += 1
        assert ticks == 5
        assert not task.done()
    finally:
        release.set()
    assert await task == "closed"


@pytest.mark.asyncio
async def test_worker_limit_bounds_active_lifecycles():
    cfg = SimpleNamespace(db_worker_limit=2)
    release = Event()
    lock = Lock()
    active = peak = 0

    def work():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            assert release.wait(2)
        finally:
            with lock:
                active -= 1

    tasks = [asyncio.create_task(run_db_work(cfg, work)) for _ in range(6)]
    try:
        for _ in range(100):
            if peak == 2:
                break
            await asyncio.sleep(0.001)
        assert peak == 2
        assert active == 2
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert peak == 2


@pytest.mark.asyncio
async def test_cancelled_waiter_retains_slot_until_worker_cleanup():
    cfg = SimpleNamespace(db_worker_limit=1)
    entered, release, closed, second_entered = Event(), Event(), Event(), Event()

    def first():
        entered.set()
        try:
            assert release.wait(2)
        finally:
            closed.set()

    first_task = asyncio.create_task(run_db_work(cfg, first))
    second_task = None
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert entered.is_set()
        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task
        second_task = asyncio.create_task(run_db_work(cfg, second_entered.set))
        await asyncio.sleep(0.02)
        assert not second_entered.is_set()
        assert not closed.is_set()
    finally:
        release.set()
        if second_task is not None:
            await second_task
        else:
            await asyncio.gather(first_task, return_exceptions=True)
    assert closed.is_set()
    assert second_entered.is_set()


@pytest.mark.asyncio
async def test_worker_timings_do_not_include_inputs():
    cfg = SimpleNamespace(db_worker_limit=1)
    assert await run_db_work(cfg, lambda value: value, "private SQL") == "private SQL"
    timing = take_worker_timings()
    assert set(timing) == {"worker_wait_ms", "db_duration_ms"}
    assert all(value >= 0 for value in timing.values())
    assert take_worker_timings() is None


@pytest.mark.asyncio
async def test_shutdown_drains_lifecycles_and_stops_threads():
    from db_semantic_mcp.infrastructure import workers

    cfg = SimpleNamespace(db_worker_limit=1)
    entered, release, closed = Event(), Event(), Event()

    def blocking():
        entered.set()
        try:
            assert release.wait(2)
        finally:
            closed.set()

    task = asyncio.create_task(run_db_work(cfg, blocking))
    shutting_down = None
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert entered.is_set()
        pool = workers._WORKERS[asyncio.get_running_loop()]
        shutting_down = asyncio.create_task(workers.shutdown_db_workers())
        await asyncio.sleep(0.01)
        assert not shutting_down.done()
        assert not closed.is_set()
        with pytest.raises(RuntimeError, match="shutting down"):
            await run_db_work(cfg, lambda: None)
    finally:
        release.set()
        await task
        if shutting_down:
            await shutting_down
    assert closed.is_set()
    assert all(not thread.is_alive() for thread in pool.executor._threads)
    assert asyncio.get_running_loop() not in workers._WORKERS
