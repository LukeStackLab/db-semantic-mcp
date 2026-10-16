"""Tool spans stay outside the original MCP result, including cancel and background work."""

import asyncio
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
from mcp.types import CallToolResult

from db_semantic_mcp.config import Config
from db_semantic_mcp.infrastructure.background import background_fill_loop
from db_semantic_mcp.infrastructure.cache import revalidation
from db_semantic_mcp.infrastructure.cache.store import SCHEMA_DDL, SCHEMA_TABLES
from db_semantic_mcp.infrastructure.cache.structural import StructuralSnapshot, _replace_snapshot_rows
from db_semantic_mcp.infrastructure.recording.context import (
    record_tool,
    recording_active,
    summarize_tool,
    tool_outcome,
)
from db_semantic_mcp.infrastructure.recording.dbapi import instrument_db_connection
from db_semantic_mcp.infrastructure.recording.schema import initialize_recording_schema
from db_semantic_mcp.infrastructure.recording.writer import reset_recording, stop_recording
from db_semantic_mcp.infrastructure.workers import run_db_work
from db_semantic_mcp.policy.models import PolicyProfile
from db_semantic_mcp.server import source_context
from db_semantic_mcp.server.app import _OUTPUT_VALIDATORS, _TOOL_REGISTRY, _dispatch_tool, reset_context
from db_semantic_mcp.server.tools import register_all
from db_semantic_mcp.services.metrics_service import record_metric
from db_semantic_mcp.services.query_service import QueryService


@pytest.fixture(autouse=True)
def _isolate():
    reset_recording()
    revalidation.reset_state()
    reset_context()
    _TOOL_REGISTRY.clear()
    _OUTPUT_VALIDATORS.clear()
    yield
    reset_recording()
    revalidation.reset_state()
    reset_context()


def _sink(tmp_path):
    path = tmp_path / "semantic.db"
    with sqlite3.connect(path) as conn:
        initialize_recording_schema(conn)
    return path


def _cfg(path, **extra):
    cfg = Config.from_fixed(
        server="host",
        database="appdb",
        user="app",
        password="secret",
        cache_enabled=False,
        metrics_enabled=False,
        cache_path=str(path),
        **extra,
    )
    object.__setattr__(cfg, "execution_recording_enabled", True)
    return cfg


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


def _rows(path, record_type, expected):
    _settle_writer()
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT tool_name, step_name, action_type, status, summary, call_id, source_type, sql_text "
            "FROM mcp_execution_log WHERE record_type = ? ORDER BY sequence_no",
            (record_type,),
        ).fetchall()
    if len(rows) < expected or any(row[3] == "running" for row in rows):
        raise AssertionError(rows)
    return rows


def _stub(name):
    async def handler(_args, tool_name=name):
        if tool_name == "get_tables":
            return [{"schema_name": "dbo", "table_name": "orders"}]
        if tool_name == "plan_or_execute_query":
            return {"kind": tool_name, "data": {"fetched_row_count": 20}}
        if tool_name == "discover_relevant_tables":
            return {"kind": tool_name, "data": {}}
        if tool_name in {"get_table_relationships", "get_dependency_chain", "detect_lookup_tables"}:
            return []
        if tool_name == "describe_table":
            return {"table": "orders", "column_count": 1, "columns": [{"name": "id"}]}
        if tool_name == "find_join_path":
            return {"found": False, "ambiguous": False, "candidates": []}
        if tool_name == "describe_object":
            return {"name": "orders", "type": "VIEW"}
        return {"ok": True}

    return handler


def _payload(result: CallToolResult):
    return result.isError, result.content[0].text, result.structuredContent


async def test_registered_tools_keep_the_original_response_and_one_tool_row(tmp_path, monkeypatch):
    sink = _sink(tmp_path)
    cfg = _cfg(sink)
    object.__setattr__(cfg, "execution_recording_enabled", False)
    monkeypatch.setattr("db_semantic_mcp.config.get_config", lambda: cfg)
    monkeypatch.setattr("db_semantic_mcp.server.app.get_config", lambda: cfg)
    monkeypatch.setattr("db_semantic_mcp.server.app.revalidation.maybe_revalidate", AsyncMock())
    register_all()
    for name in list(_TOOL_REGISTRY):
        tool, _handler = _TOOL_REGISTRY[name]
        _TOOL_REGISTRY[name] = (tool, _stub(name))

    disabled = {}
    for name in list(_TOOL_REGISTRY):
        disabled[name] = _payload(await _dispatch_tool(name, {}))
    with sqlite3.connect(sink) as db:
        assert db.execute("SELECT COUNT(*) FROM mcp_execution_log").fetchone()[0] == 0

    object.__setattr__(cfg, "execution_recording_enabled", True)
    enabled = {}
    for name in list(_TOOL_REGISTRY):
        enabled[name] = _payload(await _dispatch_tool(name, {}))
    assert enabled == disabled

    rows = _rows(sink, "tool", len(_TOOL_REGISTRY))
    by_name = {row[0]: row for row in rows}
    assert set(by_name) == set(_TOOL_REGISTRY)
    assert by_name["get_tables"][1:5] == ("get_tables", "探索", "success", "找到 1 張表")
    assert by_name["plan_or_execute_query"][2:5] == ("驗證", "success", "成功，回傳 20 筆")
    assert by_name["refresh_schema_cache"][2] is None
    assert all(len(row[4]) <= 200 for row in rows)


async def test_rejections_record_a_short_summary_without_changing_the_payload(tmp_path, monkeypatch):
    sink = _sink(tmp_path)
    cfg = _cfg(sink)
    monkeypatch.setattr("db_semantic_mcp.config.get_config", lambda: cfg)
    monkeypatch.setattr("db_semantic_mcp.server.app.get_config", lambda: cfg)
    monkeypatch.setattr("db_semantic_mcp.server.app.revalidation.maybe_revalidate", AsyncMock())
    register_all()
    tool, _handler = _TOOL_REGISTRY["describe_table"]
    _TOOL_REGISTRY["describe_table"] = (tool, AsyncMock(return_value={"allowed": False, "error": "policy"}))

    unknown = await _dispatch_tool("missing_tool", {})
    source = await _dispatch_tool("plan_or_execute_query", {"sql": "SELECT 1", "source": "remote"})
    denied = await _dispatch_tool("describe_table", {"schema": "dbo", "table": "orders"})
    assert unknown.isError and source.isError and denied.isError
    assert unknown.structuredContent["error"].startswith("Unknown tool")
    assert "source" in source.structuredContent["error"]
    assert denied.structuredContent["allowed"] is False

    rows = _rows(sink, "tool", 3)
    summaries = {row[0]: row[4] for row in rows}
    assert summaries["missing_tool"] == "失敗：unknown_tool"
    assert summaries["plan_or_execute_query"] == "失敗：source_rejected"
    assert summaries["describe_table"] == "失敗：policy_denied"


async def test_sql_stays_on_the_entry_cache_when_source_rebinds_the_path(tmp_path, monkeypatch):
    sink = _sink(tmp_path)
    other = tmp_path / "other.db"
    other.write_bytes(b"")
    cfg = _cfg(sink, database_type="oracle", oracle={"service_name": "FREEPDB1"})
    monkeypatch.setattr("db_semantic_mcp.config.get_config", lambda: cfg)
    monkeypatch.setattr("db_semantic_mcp.server.app.get_config", lambda: cfg)
    monkeypatch.setattr("db_semantic_mcp.server.app.revalidation.maybe_revalidate", AsyncMock())

    class Cursor:
        def execute(self, sql, parameters=None):
            return self

    class Conn:
        def cursor(self):
            return Cursor()

    register_all()
    tool, _handler = _TOOL_REGISTRY["get_tables"]

    async def handler(_args):
        instrument_db_connection(Conn()).cursor().execute("SELECT owner FROM all_tables")
        return [{"schema_name": "APP", "table_name": "ORDERS"}]

    _TOOL_REGISTRY["get_tables"] = (tool, handler)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def bind(base, source="local"):
        cfg.cache_path = str(other)
        try:
            yield base
        finally:
            cfg.cache_path = str(sink)

    monkeypatch.setattr(source_context, "bind_source", bind)
    result = await _dispatch_tool("get_tables", {})
    assert result.isError is False
    rows = _rows(sink, "sql", 1)
    assert rows[0][6:] == ("db", "SELECT owner FROM all_tables")
    tools = _rows(sink, "tool", 1)
    assert len(tools) == 1 and tools[0][0] == "get_tables"
    assert other.stat().st_size == 0


def test_summary_is_capped_and_unknown_writes_stay_unknown():
    long_error = "E" * 500
    summary = summarize_tool("describe_table", {"error": long_error, "failure_category": "database"})
    assert summary.startswith("失敗：database") and len(summary) <= 200
    payload = {
        "executed": False,
        "failure_category": "write_outcome_unknown",
        "transaction_state": "unknown",
        "automatic_retry_allowed": False,
        "next_action": "verify_transaction",
    }
    status, text = tool_outcome("plan_or_execute_query", payload)
    assert status == "unknown" and text == "交易結果未知"
    assert payload["failure_category"] == "write_outcome_unknown"
    assert payload["transaction_state"] == "unknown"


async def test_cancelled_caller_leaves_the_worker_sql_result(tmp_path):
    sink = _sink(tmp_path)
    cfg = _cfg(sink)
    object.__setattr__(cfg, "db_worker_limit", 1)
    entered, release, done = threading.Event(), threading.Event(), threading.Event()

    def work():
        class Cursor:
            def execute(self, sql, parameters=None):
                entered.set()
                assert release.wait(2)
                return self

            def close(self):
                return None

        class Conn:
            def cursor(self):
                return Cursor()

            def close(self):
                return None

        try:
            instrument_db_connection(Conn()).cursor().execute("UPDATE orders SET qty = %s", (1,))
        finally:
            done.set()

    async def body():
        async with record_tool(cfg, "plan_or_execute_query") as span:
            await run_db_work(cfg, work)
            return span.call_id

    task = asyncio.create_task(body())
    for _ in range(200):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert done.wait(2)
    tool = _rows(sink, "tool", 1)[0]
    sql = _rows(sink, "sql", 1)[0]
    assert tool[3:5] == ("cancelled", "已取消")
    assert sql[3] == "success" and sql[7] == "UPDATE orders SET qty = %s"
    assert tool[5] == sql[5]


async def test_background_and_startup_do_not_inherit_a_call(tmp_path, monkeypatch):
    sink = _sink(tmp_path)
    cfg = _cfg(sink)
    flags = []
    stop = asyncio.Event()

    async def pending(*_args, **_kwargs):
        flags.append(("background", recording_active()))
        stop.set()
        return []

    monkeypatch.setattr(
        "db_semantic_mcp.infrastructure.background.list_pending_table_analyses",
        pending,
    )
    async with record_tool(cfg, "get_tables"):
        task = asyncio.create_task(background_fill_loop(cfg, stop))
        await asyncio.wait_for(stop.wait(), 2)
    await task
    assert flags == [("background", False)]

    async def probe(_cfg_obj, _state):
        flags.append(("inline", recording_active()))
        return {"probed": True}

    monkeypatch.setattr(revalidation, "_run_probe", probe)
    probe_cfg = SimpleNamespace(
        cache_path=str(sink),
        database="appdb",
        cache_enabled=True,
        probe_interval_s=0,
        schema_cache_ttl_s=0,
    )
    async with record_tool(cfg, "get_tables"):
        await revalidation.revalidate_if_stale(probe_cfg, force=True)
        await revalidation.maybe_revalidate(probe_cfg)
        for _ in range(50):
            if ("inline", False) in flags:
                break
            await asyncio.sleep(0.01)
    assert ("inline", True) in flags and ("inline", False) in flags

    seen = []

    async def warmup(_cfg_obj):
        seen.append(recording_active())
        return {"structural_hash": "fresh"}

    from db_semantic_mcp import main as main_mod

    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "host")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "appdb")
    monkeypatch.setenv("SEMANTIC_MCP_USER", "app")
    monkeypatch.setenv("SEMANTIC_MCP_PASSWORD", "secret")
    monkeypatch.setenv("SEMANTIC_MCP_CACHE_PATH", str(sink))
    monkeypatch.setenv("SEMANTIC_MCP_STARTUP_MODE", "full")
    from db_semantic_mcp.config import reset_config

    reset_config()
    with (
        patch.object(main_mod, "init_store", AsyncMock()),
        patch.object(main_mod, "read_schema_version", AsyncMock(return_value=None)),
        patch.object(main_mod, "warmup_structural_cache", warmup),
        patch.object(main_mod, "enqueue_all_tables", AsyncMock()),
        patch.object(main_mod, "background_fill_loop", MagicMock(return_value="loop")),
        patch.object(main_mod, "register_all"),
        patch.object(main_mod, "register_prompts"),
        patch.object(main_mod, "get_context"),
        patch("db_semantic_mcp.main.asyncio.create_task", return_value=MagicMock()),
    ):
        async with record_tool(cfg, "get_tables"):
            await main_mod._startup()
    assert seen == [False]


async def test_on_demand_source_init_keeps_the_call_and_metrics_insert_is_raw(tmp_path, monkeypatch):
    sink = _sink(tmp_path)
    data = tmp_path / "metrics.db"
    cfg = _cfg(sink)
    flags = []

    async def init_store(_path):
        flags.append(recording_active())

    monkeypatch.setattr(source_context, "init_store", init_store)
    monkeypatch.setattr(source_context, "read_schema_version", AsyncMock(return_value={"captured_at": "x"}))
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
    async with record_tool(cfg, "describe_table"):
        await source_context._initialize(SimpleNamespace(cfg=cfg))
        await record_metric(str(data), "describe_table", response_bytes=8)
    assert flags == [True]
    await asyncio.sleep(0.2)
    with sqlite3.connect(sink) as db:
        sql = db.execute(
            "SELECT sql_text FROM mcp_execution_log WHERE record_type = 'sql' AND sql_text LIKE '%tool_metrics%'"
        ).fetchall()
    assert sql == []


async def test_cache_refresh_does_not_delete_execution_rows(tmp_path):
    path = tmp_path / "cache.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA_DDL)
        initialize_recording_schema(conn)
        conn.execute(
            "INSERT INTO mcp_execution_log (id, call_id, sequence_no, record_type, tool_name, step_name, "
            "started_at, status, summary) VALUES ('1','c',1,'tool','get_tables','get_tables',?, 'success','完成')",
            ("2026-10-16T00:00:00.000000+08:00",),
        )
        conn.commit()
    assert "mcp_execution_log" not in SCHEMA_TABLES
    async with aiosqlite.connect(path) as db:
        await _replace_snapshot_rows(
            db,
            "appdb",
            StructuralSnapshot([], [], [], [], [], [], []),
            {},
        )
        await db.commit()
        count = await db.execute_fetchall("SELECT COUNT(*) FROM mcp_execution_log")
    assert count[0][0] == 1


def test_stop_recording_does_not_wait(monkeypatch):
    joined = []

    def join(self, timeout=None):
        joined.append(self.name)

    monkeypatch.setattr(threading.Thread, "join", join)
    from db_semantic_mcp.infrastructure.recording.writer import get_recorder

    get_recorder()
    started = time.monotonic()
    stop_recording()
    assert time.monotonic() - started < 0.5
    assert "mcp-execution-recorder" not in joined


async def test_query_write_failure_keeps_unknown_outcome(tmp_path):
    sink = _sink(tmp_path)
    cfg = _cfg(sink)
    policy = MagicMock()
    policy.current_policy.return_value = PolicyProfile(profile_name="readonly")
    policy.validate.return_value = {"allowed": True, "intent": {"primary_operation": "UPDATE"}}
    query = QueryService(policy, cfg)
    with patch("db_semantic_mcp.services.query_service.open_connection") as opened:
        cursor = opened.return_value.__enter__.return_value.cursor.return_value
        cursor.execute.side_effect = RuntimeError("driver lost the acknowledgement")
        async with record_tool(cfg, "plan_or_execute_query"):
            result = query.execute_query("UPDATE dbo.T SET a = 1")
    assert result["failure_category"] == "write_outcome_unknown"
    assert result["transaction_state"] == "unknown"
    assert result["automatic_retry_allowed"] is False
    assert tool_outcome("plan_or_execute_query", result)[0] == "unknown"
