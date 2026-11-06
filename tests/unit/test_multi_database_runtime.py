"""Database routing checks run without a SQL Server or metadata cache."""

import asyncio
import importlib
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from mcp.types import Tool
from pydantic import AnyUrl

from db_semantic_mcp.config import Config, configure, get_config, reset_config
from db_semantic_mcp.policy.models import PolicyProfile
from db_semantic_mcp.server import database_context as databases
from db_semantic_mcp.server.app import get_context, reset_context

server = importlib.import_module("db_semantic_mcp.server.app")
_REAL_INITIALIZE = databases._initialize


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    cfg = Config.from_fixed(
        server="host",
        database="ERP",
        databases=["ERP", "CRM"],
        profile_version=3,
        cache_path=str(tmp_path / "cache.db"),
        cache_enabled=False,
        metrics_enabled=False,
    )
    policy = PolicyProfile(
        profile_name="readonly", scope={"allowed_databases": ["ERP", "CRM"], "database_scopes": {"ERP": {}, "CRM": {}}}
    )
    cfg._policy_snapshot = policy
    configure(cfg)
    reset_context()
    root = get_context()
    monkeypatch.setattr(server, "_TOOL_REGISTRY", {})
    monkeypatch.setattr(server, "_OUTPUT_VALIDATORS", {})
    initialize = AsyncMock(side_effect=lambda ctx: ctx)
    monkeypatch.setattr(databases, "_initialize", initialize)
    yield root, initialize
    await databases.shutdown_database_contexts()
    reset_context()
    reset_config()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [None, "", 123, "Forbidden", "erp"])
async def test_invalid_target_precedes_cache_or_database(runtime, target):
    _, initialize = runtime
    with pytest.raises((ValueError, PermissionError)):
        async with databases.bind_database(target):
            pytest.fail("invalid database entered request")
    initialize.assert_not_called()


@pytest.mark.asyncio
async def test_parallel_binding_singleflight_and_snapshot(runtime):
    root, initialize = runtime
    barrier = asyncio.Barrier(6)

    async def request(name):
        async with databases.bind_database(name):
            ctx = get_context()
            before = (get_config().database, get_config().cache_path)
            await barrier.wait()
            await asyncio.sleep(0)
            assert get_context() is ctx
            assert (get_config().database, get_config().cache_path) == before
            assert ctx.policy.current_policy().scope.allowed_databases == ["ERP", "CRM"]
            return before

    results = await asyncio.gather(*(request(name) for name in ["ERP", "CRM"] * 3))
    assert len({path for _, path in results}) == 2
    assert initialize.call_count == 1  # ERP already initialized at startup
    assert root.cfg.database == "ERP"
    assert len(databases._STATES[asyncio.get_running_loop()]) <= 2


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_initialization(runtime, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def initialize(ctx):
        started.set()
        await release.wait()
        return ctx

    monkeypatch.setattr(databases, "_initialize", initialize)

    async def request():
        async with databases.bind_database("CRM"):
            return get_config().database

    first = asyncio.create_task(request())
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(request())
    release.set()
    assert await second == "CRM"


@pytest.mark.asyncio
async def test_policy_reload_is_root_owned_and_denies_before_cached_context(runtime, monkeypatch):
    root, initialize = runtime
    async with databases.bind_database("CRM"):
        old = get_context()
        later = root.policy.current_policy().model_copy(deep=True)
        later.scope.allowed_databases = ["ERP"]
        monkeypatch.setattr(root.policy, "reload", lambda: setattr(root.policy, "_policy", later))
        async with databases.bind_database("ERP", reload=True):
            assert get_config().database == "ERP"
        assert get_context() is old
        assert old.policy.current_policy().scope.allowed_databases == ["ERP", "CRM"]
    calls = initialize.call_count
    with pytest.raises(PermissionError):
        async with databases.bind_database("CRM"):
            pytest.fail("revoked cached database")
    assert initialize.call_count == calls


@pytest.mark.asyncio
async def test_tool_schema_list_and_recording_order(runtime, monkeypatch):
    from db_semantic_mcp.infrastructure.recording import context as recording
    from db_semantic_mcp.server.tools import databases as tool

    entered = []

    @asynccontextmanager
    async def record(cfg, name):
        entered.append(cfg.database)

        class Span:
            finished = True

        yield Span()

    monkeypatch.setattr(recording, "record_tool", record)
    monkeypatch.setattr(server.revalidation, "maybe_revalidate", AsyncMock())

    async def handler(args):
        return {"database": get_config().database}

    server.register_tool(Tool(name="sample", inputSchema={"type": "object"}), handler)
    tool.register()
    assert "database" in server._TOOL_REGISTRY["sample"][0].inputSchema["required"]
    result = await server._dispatch_tool("sample", {"database": "CRM"})
    assert not result.isError
    assert entered == ["CRM"]
    entered.clear()
    listing = await server._dispatch_tool("list_databases", {})
    assert listing.structuredContent == {"databases": ["ERP", "CRM"], "selection_required": True}
    assert not entered
    server.revalidation.maybe_revalidate.assert_awaited_once()


@pytest.mark.asyncio
async def test_resource_selection_and_encoding(runtime, monkeypatch):
    from db_semantic_mcp.server.resources import schema

    monkeypatch.setattr(schema, "_read_resource_bound", AsyncMock(return_value="{}"))
    with pytest.raises(ValueError, match="database is required"):
        await schema.read_resource(AnyUrl("semantic://summary/database"))
    await schema.read_resource(AnyUrl("semantic://summary/database?database=CRM"))
    with pytest.raises(ValueError):
        await schema.read_resource(AnyUrl("semantic://summary/database?database=CRM%26database%3DERP"))
    resources = await schema.list_resources()
    assert {str(item.uri) for item in resources} == {
        "semantic://summary/database?database=ERP",
        "semantic://summary/database?database=CRM",
    }


@pytest.mark.asyncio
async def test_v2_explicit_wrong_database_is_rejected(runtime):
    root, initialize = runtime
    root.cfg.profile_version = 2
    root.cfg.databases = []
    async with databases.bind_database("ERP"):
        assert get_config().database == "ERP"
    with pytest.raises(ValueError, match="not configured"):
        async with databases.bind_database("CRM"):
            pytest.fail("v2 routed to another database")
    initialize.assert_not_called()


@pytest.mark.asyncio
async def test_database_workers_share_process_limit(runtime):
    import threading
    import time

    from db_semantic_mcp.infrastructure.workers import run_db_work, shutdown_db_workers

    root, _ = runtime
    root.cfg.db_worker_limit = 2
    lock = threading.Lock()
    active = peak = 0
    observed = []

    def work():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(active, peak)
        time.sleep(0.01)
        observed.append(get_config().database)
        with lock:
            active -= 1

    async def request(name):
        async with databases.bind_database(name):
            await run_db_work(get_config(), work)

    try:
        await asyncio.gather(*(request(name) for name in ["ERP", "CRM"] * 4))
        assert peak == 2
        assert sorted(observed) == ["CRM"] * 4 + ["ERP"] * 4
    finally:
        await shutdown_db_workers()


@pytest.mark.asyncio
async def test_bootstrap_background_uses_snapshot_and_reload_stops_it(runtime, monkeypatch):
    root, _ = runtime
    started = asyncio.Event()
    observations = []

    async def fill(cfg, stop):
        observations.append((get_config() is cfg, get_context().cfg is cfg, cfg.database))
        before = get_context().policy.current_policy().scope.allowed_databases
        started.set()
        await stop.wait()
        observations.append(get_context().policy.current_policy().scope.allowed_databases == before)

    task = await databases.start_database_background(root, fill)
    await started.wait()
    assert task is databases._BACKGROUNDS[asyncio.get_running_loop()][(None, "ERP")][1]
    later = root.policy.current_policy().model_copy(deep=True)
    later.scope.allowed_databases = ["ERP"]
    monkeypatch.setattr(root.policy, "reload", lambda: setattr(root.policy, "_policy", later))
    async with databases.bind_database("ERP", reload=True):
        pass
    await task
    assert observations == [(True, True, "ERP"), True]
    assert root.cfg._policy_snapshot.scope.allowed_databases == ["ERP", "CRM"]


@pytest.mark.asyncio
async def test_shutdown_drains_managed_bootstrap_background(runtime):
    root, _ = runtime
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def fill(cfg, stop):
        started.set()
        await stop.wait()
        stopped.set()

    task = await databases.start_database_background(root, fill)
    await started.wait()
    await databases.shutdown_database_contexts()
    assert stopped.is_set() and task.done()
    assert asyncio.get_running_loop() not in databases._BACKGROUNDS


@pytest.mark.asyncio
async def test_lazy_warmup_enqueues_selected_database_before_fill(runtime, monkeypatch):
    from db_semantic_mcp.infrastructure.cache import semantic
    from db_semantic_mcp.server import source_context

    root, _ = runtime
    clone = root.cfg.model_copy(deep=True)
    clone.database = "CRM"
    clone.cache_enabled = True
    ctx = server.Context(clone)
    ctx.policy.load()
    observed = []

    async def initialized(selected):
        assert selected is ctx and get_context() is ctx and get_config() is ctx.cfg
        observed.append("warmup")
        return ctx

    async def enqueue(path, database):
        assert get_context() is ctx and get_config() is ctx.cfg
        assert (path, database) == (ctx.cfg.cache_path, "CRM")
        observed.append("enqueue")

    async def background(selected):
        assert selected is ctx
        observed.append("fill")

    monkeypatch.setattr(source_context, "_initialize", initialized)
    monkeypatch.setattr(semantic, "enqueue_all_tables", enqueue)
    monkeypatch.setattr(databases, "start_database_background", background)
    assert await _REAL_INITIALIZE(ctx) is ctx
    assert observed == ["warmup", "enqueue", "fill"]
    assert get_config() is root.cfg


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, ValueError, PermissionError])
async def test_resource_lazy_initialization_errors_do_not_expose_password(runtime, monkeypatch, error_type):
    from db_semantic_mcp.server.resources import schema

    root, _ = runtime
    secret = "resource-driver-secret"
    root.cfg.password = secret
    monkeypatch.setattr(databases, "_initialize", AsyncMock(side_effect=error_type("driver login failed " + secret)))
    with pytest.raises((ValueError, PermissionError)) as caught:
        await schema.read_resource(AnyUrl("semantic://summary/database?database=CRM"))
    assert secret not in str(caught.value)
    if error_type is RuntimeError:
        assert str(caught.value) == "Database unavailable; check connection and permissions"
    else:
        assert "***" in str(caught.value)


@pytest.mark.asyncio
async def test_recording_and_metrics_follow_parallel_database_binding(runtime, monkeypatch):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from db_semantic_mcp.infrastructure.recording import writer
    from db_semantic_mcp.infrastructure.recording.context import record_sql

    root, _ = runtime
    root.cfg.execution_recording_enabled = True
    root.cfg.metrics_enabled = True
    events = []
    recorder = SimpleNamespace(_clock=lambda: datetime.now(UTC), max_bytes=1048576, try_emit=events.append)
    monkeypatch.setattr(writer, "get_recorder", lambda: recorder)
    metrics = []

    async def metric(path, *args, **kwargs):
        metrics.append((get_config().database, path))

    monkeypatch.setattr(server.metrics_service, "record_metric", metric)
    monkeypatch.setattr(server.revalidation, "maybe_revalidate", AsyncMock())
    barrier = asyncio.Barrier(2)

    async def handler(args):
        name = get_config().database
        await barrier.wait()
        with record_sql("db", "SELECT value FROM dbo.Table", (name,)):
            pass
        return [{"database": name}]

    server.register_tool(Tool(name="get_tables", inputSchema={"type": "object"}), handler)
    results = await asyncio.gather(
        *(server._dispatch_tool("get_tables", {"database": name}) for name in ["ERP", "CRM"])
    )
    assert all(not result.isError for result in results)
    assert [result.structuredContent["source"]["database"] for result in results] == ["ERP", "CRM"]
    metric_paths = dict(metrics)
    assert len(set(metric_paths.values())) == 2
    sql_events = [event for event in events if event.record_type == "sql" and event.phase == "end"]
    assert len(sql_events) == 2
    for event in sql_events:
        selected = "ERP" if "ERP" in event.parameters_json else "CRM"
        assert event.sink_path == metric_paths[selected]
    assert len({event.call_id for event in events}) == 2
