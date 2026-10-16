"""Request-scoped source selection; original SQL always uses the base connection."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from weakref import WeakKeyDictionary

from ..config import _request_config
from ..infrastructure.backends import backend_for
from ..infrastructure.backends.oracle_resolution import OracleResolver
from ..infrastructure.cache.store import init_store
from ..infrastructure.cache.structural import read_schema_version, warmup_structural_cache
from ..infrastructure.connection import open_connection
from ..infrastructure.workers import run_db_work

_request_context = ContextVar("request_context", default=None)
# One latest context per configured source. Retargets cannot retain unbounded
# context caches. In-flight requests hold their own immutable references.
_STATES = WeakKeyDictionary()
_INITIALIZATIONS = WeakKeyDictionary()
EXPLORATION_TOOLS = frozenset(
    {
        "get_tables",
        "describe_table",
        "describe_object",
        "get_table_relationships",
        "get_dependency_chain",
        "detect_lookup_tables",
        "find_join_path",
        "discover_relevant_tables",
        "refresh_schema_cache",
        "get_execution_policy",
    }
)


def source_metadata(cfg):
    result = {
        "database_type": cfg.database_type,
        "id": cfg.source_id,
        "database": cfg.database,
        "capabilities": {
            key: value
            for key, value in backend_for(cfg).capabilities(cfg).items()
            if key
            in {
                "driver_mode",
                "native_timestamp_max_precision",
                "native_timestamp_timezone",
                "remote_sources",
            }
        },
    }
    if cfg.connection_name:
        result["connection"] = cfg.connection_name
    if cfg.source_dblink:
        result["db_link"] = cfg._resolved_dblink or cfg.source_dblink
        result["query_hint"] = (
            'Use native "OWNER"."OBJECT"@' + result["db_link"] + "; SQL runs through the base connection"
        )
    return result


def _resolve_source(base, source):
    with open_connection(base.cfg) as conn:
        return OracleResolver(base.cfg, conn, base.policy.current_policy()).source_config(source)


async def _initialize(ctx):
    token = _request_context.set(ctx)
    config_token = _request_config.set(ctx.cfg)
    try:
        await init_store(ctx.cfg.cache_path)
        if ctx.cfg.cache_enabled and await read_schema_version(ctx.cfg.cache_path, ctx.cfg.database) is None:
            await warmup_structural_cache(ctx.cfg)
        return ctx
    finally:
        _request_config.reset(config_token)
        _request_context.reset(token)


async def source_context(base, source="local"):
    from .app import Context

    if not isinstance(source, str):
        raise ValueError("source must be a configured source id")
    if source == "local":
        if base.cfg.database_type != "oracle":
            return base
        cfg = base.cfg.model_copy(deep=True)
        cfg._policy_snapshot = base.policy.current_policy().model_copy(deep=True)
        ctx = Context(cfg)
        ctx.policy.load()
        if cfg.cache_path == base.cfg.cache_path:
            return ctx
    elif source not in base.cfg.sources:
        raise ValueError("Unknown source")
    if source != "local":
        # Prove route/principal and current policy before cache lookup.
        policy_snapshot = base.policy.current_policy().model_copy(deep=True)
        proof_context = SimpleNamespace(cfg=base.cfg, policy=SimpleNamespace(current_policy=lambda: policy_snapshot))
        cfg = await run_db_work(base.cfg, _resolve_source, proof_context, source)
        cfg._policy_snapshot = policy_snapshot
        cfg.scope_cache(policy_snapshot)
    states = _STATES.setdefault(asyncio.get_running_loop(), {})
    key = (id(base), source)
    entry = states.get(key)
    if entry is None or entry[0] != cfg.cache_path:
        ctx = Context(cfg)
        ctx.policy.load()
        task = asyncio.create_task(_initialize(ctx))
        initializing = _INITIALIZATIONS.setdefault(asyncio.get_running_loop(), set())
        initializing.add(task)
        entry = (cfg.cache_path, task)
        states[key] = entry

        def finished(done):
            initializing.discard(done)
            if done.cancelled() or done.exception() is not None:
                if states.get(key) is entry:
                    states.pop(key, None)

        task.add_done_callback(finished)
    return await asyncio.shield(entry[1])


@asynccontextmanager
async def bind_source(base, source="local"):
    ctx = await source_context(base, source)
    token = _request_context.set(ctx)
    config_token = _request_config.set(ctx.cfg)
    try:
        yield ctx
    finally:
        _request_config.reset(config_token)
        _request_context.reset(token)


async def shutdown_source_contexts():
    """Drain shielded cache initialization before closing DB workers/event loop."""
    tasks = list(_INITIALIZATIONS.get(asyncio.get_running_loop(), ()))
    if tasks:
        await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)
