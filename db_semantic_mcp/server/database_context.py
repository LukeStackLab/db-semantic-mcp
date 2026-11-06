"""Request-local database routing; selection is checked before cache access."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import Context
from weakref import WeakKeyDictionary

from ..config import _request_config
from .source_context import _request_context

_STATES = WeakKeyDictionary()
_INITIALIZATIONS = WeakKeyDictionary()
_BACKGROUNDS = WeakKeyDictionary()


def configured_databases(cfg):
    return list(getattr(cfg, "databases", None) or [cfg.database])


def available_databases(cfg, policy):
    names = configured_databases(cfg)
    allowed = policy.scope.allowed_databases
    if getattr(cfg, "profile_version", 2) == 3:
        return [name for name in names if name in allowed and name in policy.scope.database_scopes]
    return [name for name in names if not allowed or name in allowed]


def _root_context():
    # Explicitly bypass request context when refreshing the process policy.
    from . import app as app_module

    token = _request_context.set(None)
    config_token = _request_config.set(None)
    try:
        return app_module.get_context()
    finally:
        _request_config.reset(config_token)
        _request_context.reset(token)


async def _initialize(ctx):
    from ..infrastructure.cache.semantic import enqueue_all_tables
    from .source_context import _initialize as initialize

    token = _request_context.set(ctx)
    config_token = _request_config.set(ctx.cfg)
    try:
        result = await initialize(ctx)
        if ctx.cfg.cache_enabled:
            await enqueue_all_tables(ctx.cfg.cache_path, ctx.cfg.database)
        root = _root_context()
        current = root.policy.current_policy()
        if (
            ctx.cfg.cache_enabled
            and ctx.cfg.database in available_databases(root.cfg, current)
            and current.model_dump() == ctx.policy.current_policy().model_dump()
        ):
            await start_database_background(ctx)
        return result
    finally:
        _request_config.reset(config_token)
        _request_context.reset(token)


async def start_database_background(ctx, fill_loop=None):
    """Register startup/lazy fill under one bounded lifecycle and snapshot."""
    from ..infrastructure.background import background_fill_loop
    from .app import Context as DatabaseContext

    snapshot = ctx.policy.current_policy().model_copy(deep=True)
    clone = ctx.cfg.model_copy(deep=True)
    clone._policy_snapshot = snapshot
    bound = DatabaseContext(clone)
    bound.policy.load()
    backgrounds = _BACKGROUNDS.setdefault(asyncio.get_running_loop(), {})
    key = (clone.connection_name, clone.database)
    previous = backgrounds.get(key)
    if previous:
        previous[0].set()
        await asyncio.gather(previous[1], return_exceptions=True)
    stop = asyncio.Event()
    runner = fill_loop or background_fill_loop

    async def fill():
        token = _request_context.set(bound)
        config_token = _request_config.set(clone)
        try:
            await runner(clone, stop)
        finally:
            _request_config.reset(config_token)
            _request_context.reset(token)

    task = asyncio.create_task(fill(), context=Context())
    backgrounds[key] = (stop, task)
    return task


@asynccontextmanager
async def bind_database(database=None, reload=False):
    from .app import Context as DatabaseContext

    root = _root_context()
    cfg = root.cfg
    names = configured_databases(cfg)
    if database is None:
        if len(names) > 1:
            raise ValueError("database is required; call list_databases and select a database")
        database = names[0]
    if not isinstance(database, str) or not database:
        raise ValueError("database must be a non-empty string")
    if database not in names:
        raise ValueError("Database is not configured")
    if getattr(cfg, "profile_version", 2) != 3:
        yield root
        return
    if reload:
        previous_policy = root.policy.current_policy().model_dump()
        root.policy.reload()
        if previous_policy != root.policy.current_policy().model_dump():
            for stop, _task in _BACKGROUNDS.get(asyncio.get_running_loop(), {}).values():
                stop.set()
    snapshot = root.policy.current_policy().model_copy(deep=True)
    if database not in available_databases(cfg, snapshot):
        raise PermissionError("Database is denied by policy scope")
    # Preserve Oracle's existing source initialization and resolution path.
    if cfg.database_type == "oracle":
        yield root
        return
    clone = cfg.model_copy(deep=True)
    clone.database = database
    clone._policy_snapshot = snapshot
    clone.scope_cache(snapshot)
    if clone.cache_path == cfg.cache_path:
        ctx = DatabaseContext(clone)
        ctx.policy.load()
        background = _BACKGROUNDS.get(asyncio.get_running_loop(), {}).get((clone.connection_name, database))
        if clone.cache_enabled and background and (background[0].is_set() or background[1].done()):
            await start_database_background(ctx)
        token = _request_context.set(ctx)
        config_token = _request_config.set(clone)
        try:
            yield ctx
        finally:
            _request_config.reset(config_token)
            _request_context.reset(token)
        return
    states = _STATES.setdefault(asyncio.get_running_loop(), {})
    for stale in [key for key in states if key[0] != id(root)]:
        states.pop(stale)
    key = (id(root), database)
    entry = states.get(key)
    if entry is None or entry[0] != clone.cache_path:
        ctx = DatabaseContext(clone)
        ctx.policy.load()
        # Empty context prevents recording/request inheritance in shielded tasks.
        task = asyncio.create_task(_initialize(ctx), context=Context())
        pending = _INITIALIZATIONS.setdefault(asyncio.get_running_loop(), set())
        pending.add(task)
        entry = (clone.cache_path, task)
        states[key] = entry

        def finished(done):
            pending.discard(done)
            if done.cancelled() or done.exception() is not None:
                if states.get(key) is entry:
                    states.pop(key, None)

        task.add_done_callback(finished)
    ctx = await asyncio.shield(entry[1])
    token = _request_context.set(ctx)
    config_token = _request_config.set(ctx.cfg)
    try:
        yield ctx
    finally:
        _request_config.reset(config_token)
        _request_context.reset(token)


async def shutdown_database_contexts():
    loop = asyncio.get_running_loop()
    tasks = list(_INITIALIZATIONS.get(loop, ()))
    if tasks:
        await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)
    backgrounds = _BACKGROUNDS.pop(loop, {})
    for stop, _task in backgrounds.values():
        stop.set()
    if backgrounds:
        await asyncio.gather(*(task for _stop, task in backgrounds.values()), return_exceptions=True)
    _STATES.pop(loop, None)
