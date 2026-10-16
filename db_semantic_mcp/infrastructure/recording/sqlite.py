"""aiosqlite factory that records cache SQL only inside an active tool call."""

import aiosqlite

from .context import record_sql, recording_active, suppress_recording


def recording_connect(database: str, **kwargs):
    """Match ``aiosqlite.connect``: the result is awaitable and an async context manager."""
    native = aiosqlite.connect(database, **kwargs)
    if not recording_active():
        return native
    return _Connect(native)


class _AioConnection:
    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        setattr(self._inner, name, value)

    async def __aenter__(self):
        entered = await self._inner.__aenter__()
        return self if entered is self._inner else _AioConnection(entered)

    async def __aexit__(self, exc_type, exc, tb):
        return await self._inner.__aexit__(exc_type, exc, tb)

    async def execute(self, sql, parameters=None):
        with record_sql("cache", sql, parameters):
            if parameters is None:
                return await self._inner.execute(sql)
            return await self._inner.execute(sql, parameters)

    async def executemany(self, sql, parameters):
        with record_sql("cache", sql, parameters):
            return await self._inner.executemany(sql, parameters)

    async def executescript(self, script):
        with suppress_recording():
            return await self._inner.executescript(script)


class _Connect:
    def __init__(self, native):
        self._native = native
        self._proxy: _AioConnection | None = None

    def __await__(self):
        async def _open():
            return _AioConnection(await self._native)

        return _open().__await__()

    async def __aenter__(self):
        self._proxy = await self
        return self._proxy

    async def __aexit__(self, exc_type, exc, tb):
        return await self._proxy.__aexit__(exc_type, exc, tb)
