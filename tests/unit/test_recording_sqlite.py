"""Cache SQL is recorded only inside an active tool call, on a separate data file."""

import sqlite3
import time
from types import SimpleNamespace

import aiosqlite
import pytest

from db_semantic_mcp.infrastructure.cache.probe import read_fingerprints
from db_semantic_mcp.infrastructure.connection import open_connection
from db_semantic_mcp.infrastructure.recording.context import record_tool
from db_semantic_mcp.infrastructure.recording.schema import initialize_recording_schema
from db_semantic_mcp.infrastructure.recording.sqlite import recording_connect
from db_semantic_mcp.infrastructure.recording.writer import reset_recording
from db_semantic_mcp.services.metadata_service import list_tables
from db_semantic_mcp.services.metrics_service import query_top_tools, record_metric


@pytest.fixture(autouse=True)
def _isolate():
    reset_recording()
    yield
    reset_recording()


def _cfg(path):
    return SimpleNamespace(execution_recording_enabled=True, cache_path=str(path))


def _sink(tmp_path):
    path = tmp_path / "semantic.db"
    with sqlite3.connect(path) as conn:
        initialize_recording_schema(conn)
    return path


def _sql_rows(path, expected):
    deadline = time.monotonic() + 2
    rows = []
    while time.monotonic() < deadline:
        with sqlite3.connect(path) as db:
            rows = db.execute(
                "SELECT source_type, status, sql_text, call_id FROM mcp_execution_log "
                "WHERE record_type = 'sql' ORDER BY sequence_no"
            ).fetchall()
        if len(rows) >= expected and all(row[1] != "running" for row in rows):
            return rows
        time.sleep(0.02)
    raise AssertionError(rows)


def test_without_context_recording_connect_returns_the_native_factory(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(
        "db_semantic_mcp.infrastructure.recording.sqlite.aiosqlite.connect",
        lambda *_args, **_kwargs: sentinel,
    )
    assert recording_connect("cache.db") is sentinel


async def test_await_and_async_with_record_each_statement_once(tmp_path):
    sink = _sink(tmp_path)
    data = tmp_path / "data.db"
    async with aiosqlite.connect(data) as db:
        await db.execute("CREATE TABLE t (id INTEGER)")
        await db.commit()

    groups = [(1,), (2,)]
    async with record_tool(_cfg(sink), "get_tables") as span:
        async with recording_connect(data) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT id FROM t")
            seen = [row["id"] async for row in cur]
            assert seen == []
            await db.executemany("INSERT INTO t VALUES (?)", groups)
            await db.executescript("CREATE TABLE hidden(id INT); INSERT INTO hidden VALUES (1)")
        opened = await recording_connect(data)
        try:
            await opened.execute("SELECT 2")
        finally:
            await opened.close()
        span.finish("success", "ok")

    rows = _sql_rows(sink, 3)
    texts = [row[2] for row in rows]
    assert texts == ["SELECT id FROM t", "INSERT INTO t VALUES (?)", "SELECT 2"]
    assert {row[0] for row in rows} == {"cache"}
    assert {row[3] for row in rows} == {span.call_id}
    assert all(row[1] == "success" for row in rows)
    async with aiosqlite.connect(data) as db:
        count = await db.execute_fetchall("SELECT COUNT(*) FROM t")
    assert count[0][0] == 2


async def test_list_tables_and_read_fingerprints_use_the_cache_source(tmp_path):
    sink = _sink(tmp_path)
    data = tmp_path / "data.db"
    async with aiosqlite.connect(data) as db:
        await db.executescript(
            """
            CREATE TABLE sc_tables (
                database_name TEXT, schema_name TEXT, table_name TEXT, structural_hash TEXT
            );
            INSERT INTO sc_tables VALUES ('app', 'dbo', 'orders', '');
            CREATE TABLE sc_fingerprints (
                database_name TEXT, kind TEXT, schema_name TEXT, name TEXT,
                object_type TEXT, fingerprint TEXT
            );
            INSERT INTO sc_fingerprints VALUES ('app', 'table', 'dbo', 'orders', '', 'abc');
            """
        )
        await db.commit()

    async with record_tool(_cfg(sink), "get_tables") as span:
        tables = await list_tables(str(data), "app", policy=None)
        stored = await read_fingerprints(str(data), "app")
        span.finish("success", "ok")

    assert tables == [{"schema_name": "dbo", "table_name": "orders"}]
    assert stored[0][("dbo", "orders")] == "abc"
    rows = _sql_rows(sink, 2)
    assert rows[0][2].startswith("SELECT schema_name,table_name FROM sc_tables")
    assert "sc_fingerprints" in rows[1][2]
    assert {row[0] for row in rows} == {"cache"}
    assert {row[3] for row in rows} == {span.call_id}


async def test_metric_writes_stay_raw_and_metric_reads_are_recorded(tmp_path):
    sink = _sink(tmp_path)
    data = tmp_path / "data.db"
    async with aiosqlite.connect(data) as db:
        await db.execute(
            """
            CREATE TABLE tool_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_name TEXT NOT NULL,
                response_bytes INTEGER NOT NULL,
                array_length INTEGER,
                fields_returned INTEGER,
                route_type TEXT,
                was_direct_execute INTEGER,
                bundle_used INTEGER,
                next_action TEXT,
                elapsed_ms REAL,
                worker_wait_ms REAL,
                db_duration_ms REAL,
                fetched_rows INTEGER,
                cache_hit INTEGER,
                error_category TEXT,
                recorded_at TEXT NOT NULL
            )
            """
        )
        await db.commit()

    async with record_tool(_cfg(sink), "get_tables") as span:
        await record_metric(str(data), "get_tables", response_bytes=12, elapsed_ms=3)
        found = await query_top_tools(str(data), limit=10)
        span.finish("success", "ok")

    assert found[0]["tool_name"] == "get_tables"
    rows = _sql_rows(sink, 2)
    assert all("tool_metrics" in row[2] and row[2].lstrip().upper().startswith("SELECT") for row in rows)
    assert {row[0] for row in rows} == {"cache"}
    assert {row[3] for row in rows} == {span.call_id}


async def test_open_connection_instruments_only_inside_a_call(tmp_path, monkeypatch):
    raw_calls = []

    class Cursor:
        def execute(self, sql, parameters=None):
            raw_calls.append(sql)
            return self

        def close(self):
            return None

    class Conn:
        def cursor(self):
            return Cursor()

        def close(self):
            raw_calls.append("close")

    class Backend:
        def connect(self, cfg):
            return Conn()

    monkeypatch.setattr("db_semantic_mcp.infrastructure.backends.backend_for", lambda cfg: Backend())
    with open_connection(SimpleNamespace()) as conn:
        assert type(conn).__name__ == "Conn"
    sink = _sink(tmp_path)
    async with record_tool(_cfg(sink), "describe_table") as span:
        with open_connection(SimpleNamespace()) as conn:
            assert type(conn).__name__ != "Conn"
            conn.cursor().execute("SELECT live")
        span.finish("success", "ok")
    rows = _sql_rows(sink, 1)
    assert rows[0][2] == "SELECT live"
    assert rows[0][0] == "db" and rows[0][3] == span.call_id
    assert raw_calls.count("close") == 2
