"""Tool and SQL spans. Recording failure stays outside the original result."""

import sqlite3
import threading
import time
from contextvars import copy_context
from types import SimpleNamespace

import pytest

from db_semantic_mcp.infrastructure.recording import events as events_mod
from db_semantic_mcp.infrastructure.recording.context import record_sql, record_tool, suppress_recording
from db_semantic_mcp.infrastructure.recording.schema import initialize_recording_schema
from db_semantic_mcp.infrastructure.recording.writer import Recorder, reset_recording


@pytest.fixture(autouse=True)
def _isolate_recorder():
    reset_recording()
    yield
    reset_recording()


def _db(tmp_path):
    path = tmp_path / "semantic.db"
    with sqlite3.connect(path) as conn:
        initialize_recording_schema(conn)
    return path


def _cfg(path, enabled=True):
    return SimpleNamespace(execution_recording_enabled=enabled, cache_path=str(path))


def _wait(path, sql, params=(), timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with sqlite3.connect(path) as conn:
            row = conn.execute(sql, params).fetchone()
        if row is not None:
            return row
        time.sleep(0.01)
    raise AssertionError(sql)


async def test_disabled_does_not_start_writer_or_serialize(tmp_path, monkeypatch):
    path = _db(tmp_path)

    def explode_recorder():
        raise AssertionError("writer started")

    def explode_snapshot(*_args, **_kwargs):
        raise AssertionError("serialized")

    monkeypatch.setattr(
        "db_semantic_mcp.infrastructure.recording.writer.get_recorder",
        explode_recorder,
    )
    monkeypatch.setattr(
        "db_semantic_mcp.infrastructure.recording.events.snapshot_parameters",
        explode_snapshot,
    )

    class Bad:
        def __repr__(self):
            raise AssertionError("repr")

        def __iter__(self):
            raise AssertionError("iter")

    async with record_tool(_cfg(path, enabled=False), "get_tables") as span:
        span.finish("success", "找到 1 張表")
        with record_sql("db", "SELECT secret", Bad()):
            value = 5
    assert value == 5
    assert list(path.parent.glob("*.db")) == [path]


async def test_running_row_is_visible_before_finish(tmp_path):
    path = _db(tmp_path)
    async with record_tool(_cfg(path), "get_tables") as span:
        row = _wait(path, "SELECT status, tool_name, action_type, source_type FROM mcp_execution_log")
        assert row == ("running", "get_tables", "探索", None)
        assert isinstance(span.call_id, str) and span.call_id
        span.finish("success", "找到 5 張表")
    done = _wait(path, "SELECT status, summary FROM mcp_execution_log WHERE status != 'running'")
    assert done == ("success", "找到 5 張表")


async def test_tool_returns_while_writer_is_blocked(tmp_path, monkeypatch):
    path = _db(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def before_write(_event):
        if not entered.is_set():
            entered.set()
            release.wait(timeout=3)

    recorder = Recorder(before_write=before_write)
    monkeypatch.setattr(
        "db_semantic_mcp.infrastructure.recording.writer.get_recorder",
        lambda: recorder,
    )
    try:
        async with record_tool(_cfg(path), "describe_table") as span:
            assert entered.wait(timeout=2)
            started = time.monotonic()
            span.finish("success", "ok")
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        recorder.close()


async def test_sql_failure_and_unrepresentable_parameters_keep_the_caller_result(tmp_path):
    path = _db(tmp_path)

    class Stream:
        def __init__(self):
            self.consumed = False

        def read(self, _n=-1):
            self.consumed = True
            return b"data"

    stream = Stream()

    async with record_tool(_cfg(path), "plan_or_execute_query") as span:
        with record_sql("db", "SELECT " + ("A" * 50), stream):
            caller = "kept"
        with pytest.raises(RuntimeError, match="SELECT SECRET"):
            with record_sql("cache", "SELECT SECRET", (1, "visible-param")):
                raise RuntimeError("SELECT SECRET")
        with record_sql("db", "UPDATE t SET a = :a", {"a": "full-value"}) as sql_span:
            sql_span.finish("unknown", "交易結果未知")
        span.finish("failed", "失敗：policy_denied" + ("x" * 300))
    assert caller == "kept"
    assert stream.consumed is False
    deadline = time.monotonic() + 2
    rows = []
    while time.monotonic() < deadline:
        with sqlite3.connect(path) as conn:
            rows = conn.execute(
                "SELECT record_type, source_type, status, summary, sql_text, parameters_json, action_type "
                "FROM mcp_execution_log ORDER BY sequence_no"
            ).fetchall()
        if len(rows) >= 3 and rows[0][2] == "failed" and all(row[2] != "running" for row in rows):
            break
        time.sleep(0.01)
    assert rows[0][0] == "tool"
    assert rows[0][2] == "failed"
    assert len(rows[0][3]) == 200
    assert rows[0][6] == "驗證"
    sql_rows = [row for row in rows if row[0] == "sql"]
    assert ("cache", "failed") in {(row[1], row[2]) for row in sql_rows}
    failed = next(row for row in sql_rows if row[2] == "failed")
    assert failed[3] == "失敗：RuntimeError"
    assert failed[6] == "驗證"
    assert "SELECT SECRET" not in (failed[3] or "")
    assert failed[4] == "SELECT SECRET"
    assert "visible-param" in failed[5]
    unknown = next(row for row in sql_rows if row[2] == "unknown")
    assert unknown[3] == "交易結果未知"
    assert unknown[1] == "db"
    assert "full-value" in unknown[5]
    texts = " ".join(row[4] or "" for row in sql_rows)
    assert "AAA" not in texts
    with sqlite3.connect(path) as conn:
        call_ids = {row[0] for row in conn.execute("SELECT call_id FROM mcp_execution_log")}
    assert call_ids == {span.call_id}


async def test_suppress_and_missing_context_skip_sql(tmp_path, monkeypatch):
    path = _db(tmp_path)

    def explode_snapshot(*_args, **_kwargs):
        raise AssertionError("serialized")

    with record_sql("db", "SELECT outside", ("x",)):
        outside = True
    assert outside is True
    async with record_tool(_cfg(path), "tool_metrics") as span:
        original_snapshot = events_mod.snapshot_parameters
        monkeypatch.setattr(events_mod, "snapshot_parameters", explode_snapshot)
        with suppress_recording():
            with record_sql("cache", "INSERT INTO tool_metrics VALUES (?)", ("metric",)):
                suppressed = True
        monkeypatch.setattr(events_mod, "snapshot_parameters", original_snapshot)
        with record_sql("cache", "SELECT counted", None):
            counted = True
        span.finish("success", "metrics")
    assert suppressed and counted
    _wait(path, "SELECT summary FROM mcp_execution_log WHERE record_type = 'sql'")
    with sqlite3.connect(path) as conn:
        sql_text = [row[0] for row in conn.execute("SELECT sql_text FROM mcp_execution_log")]
    assert "INSERT INTO tool_metrics VALUES (?)" not in sql_text
    assert "SELECT counted" in sql_text
    assert all(text is None or "outside" not in text for text in sql_text)


async def test_sequence_numbers_are_unique_across_copied_contexts(tmp_path):
    path = _db(tmp_path)

    async with record_tool(_cfg(path), "get_tables") as span:
        parent = copy_context()
        errors = []

        def work(index):
            try:
                parent.run(_one_sql, index)
            except Exception as exc:  # noqa: BLE001 - the test records worker failures
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        span.finish("success", "ok")
    deadline = time.monotonic() + 2
    ready = False
    while time.monotonic() < deadline:
        with sqlite3.connect(path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM mcp_execution_log WHERE sequence_no = 9").fetchone()[0]
        if count == 1:
            ready = True
            break
        time.sleep(0.01)
    assert ready
    with sqlite3.connect(path) as conn:
        sequences = [row[0] for row in conn.execute("SELECT sequence_no FROM mcp_execution_log ORDER BY sequence_no")]
    assert sequences == list(range(1, 10))


def _one_sql(index):
    with record_sql("db", f"SELECT {index}", (index,)):
        return None
