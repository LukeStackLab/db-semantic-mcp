import asyncio
from unittest.mock import AsyncMock

import pytest
from mcp.types import Tool
from pydantic import AnyUrl

from db_semantic_mcp.config import Config, configure, get_config, reset_config
from db_semantic_mcp.infrastructure.workers import run_db_work, shutdown_db_workers
from db_semantic_mcp.policy.models import PolicyProfile
from db_semantic_mcp.server import source_context as sources
from db_semantic_mcp.server.app import Context, get_context, reset_context


@pytest.fixture
def base(tmp_path, monkeypatch):
    cfg = Config.from_fixed(
        database_type="oracle",
        server="host",
        oracle={"service_name": "test"},
        cache_path=str(tmp_path / "local.db"),
        sources={"one": {"db_link": "ONE"}, "two": {"db_link": "TWO"}},
        cache_enabled=False,
        metrics_enabled=False,
    )
    configure(cfg)
    reset_context()
    ctx = Context(cfg)
    ctx.policy._policy = PolicyProfile(profile_name="readonly", scope={"sources": {"one": {}, "two": {}}})
    calls = []

    def resolve(base, source):
        calls.append(source)
        return base.cfg.for_source(
            source, route_identity={"host": source, "policy": base.policy.current_policy().model_dump()}
        )

    monkeypatch.setattr(sources, "_resolve_source", resolve)
    yield ctx, calls
    reset_context()
    reset_config()


@pytest.mark.asyncio
async def test_local_two_remote_contexts_and_workers_are_isolated(base):
    ctx, calls = base

    async def task(source):
        async with sources.bind_source(ctx, source):
            await asyncio.sleep(0)
            expected = get_context().cfg.cache_path
            assert get_config().source_id == source
            actual = await run_db_work(get_config(), lambda: (get_config().source_id, get_context().cfg.cache_path))
            assert actual == (source, expected)
            return expected

    paths = await asyncio.gather(*(task(s) for s in ["local", "one", "two"] * 4))
    assert len(set(paths)) == 3 and ctx.cfg.source_id == "local"
    assert get_config() is ctx.cfg
    await shutdown_db_workers()


@pytest.mark.asyncio
async def test_singleflight_failure_is_retryable_and_cancellation_does_not_cancel_init(base, monkeypatch):
    ctx, _ = base
    ctx.cfg.cache_enabled = True
    started = asyncio.Event()
    finish = asyncio.Event()
    attempts = []

    async def warmup(cfg):
        attempts.append(cfg.source_id)
        started.set()
        await finish.wait()
        if len(attempts) == 1:
            raise RuntimeError("unavailable")

    monkeypatch.setattr(sources, "warmup_structural_cache", warmup)
    first = asyncio.create_task(sources.source_context(ctx, "one"))
    await started.wait()
    second = asyncio.create_task(sources.source_context(ctx, "one"))
    await asyncio.sleep(0.01)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    finish.set()
    with pytest.raises(RuntimeError):
        await second
    assert attempts == ["one"]
    local = await sources.source_context(ctx, "local")
    assert local is not ctx and local.cfg.cache_path == ctx.cfg.cache_path
    assert (await sources.source_context(ctx, "one")).cfg.source_id == "one"
    assert attempts == ["one", "one"]
    await shutdown_db_workers()


@pytest.mark.asyncio
async def test_every_request_reproves_route_and_retarget_isolates_cache(base, monkeypatch):
    ctx, _ = base
    state = {"principal": "A"}

    def resolve(base, source):
        if state["principal"] == "denied":
            raise PermissionError("revoked")
        return base.cfg.for_source(source, route_identity=state.copy())

    monkeypatch.setattr(sources, "_resolve_source", resolve)
    a = await sources.source_context(ctx, "one")
    state["principal"] = "B"
    b = await sources.source_context(ctx, "one")
    assert a.cfg.cache_path != b.cfg.cache_path
    state["principal"] = "denied"
    with pytest.raises(PermissionError):
        await sources.source_context(ctx, "one")
    with pytest.raises(ValueError, match="Unknown"):
        await sources.source_context(ctx, "missing")
    assert len(sources._STATES[asyncio.get_running_loop()]) == 1
    await shutdown_db_workers()


@pytest.mark.asyncio
async def test_tool_source_schema_default_and_original_query(base, monkeypatch):
    # import package exposes module normally; avoid mutable registry leaks.
    import importlib

    mod = importlib.import_module("db_semantic_mcp.server.app")
    monkeypatch.setattr(mod, "_TOOL_REGISTRY", {})
    monkeypatch.setattr(mod, "_OUTPUT_VALIDATORS", {})
    seen = []

    async def handler(args):
        seen.append((get_config().source_id, args.copy()))
        return []

    mod.register_tool(Tool(name="get_tables", inputSchema={"type": "object"}), handler)
    assert mod._TOOL_REGISTRY["get_tables"][0].inputSchema["properties"]["source"]["default"] == "local"
    await mod._dispatch_tool("get_tables", {})
    assert seen[0][0] == "local"

    async def query(args):
        seen.append((get_config().source_id, args["sql"]))
        return {"kind": "plan_or_execute_query", "data": {}}

    mod.register_tool(Tool(name="plan_or_execute_query", inputSchema={"type": "object"}), query)
    sql = "SELECT o.ID FROM APP.ORDERS@ONE o"
    result = await mod._dispatch_tool("plan_or_execute_query", {"sql": sql})
    assert not result.isError and seen[-1] == ("local", sql)
    count = len(seen)
    assert (await mod._dispatch_tool("plan_or_execute_query", {"sql": sql, "source": "one"})).isError
    assert len(seen) == count


@pytest.mark.asyncio
async def test_resource_source_and_encoded_quoted_names(base, monkeypatch):
    from db_semantic_mcp.server.resources import schema

    ctx, _ = base
    monkeypatch.setattr(schema, "get_context", get_context)
    describe = AsyncMock(return_value={"column_count": 1})
    monkeypatch.setattr(schema.metadata_service, "describe_table", describe)
    # Use the real binding; the base context here comes from configured globals.
    result = await schema.read_resource(
        AnyUrl("semantic://schema/tables/%22Case.Owner%22.%22Odd.Table%2FName%22?source=one")
    )
    args = describe.call_args
    assert args.args[2:] == ("Case.Owner", "Odd.Table/Name")
    assert args.kwargs["policy"]._cfg.source_id == "one"
    assert '"id":"one"' in result
    assert get_config().source_id == "local"
    await shutdown_db_workers()


@pytest.mark.asyncio
async def test_cancelled_source_worker_keeps_slot_until_actual_cleanup(base):
    import threading

    ctx, _ = base
    ctx.cfg.db_worker_limit = 1
    started = threading.Event()
    finish = threading.Event()
    cleaned = threading.Event()
    async with sources.bind_source(ctx, "local") as request:

        def slow():
            assert get_context() is request
            started.set()
            try:
                finish.wait(5)
            finally:
                cleaned.set()

        first = asyncio.create_task(run_db_work(ctx.cfg, slow))
        await asyncio.to_thread(started.wait, 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(run_db_work(ctx.cfg, lambda: cleaned.is_set()))
        await asyncio.sleep(0.02)
        assert not second.done()
        finish.set()
        assert await second
    await shutdown_db_workers()


def test_retargeted_revalidation_state_remains_bounded(base):
    from db_semantic_mcp.infrastructure.cache import revalidation

    ctx, _ = base
    revalidation.reset_state()
    for number in range(300):
        cfg = ctx.cfg.for_source("one", route_identity={"principal": number})
        revalidation.note_refreshed(cfg)
    assert len(revalidation._STATES) <= 256
    revalidation.reset_state()


@pytest.mark.asyncio
async def test_local_request_and_worker_keep_snapshot_during_concurrent_reload(base, monkeypatch):
    import importlib

    from db_semantic_mcp.server.tools import policy as policy_tool
    from db_semantic_mcp.services import policy_service

    mod = importlib.import_module("db_semantic_mcp.server.app")
    ctx, _ = base
    ctx.policy.current_policy().scope.sources["one"].allowed_schemas = ["ERP"]
    later = ctx.policy.current_policy().model_copy(deep=True)
    later.scope.sources["one"].allowed_schemas = ["ERP", "SECRET"]
    loads = []

    def load(cfg):
        loads.append(cfg.source_id)
        return later.model_copy(deep=True)

    monkeypatch.setattr(policy_service, "load_active_policy", load)
    monkeypatch.setattr(mod, "_ctx", ctx)
    monkeypatch.setattr(mod, "_TOOL_REGISTRY", {})
    monkeypatch.setattr(mod, "_OUTPUT_VALIDATORS", {})
    started, finish = asyncio.Event(), asyncio.Event()
    observations = []

    def scope():
        return list(get_context().policy.current_policy().scope.sources["one"].allowed_schemas)

    async def handler(args):
        observations.append(scope())
        started.set()
        await finish.wait()
        observations.append(scope())
        observations.append(await run_db_work(get_config(), scope))
        return []

    mod.register_tool(Tool(name="get_tables", inputSchema={"type": "object"}), handler)
    mod.register_tool(Tool(name="get_execution_policy", inputSchema={"type": "object"}), policy_tool._get_policy)
    request = asyncio.create_task(mod._dispatch_tool("get_tables", {}))
    await started.wait()
    reloaded = await mod._dispatch_tool("get_execution_policy", {"reload": True})
    finish.set()
    assert not (await request).isError and not reloaded.isError
    assert observations == [["ERP"]] * 3
    assert reloaded.structuredContent["scope"]["sources"]["one"]["allowed_schemas"] == ["ERP", "SECRET"]
    assert loads == ["local"]
    async with sources.bind_source(ctx, "local"):
        assert scope() == ["ERP", "SECRET"]
    assert ctx.cfg._policy_snapshot is None
    await shutdown_db_workers()
