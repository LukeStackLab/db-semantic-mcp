"""End-to-end recording against a temp SQLite file and a blocking fake driver."""

import asyncio
import json
import sqlite3
import threading

import aiosqlite
import pytest

from db_semantic_mcp.infrastructure.recording.dbapi import instrument_db_connection
from db_semantic_mcp.infrastructure.recording.sqlite import recording_connect
from db_semantic_mcp.infrastructure.recording.writer import reset_recording
from db_semantic_mcp.server.app import _OUTPUT_VALIDATORS, _TOOL_REGISTRY, _dispatch_tool, _list_tools, reset_context
from db_semantic_mcp.server.tools import register_all
from tests.unit.test_execution_recording import _cfg, _sink


@pytest.fixture(autouse=True)
def _isolate():
    reset_recording()
    reset_context()
    _TOOL_REGISTRY.clear()
    _OUTPUT_VALIDATORS.clear()
    yield
    reset_recording()
    reset_context()
    _TOOL_REGISTRY.clear()
    _OUTPUT_VALIDATORS.clear()


def _install(monkeypatch, cfg):
    monkeypatch.setattr("db_semantic_mcp.config.get_config", lambda: cfg)
    monkeypatch.setattr("db_semantic_mcp.server.app.get_config", lambda: cfg)
    monkeypatch.setattr(
        "db_semantic_mcp.server.app.revalidation.maybe_revalidate",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    register_all()


async def _noop_revalidate(*_args, **_kwargs):
    return None


def _settle_writer(timeout=2.0):
    import time

    from db_semantic_mcp.infrastructure.recording.writer import get_recorder

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        recorder = get_recorder()
        with recorder._lock:
            if recorder._queued == 0 and recorder._bytes == 0:
                return
        time.sleep(0.005)
    raise AssertionError("writer did not drain")


def _read(path, record_type):
    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT status, summary, sql_text, source_type FROM mcp_execution_log "
            "WHERE record_type = ? ORDER BY sequence_no",
            (record_type,),
        ).fetchall()


async def test_running_row_is_visible_before_the_tool_finishes(tmp_path, monkeypatch):
    sink = _sink(tmp_path)
    data = tmp_path / "data.db"
    async with aiosqlite.connect(data) as db:
        await db.execute("CREATE TABLE t (id INTEGER)")
        await db.commit()
    cfg = _cfg(sink)
    _install(monkeypatch, cfg)
    monkeypatch.setattr("db_semantic_mcp.server.app.revalidation.maybe_revalidate", _noop_revalidate)
    release = threading.Event()
    entered = threading.Event()

    class Cursor:
        def execute(self, sql, parameters=None):
            return self

    class Conn:
        def cursor(self):
            return Cursor()

    tool, _handler = _TOOL_REGISTRY["get_tables"]

    async def handler(_args):
        entered.set()
        await asyncio.to_thread(release.wait, 2)
        async with recording_connect(data) as db:
            await db.execute("SELECT id FROM t")
        instrument_db_connection(Conn()).cursor().execute("SELECT id FROM orders", {"id": 7})
        return [{"schema_name": "dbo", "table_name": "orders"}]

    _TOOL_REGISTRY["get_tables"] = (tool, handler)
    task = asyncio.create_task(_dispatch_tool("get_tables", {}))
    for _ in range(200):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set()
    _settle_writer()
    seen = _read(sink, "tool")
    assert seen and seen[0][0] == "running"
    release.set()
    result = await task
    assert result.isError is False
    assert "execution" not in result.structuredContent
    _settle_writer()
    tools = _read(sink, "tool")
    sqls = _read(sink, "sql")
    assert tools[0][0] == "success" and tools[0][1] == "找到 1 張表"
    assert {row[3] for row in sqls} == {"cache", "db"}
    assert all(row[0] == "success" for row in sqls)
    assert any("orders" in row[2] for row in sqls)


async def test_locked_cache_still_returns_the_original_tool_result(tmp_path, monkeypatch):
    sink = _sink(tmp_path)
    cfg = _cfg(sink)
    _install(monkeypatch, cfg)
    monkeypatch.setattr("db_semantic_mcp.server.app.revalidation.maybe_revalidate", _noop_revalidate)
    tool, _handler = _TOOL_REGISTRY["describe_table"]

    async def handler(_args):
        return {"table": "orders", "column_count": 0, "columns": []}

    _TOOL_REGISTRY["describe_table"] = (tool, handler)
    object.__setattr__(cfg, "execution_recording_enabled", False)
    baseline = await _dispatch_tool("describe_table", {"schema": "dbo", "table": "orders"})
    object.__setattr__(cfg, "execution_recording_enabled", True)
    holder = sqlite3.connect(sink)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        locked = await _dispatch_tool("describe_table", {"schema": "dbo", "table": "orders"})
    finally:
        holder.rollback()
        holder.close()
    assert locked.isError == baseline.isError
    assert locked.content[0].text == baseline.content[0].text
    assert locked.structuredContent == baseline.structuredContent


async def test_recording_flag_does_not_change_tool_descriptors_or_payload(tmp_path, monkeypatch):
    sink = _sink(tmp_path)
    cfg = _cfg(sink)
    _install(monkeypatch, cfg)
    monkeypatch.setattr("db_semantic_mcp.server.app.revalidation.maybe_revalidate", _noop_revalidate)
    tool, _handler = _TOOL_REGISTRY["get_tables"]

    async def handler(_args):
        return [{"schema_name": "dbo", "table_name": "orders"}]

    _TOOL_REGISTRY["get_tables"] = (tool, handler)
    object.__setattr__(cfg, "execution_recording_enabled", False)
    off_tools = json.dumps([item.model_dump(mode="json") for item in await _list_tools()], sort_keys=True)
    off = await _dispatch_tool("get_tables", {})
    object.__setattr__(cfg, "execution_recording_enabled", True)
    on_tools = json.dumps([item.model_dump(mode="json") for item in await _list_tools()], sort_keys=True)
    on = await _dispatch_tool("get_tables", {})
    assert on_tools == off_tools
    assert on.content[0].text == off.content[0].text
    assert set(on.structuredContent) == set(off.structuredContent)
