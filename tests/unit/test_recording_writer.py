"""Non-blocking execution recorder: bounds, isolation, and retention."""

import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from decimal import Decimal

import pytest

from db_semantic_mcp.infrastructure.recording.events import (
    RecordingEvent,
    SnapshotTooLarge,
    event_weight,
    snapshot_parameters,
)
from db_semantic_mcp.infrastructure.recording.schema import initialize_recording_schema
from db_semantic_mcp.infrastructure.recording.writer import (
    MAX_QUEUE_BYTES,
    MAX_QUEUE_EVENTS,
    Recorder,
    reset_recording,
)

TAIWAN = timezone(timedelta(hours=8))


def _fixed_clock():
    return datetime(2026, 10, 16, 12, tzinfo=TAIWAN)


def _stamp(day: str, clock: str = "12:00:00.000000") -> str:
    return f"{day}T{clock}+08:00"


def _event(path: str, row_id: str = "row-1", **overrides) -> RecordingEvent:
    values = {
        "sink_path": str(path),
        "phase": "start",
        "id": row_id,
        "call_id": "call-1",
        "sequence_no": 1,
        "record_type": "tool",
        "tool_name": "get_tables",
        "step_name": "get_tables",
        "action_type": "探索",
        "source_type": None,
        "started_at": _stamp("2026-10-16"),
        "finished_at": None,
        "duration_ms": None,
        "status": "running",
        "sql_text": None,
        "parameters_json": None,
        "summary": "running",
    }
    values.update(overrides)
    return RecordingEvent(**values)


def _wait(path, sql, params=(), timeout=2.0, recorder=None):
    """Read after this recorder is idle. A second connection during commit drops the event."""
    if recorder is None:
        raise AssertionError("pass the recorder so the read does not overlap its commit")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with recorder._lock:
            idle = recorder._queued == 0 and recorder._bytes == 0
        if idle:
            break
        time.sleep(0.005)
    else:
        raise AssertionError("writer did not drain")
    with sqlite3.connect(path) as conn:
        row = conn.execute(sql, params).fetchone()
    if row is None:
        raise AssertionError(f"timed out waiting for {sql}")
    return row


@pytest.fixture(autouse=True)
def _isolate_recorder():
    reset_recording()
    yield
    reset_recording()


def _prepare(tmp_path):
    path = tmp_path / "semantic.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sc_tables (name TEXT)")
        conn.execute("INSERT INTO sc_tables (name) VALUES ('orders')")
        initialize_recording_schema(conn)
    return path


def test_event_is_immutable_and_queue_constants_match_spec():
    event = _event("cache.db")
    with pytest.raises(AttributeError):
        event.status = "success"
    assert MAX_QUEUE_EVENTS == 1024
    assert MAX_QUEUE_BYTES == 8 * 1024 * 1024


def test_terminal_state_does_not_regress_and_end_only_upserts(tmp_path):
    path = _prepare(tmp_path)
    recorder = Recorder(clock=_fixed_clock)
    end = _event(
        path,
        phase="end",
        status="unknown",
        finished_at=_stamp("2026-10-16", "12:00:01.000000"),
        duration_ms=12.5,
        summary="交易結果未知",
        sql_text="SELECT secret_sql",
        parameters_json='{"t":"str","v":"hunter2"}',
        record_type="sql",
        source_type="db",
        step_name="sql",
    )
    assert recorder.try_emit(end) is True
    row = _wait(
        path,
        "SELECT status, summary, sql_text, parameters_json FROM mcp_execution_log WHERE id = ?",
        ("row-1",),
        recorder=recorder,
    )
    assert row == ("unknown", "交易結果未知", "SELECT secret_sql", '{"t":"str","v":"hunter2"}')
    late_start = _event(path, phase="start", status="running", summary="running")
    assert recorder.try_emit(late_start) is True
    probe = _event(path, "probe", phase="start", summary="probe")
    assert recorder.try_emit(probe) is True
    _wait(path, "SELECT id FROM mcp_execution_log WHERE id = ?", ("probe",), recorder=recorder)
    again = _wait(
        path,
        "SELECT status, summary, sql_text FROM mcp_execution_log WHERE id = ?",
        ("row-1",),
        recorder=recorder,
    )
    assert again == ("unknown", "交易結果未知", "SELECT secret_sql")
    recorder.close()


def test_start_only_stays_running_and_expired_end_does_not_resurrect(tmp_path):
    path = _prepare(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO mcp_execution_log (
                id, call_id, sequence_no, record_type, tool_name, step_name, action_type,
                source_type, started_at, finished_at, duration_ms, status, sql_text,
                parameters_json, summary
            ) VALUES ('old', 'c', 1, 'sql', 'get_tables', 'sql', '探索', 'db', ?, NULL, NULL,
                      'running', 'SELECT old', NULL, 'running')
            """,
            (_stamp("2026-10-09"),),
        )
    clock = {"now": datetime(2026, 10, 16, 12, tzinfo=TAIWAN)}
    recorder = Recorder(clock=lambda: clock["now"])
    assert recorder.try_emit(_event(path, "live")) is True
    _wait(path, "SELECT status FROM mcp_execution_log WHERE id = ?", ("live",), recorder=recorder)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM mcp_execution_log WHERE id = ?", ("live",)).fetchone() == ("running",)
        assert conn.execute("SELECT id FROM mcp_execution_log WHERE id = 'old'").fetchone() is None
        assert conn.execute("SELECT name FROM sc_tables").fetchone() == ("orders",)
    expired_end = _event(
        path,
        "old",
        phase="end",
        status="success",
        started_at=_stamp("2026-10-09"),
        finished_at=_stamp("2026-10-09", "12:00:01.000000"),
        duration_ms=1,
        summary="SQL 執行完成",
        sql_text="SELECT old",
        record_type="sql",
        source_type="db",
        step_name="sql",
    )
    assert recorder.try_emit(expired_end) is True
    assert recorder.try_emit(_event(path, "after")) is True
    _wait(path, "SELECT id FROM mcp_execution_log WHERE id = ?", ("after",), recorder=recorder)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT id FROM mcp_execution_log WHERE id = 'old'").fetchone() is None
    recorder.close()


def test_cleanup_runs_once_per_taiwan_day_even_when_delete_fails(tmp_path):
    path = _prepare(tmp_path)
    clock = {"now": datetime(2026, 10, 16, 12, tzinfo=TAIWAN)}
    deletes = {"count": 0}

    committed: list[str] = []

    def connector(target):
        conn = sqlite3.connect(target, timeout=0)
        pending: list[str] = []

        class Wrapped:
            def execute(self, sql, params=()):
                if sql.lstrip().upper().startswith("DELETE"):
                    deletes["count"] += 1
                    raise sqlite3.OperationalError("disk I/O error")
                result = conn.execute(sql, params)
                if sql.lstrip().upper().startswith("INSERT") and params:
                    pending.append(str(params[0]))
                return result

            def executescript(self, script):
                return conn.executescript(script)

            def commit(self):
                committed.extend(pending)
                pending.clear()
                return conn.commit()

            def rollback(self):
                pending.clear()
                return conn.rollback()

            def close(self):
                return conn.close()

        return Wrapped()

    def _committed(row_id: str) -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if row_id in committed:
                return
            time.sleep(0.01)
        raise AssertionError(row_id)

    recorder = Recorder(clock=lambda: clock["now"], connector=connector)
    assert recorder.try_emit(_event(path, "a")) is True
    assert recorder.try_emit(_event(path, "b")) is True
    _committed("b")
    time.sleep(0.05)
    assert deletes["count"] == 1
    clock["now"] = datetime(2026, 10, 17, 1, tzinfo=TAIWAN)
    assert recorder.try_emit(_event(path, "c", started_at=_stamp("2026-10-17"))) is True
    _committed("c")
    time.sleep(0.05)
    assert deletes["count"] == 2
    recorder.close()


def test_queue_bounds_drop_whole_events_without_truncation(tmp_path):
    path = _prepare(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def before_write(_event):
        if not entered.is_set():
            entered.set()
            release.wait(timeout=3)

    recorder = Recorder(max_events=1024, max_bytes=8 * 1024 * 1024, before_write=before_write, clock=_fixed_clock)
    try:
        try:
            assert recorder.try_emit(_event(path, "first")) is True
            assert entered.wait(timeout=2)
            for index in range(1024):
                assert recorder.try_emit(_event(path, f"q{index}")) is True
            dropped = _event(path, "overflow", sql_text="SELECT FULL_NOT_TRUNCATED")
            assert recorder.try_emit(dropped) is False
        finally:
            release.set()
        accepted = False
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if recorder.try_emit(_event(path, "after-release")):
                accepted = True
                break
            time.sleep(0.01)
        assert accepted
        _wait(
            path,
            "SELECT id FROM mcp_execution_log WHERE id = ?",
            ("after-release",),
            timeout=5,
            recorder=recorder,
        )
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT id FROM mcp_execution_log WHERE id = 'overflow'").fetchone() is None
            stored = conn.execute("SELECT sql_text FROM mcp_execution_log WHERE sql_text IS NOT NULL").fetchall()
        assert all(row[0] != "SELECT FULL_NOT_TRUNCAT" for row in stored)
    finally:
        recorder.close()


def test_byte_budget_is_injectable_and_drops_oversized_event_whole(tmp_path):
    path = _prepare(tmp_path)
    small = _event(path, "small", sql_text="SELECT ok")
    weight = event_weight(small)
    entered = threading.Event()
    release = threading.Event()

    def before_write(event):
        if event.id == "small":
            entered.set()
            release.wait(timeout=3)

    recorder = Recorder(max_bytes=weight, before_write=before_write, clock=_fixed_clock)
    try:
        assert recorder.try_emit(small) is True
        assert entered.wait(timeout=2)
        second = _event(path, "other", sql_text="SELECT ok")
        assert recorder.try_emit(second) is False
        huge = _event(path, "huge", sql_text="SELECT " + ("S" * 100))
        assert event_weight(huge) > weight
        assert recorder.try_emit(huge) is False
    finally:
        release.set()
    probe = _event(path, "probe")
    accepted = False
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if recorder.try_emit(probe):
            accepted = True
            break
        time.sleep(0.01)
    assert accepted
    _wait(path, "SELECT id FROM mcp_execution_log WHERE id = ?", ("probe",), recorder=recorder)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT sql_text FROM mcp_execution_log WHERE id = 'small'").fetchone() == ("SELECT ok",)
        assert conn.execute("SELECT id FROM mcp_execution_log WHERE id IN ('other', 'huge')").fetchall() == []
        assert conn.execute("SELECT sql_text FROM mcp_execution_log WHERE sql_text LIKE '%SSS%'").fetchone() is None
    recorder.close()


def test_snapshot_preserves_typed_values_and_rejects_streams_without_reading():
    class Stream:
        def read(self, _n=-1):
            raise AssertionError("stream consumed")

    def source():
        raise AssertionError("iterator consumed")
        yield 1

    stream = Stream()
    generator = source()
    with pytest.raises(TypeError):
        snapshot_parameters(stream, limit=10_000)
    with pytest.raises(TypeError):
        snapshot_parameters(generator, limit=10_000)
    assert stream.read.__func__ is Stream.read
    payload = snapshot_parameters(
        {
            "amount": Decimal("10.50"),
            "day": date(2026, 10, 16),
            "clock": dt_time(1, 2, 3),
            "moment": datetime(2026, 10, 16, 1, 2, 3, tzinfo=TAIWAN),
            "flag": True,
            "blob": b"\xff\x00",
            "n": 3,
            "empty": None,
        },
        limit=10_000,
    )
    assert '"t": "Decimal"' in payload and "10.50" in payload
    assert '"t": "date"' in payload and "2026-10-16" in payload
    assert '"t": "bool"' in payload
    assert '"t": "bytes"' in payload and "/wA=" in payload
    assert '"amount"' in payload and '"n"' in payload
    ordered = snapshot_parameters([("first", 1), ("second", 2)], limit=10_000)
    assert ordered.index("first") < ordered.index("second")
    assert snapshot_parameters(None, limit=100) is None
    with pytest.raises(SnapshotTooLarge):
        snapshot_parameters("P" * 500, limit=32)


def test_writer_failures_do_not_escape_and_warnings_are_bounded(tmp_path, capsys):
    path = _prepare(tmp_path)
    holder = sqlite3.connect(path)
    holder.execute("BEGIN EXCLUSIVE")
    mono = {"now": 1_000.0}
    recorder = Recorder(monotonic=lambda: mono["now"], clock=_fixed_clock)
    try:
        for index in range(4):
            assert (
                recorder.try_emit(
                    _event(path, f"locked-{index}", sql_text="SELECT SECRET_SQL", parameters_json="hunter2")
                )
                is True
            )
        deadline = time.monotonic() + 2
        captured = ""
        while time.monotonic() < deadline:
            captured += capsys.readouterr().err
            if "execution recording dropped" in captured:
                break
            time.sleep(0.01)
        time.sleep(0.15)
        captured += capsys.readouterr().err
        assert captured.count("execution recording dropped") == 1
        assert "SECRET_SQL" not in captured
        assert "hunter2" not in captured
        assert "locked" in captured
    finally:
        holder.rollback()
        holder.close()
    # Re-read through the same file after release: locked events were not retried.
    assert recorder.try_emit(_event(path, "later")) is True
    _wait(path, "SELECT id FROM mcp_execution_log WHERE id = ?", ("later",), recorder=recorder)
    with sqlite3.connect(path) as conn:
        locked = conn.execute("SELECT id FROM mcp_execution_log WHERE id LIKE 'locked-%'").fetchall()
    assert locked == []
    mono["now"] += 61
    missing = tmp_path / "missing" / "semantic.db"
    assert recorder.try_emit(_event(missing, "gone", sql_text="SELECT SECRET_SQL")) is True
    deadline = time.monotonic() + 2
    seen = ""
    while time.monotonic() < deadline:
        seen = capsys.readouterr().err
        if "execution recording dropped" in seen:
            break
        time.sleep(0.01)
    assert "execution recording dropped" in seen
    assert "SECRET_SQL" not in seen
    assert not missing.exists()
    recorder.close()


def test_schema_failure_disables_path_until_process_restart(tmp_path):
    path = tmp_path / "semantic.db"
    sqlite3.connect(path).close()
    scripts = {"count": 0}

    def connector(target):
        conn = sqlite3.connect(target, timeout=0)

        class Wrapped:
            def execute(self, sql, params=()):
                return conn.execute(sql, params)

            def executescript(self, script):
                scripts["count"] += 1
                raise sqlite3.DatabaseError("schema mismatch")

            def commit(self):
                return conn.commit()

            def rollback(self):
                return conn.rollback()

            def close(self):
                return conn.close()

        return Wrapped()

    recorder = Recorder(connector=connector)
    assert recorder.try_emit(_event(path, "a")) is True
    assert recorder.try_emit(_event(path, "b")) is True
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and scripts["count"] < 1:
        time.sleep(0.01)
    time.sleep(0.05)
    assert scripts["count"] == 1
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'mcp_execution_log'"
            ).fetchone()
            is None
        )
    recorder.close()


def test_writer_connects_with_zero_sqlite_timeout(tmp_path, monkeypatch):
    path = _prepare(tmp_path)
    timeouts = []
    real = sqlite3.connect

    def connect(target, timeout=5.0, **kwargs):
        if threading.current_thread().name == "mcp-execution-recorder":
            timeouts.append(timeout)
        return real(target, timeout=timeout, **kwargs)

    monkeypatch.setattr(
        "db_semantic_mcp.infrastructure.recording.writer.sqlite3.connect",
        connect,
    )
    recorder = Recorder(clock=_fixed_clock)
    try:
        assert recorder.try_emit(_event(path, "row")) is True
        _wait(path, "SELECT id FROM mcp_execution_log WHERE id = ?", ("row",), recorder=recorder)
        assert timeouts == [0]
    finally:
        recorder.close()
