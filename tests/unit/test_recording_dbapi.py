"""DB-API proxies record each execution once and forward the driver unchanged."""

import sqlite3
import time
from types import SimpleNamespace

import pytest

from db_semantic_mcp.config import Config
from db_semantic_mcp.infrastructure.backends import oracle
from db_semantic_mcp.infrastructure.backends.oracle_catalog import _fetch
from db_semantic_mcp.infrastructure.backends.sqlserver import SQLServerBackend
from db_semantic_mcp.infrastructure.recording.context import record_tool
from db_semantic_mcp.infrastructure.recording.dbapi import instrument_db_connection
from db_semantic_mcp.infrastructure.recording.schema import initialize_recording_schema
from db_semantic_mcp.infrastructure.recording.writer import reset_recording


@pytest.fixture(autouse=True)
def _isolate():
    reset_recording()
    yield
    reset_recording()


def _cfg(path):
    return SimpleNamespace(execution_recording_enabled=True, cache_path=str(path))


def _db(tmp_path):
    path = tmp_path / "semantic.db"
    with sqlite3.connect(path) as conn:
        initialize_recording_schema(conn)
    return path


class _Cursor:
    def __init__(self, log):
        self.log = log
        self.description = [("name", None, None, None, None, None, True)]
        self.rowcount = 1

    def execute(self, sql, parameters=None):
        self.log.append(("cursor.execute", sql, parameters))
        self._last_sql = sql
        if sql == "boom":
            raise RuntimeError("driver failed")
        return self

    def executemany(self, sql, parameters):
        self.log.append(("cursor.executemany", sql, parameters))
        return self

    def executescript(self, script):
        self.log.append(("cursor.executescript", script))
        return self

    def fetchall(self):
        self.log.append(("fetchall",))
        if str(getattr(self, "_last_sql", "")).startswith("SELECT column_ordinal"):
            return [(1, "id", "int", 0, None, None)]
        return [("kept",)]

    def fetchone(self):
        self.log.append(("fetchone",))
        return ("kept",)

    def close(self):
        self.log.append(("cursor.close",))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class _Connection:
    def __init__(self):
        self.log = []
        self.cursor_obj = _Cursor(self.log)

    def cursor(self):
        self.log.append(("cursor",))
        return self.cursor_obj

    def execute(self, sql, parameters=None):
        self.log.append(("connection.execute", sql, parameters))
        return self.cursor_obj

    def executemany(self, sql, parameters):
        self.log.append(("connection.executemany", sql, parameters))
        return self.cursor_obj

    def executescript(self, script):
        self.log.append(("connection.executescript", script))

    def commit(self):
        self.log.append(("commit",))

    def rollback(self):
        self.log.append(("rollback",))

    def close(self):
        self.log.append(("close",))

    def __enter__(self):
        self.log.append(("enter",))
        return self

    def __exit__(self, *_args):
        self.log.append(("exit",))
        return None


def _settle_writer(timeout=2.0):
    from db_semantic_mcp.infrastructure.recording.writer import get_recorder

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        recorder = get_recorder()
        with recorder._lock:
            if recorder._queued == 0 and recorder._bytes == 0:
                return
        time.sleep(0.005)
    raise AssertionError("writer did not drain")


def _sql_rows(path, expected):
    _settle_writer()
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT source_type, status, sql_text, parameters_json, call_id FROM mcp_execution_log "
            "WHERE record_type = 'sql' ORDER BY sequence_no"
        ).fetchall()
    if len(rows) < expected or any(row[1] == "running" for row in rows):
        raise AssertionError(rows)
    return rows


def test_without_context_the_driver_object_is_unchanged():
    conn = _Connection()
    assert instrument_db_connection(conn) is conn


async def test_proxy_forwards_each_driver_call_once_and_records_three_sql(tmp_path):
    path = _db(tmp_path)
    conn = _Connection()
    long_sql = "SELECT " + ("Q" * 5000)
    groups = [("first", 1), ("second", 2)]

    async with record_tool(_cfg(path), "plan_or_execute_query") as span:
        wrapped = instrument_db_connection(conn)
        assert instrument_db_connection(wrapped) is wrapped
        wrapped.label = "primary"
        assert conn.label == "primary"
        with wrapped as entered:
            assert entered is wrapped
            cursor = wrapped.cursor()
            cursor.execute("SELECT cache_skip", ("a",))
            assert cursor.fetchall() == [("kept",)]
            cursor.executemany("INSERT INTO t VALUES (%s, %s)", groups)
            with pytest.raises(RuntimeError, match="driver failed"):
                cursor.execute("boom")
            wrapped.execute(long_sql, None)
            wrapped.executescript("CREATE TABLE internal(id INT); INSERT INTO internal VALUES (1)")
            wrapped.commit()
            wrapped.rollback()
        wrapped.close()
        span.finish("success", "ok")

    assert conn.label == "primary"
    assert ("cursor.execute", "SELECT cache_skip", ("a",)) in conn.log
    assert conn.log.count(("cursor.execute", "SELECT cache_skip", ("a",))) == 1
    assert ("fetchall",) in conn.log
    assert ("connection.executemany", "INSERT INTO t VALUES (%s, %s)", groups) in conn.log or (
        "cursor.executemany",
        "INSERT INTO t VALUES (%s, %s)",
        groups,
    ) in conn.log
    assert ("connection.execute", long_sql, None) in conn.log
    assert ("connection.executescript", "CREATE TABLE internal(id INT); INSERT INTO internal VALUES (1)") in conn.log
    assert conn.log.count(("commit",)) == 1
    assert conn.log.count(("rollback",)) == 1
    assert conn.log.count(("close",)) == 1
    rows = _sql_rows(path, 4)
    texts = [row[2] for row in rows]
    assert texts == ["SELECT cache_skip", "INSERT INTO t VALUES (%s, %s)", "boom", long_sql]
    assert all(row[0] == "db" and row[4] == span.call_id for row in rows)
    assert rows[1][3].index("first") < rows[1][3].index("second")
    assert "CREATE TABLE internal" not in texts
    assert rows[2][1] == "failed"
    assert rows[0][1] == "success"


async def test_executemany_does_not_consume_a_parameter_iterator(tmp_path):
    path = _db(tmp_path)
    conn = _Connection()

    def groups():
        raise AssertionError("iterator consumed")
        yield ("x",)

    stream = groups()
    async with record_tool(_cfg(path), "plan_or_execute_query") as span:
        wrapped = instrument_db_connection(conn)
        wrapped.executemany("INSERT INTO t VALUES (%s)", stream)
        span.finish("success", "ok")
    assert ("connection.executemany", "INSERT INTO t VALUES (%s)", stream) in conn.log


async def test_borrowed_oracle_catalog_and_result_schema_sql_are_recorded(tmp_path, monkeypatch):
    path = _db(tmp_path)
    conn = _Connection()

    async with record_tool(_cfg(path), "describe_table") as span:
        borrowed = instrument_db_connection(conn)
        _fetch(borrowed, "SELECT owner FROM sys.all_objects WHERE name = :name", {"name": "ORDERS"})
        SQLServerBackend().describe_result_schema(borrowed.cursor(), "SELECT id FROM orders")
        span.finish("success", "ok")

    rows = _sql_rows(path, 2)
    assert [row[2] for row in rows] == [
        "SELECT owner FROM sys.all_objects WHERE name = :name",
        "SELECT column_ordinal, name, system_type_name, is_nullable, error_number, error_message FROM "
        "sys.dm_exec_describe_first_result_set(%s, NULL, 0)",
    ]
    assert {row[0] for row in rows} == {"db"}
    assert {row[4] for row in rows} == {span.call_id}
    assert (
        '"name"'
        in sqlite3.connect(path)
        .execute("SELECT parameters_json FROM mcp_execution_log WHERE sql_text LIKE 'SELECT owner%'")
        .fetchone()[0]
    )


async def test_thick_mode_session_sql_is_recorded_inside_connect(tmp_path, monkeypatch):
    path = _db(tmp_path)
    executed = []

    class Cursor:
        def execute(self, sql, parameters=None):
            executed.append(sql)
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Connection:
        def __init__(self):
            self.autocommit = True
            self.call_timeout = None
            self.outputtypehandler = None
            self.closed = False

        def cursor(self):
            return Cursor()

        def close(self):
            self.closed = True

    class ConnectParams:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        def set(self, **kwargs):
            self.__dict__.update(kwargs)

    driver = SimpleNamespace(
        is_thin_mode=lambda: False,
        init_oracle_client=lambda **_kwargs: None,
        connect=lambda **_kwargs: Connection(),
        enable_thin_mode=lambda: None,
        ConnectParams=ConnectParams,
    )
    monkeypatch.setattr(oracle, "_driver", lambda: driver)
    monkeypatch.setattr(oracle, "_thick_options", None)
    cfg = Config.from_fixed(
        database_type="oracle",
        server="host",
        user="app",
        password="secret",
        oracle={"service_name": "FREEPDB1", "driver_mode": "thick"},
    )
    async with record_tool(_cfg(path), "get_tables") as span:
        oracle.OracleBackend().connect(cfg)
        span.finish("success", "ok")
    assert executed == [oracle._SESSION_FORMATS]
    row = _sql_rows(path, 1)[0]
    assert (row[2], row[0], row[4]) == (oracle._SESSION_FORMATS, "db", span.call_id)
