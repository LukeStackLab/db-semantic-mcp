"""Local DB-API proxies. They never patch sqlite3, aiosqlite, oracledb, or pymssql."""

from .context import record_sql, recording_active, suppress_recording


def instrument_db_connection(conn):
    """Return a recording proxy while a tool call is active, otherwise the original object."""
    if conn is None or isinstance(conn, _RecordingConnection) or not recording_active():
        return conn
    return _RecordingConnection(conn)


class _RecordingCursor:
    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        setattr(self._inner, name, value)

    def __enter__(self):
        entered = self._inner.__enter__()
        return self if entered is self._inner else _RecordingCursor(entered)

    def __exit__(self, exc_type, exc, tb):
        return self._inner.__exit__(exc_type, exc, tb)

    def _finish(self, result):
        if result is None or result is self._inner:
            return self if result is self._inner else None
        if isinstance(result, _RecordingCursor):
            return result
        return _RecordingCursor(result)

    def execute(self, sql, parameters=None):
        with record_sql("db", sql, parameters):
            result = self._inner.execute(sql) if parameters is None else self._inner.execute(sql, parameters)
        return self._finish(result)

    def executemany(self, sql, parameters):
        with record_sql("db", sql, parameters):
            result = self._inner.executemany(sql, parameters)
        return self._finish(result)

    def executescript(self, script):
        with suppress_recording():
            result = self._inner.executescript(script)
        return self._finish(result)


class _RecordingConnection:
    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        setattr(self._inner, name, value)

    def __enter__(self):
        entered = self._inner.__enter__()
        return self if entered is self._inner else _RecordingConnection(entered)

    def __exit__(self, exc_type, exc, tb):
        return self._inner.__exit__(exc_type, exc, tb)

    def cursor(self, *args, **kwargs):
        return _RecordingCursor(self._inner.cursor(*args, **kwargs))

    def execute(self, sql, parameters=None):
        with record_sql("db", sql, parameters):
            result = self._inner.execute(sql) if parameters is None else self._inner.execute(sql, parameters)
        if result is None:
            return None
        return _RecordingCursor(result)

    def executemany(self, sql, parameters):
        with record_sql("db", sql, parameters):
            result = self._inner.executemany(sql, parameters)
        if result is None:
            return None
        return _RecordingCursor(result)

    def executescript(self, script):
        with suppress_recording():
            return self._inner.executescript(script)
