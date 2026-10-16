"""Schema, Taiwan-day view, and retention boundary for execution recording."""

import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest

from db_semantic_mcp.infrastructure.recording.schema import (
    RETENTION_PREVIOUS_DAYS,
    initialize_recording_schema,
    retention_cutoff,
)

TAIWAN = timezone(timedelta(hours=8))
COLUMNS = [
    "id",
    "call_id",
    "sequence_no",
    "record_type",
    "tool_name",
    "step_name",
    "action_type",
    "source_type",
    "started_at",
    "finished_at",
    "duration_ms",
    "status",
    "sql_text",
    "parameters_json",
    "summary",
]


def _stamp(day: str, clock: str = "12:00:00.000000") -> str:
    return f"{day}T{clock}+08:00"


def _insert(conn: sqlite3.Connection, row_id: str, started_at: str, **overrides) -> None:
    values = {
        "id": row_id,
        "call_id": "call-1",
        "sequence_no": 1,
        "record_type": "tool",
        "tool_name": "get_tables",
        "step_name": "get_tables",
        "action_type": "探索",
        "source_type": None,
        "started_at": started_at,
        "finished_at": None,
        "duration_ms": None,
        "status": "running",
        "sql_text": None,
        "parameters_json": None,
        "summary": "running",
    }
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    conn.execute(
        f"INSERT INTO mcp_execution_log ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )


def _freeze_date(conn: sqlite3.Connection, utc_now: str) -> None:
    """Override only this connection's date() so production SQL can still say date('now')."""
    helper = sqlite3.connect(":memory:")

    def frozen_date(*args):
        rewritten = [utc_now if isinstance(arg, str) and arg.lower() == "now" else arg for arg in args]
        marks = ", ".join("?" for _ in rewritten)
        return helper.execute(f"SELECT date({marks})", rewritten).fetchone()[0]

    conn.create_function("date", -1, frozen_date)


def test_old_cache_gains_recording_tables_without_rewriting_existing_rows():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sc_tables (name TEXT)")
    conn.execute("INSERT INTO sc_tables (name) VALUES ('orders')")
    initialize_recording_schema(conn)
    _insert(conn, "row-1", _stamp("2026-10-16"))
    initialize_recording_schema(conn)

    assert conn.execute("SELECT name FROM sc_tables").fetchall() == [("orders",)]
    assert conn.execute("SELECT id, summary FROM mcp_execution_log").fetchall() == [("row-1", "running")]
    columns = [row[1] for row in conn.execute("PRAGMA table_info(mcp_execution_log)")]
    assert columns == COLUMNS
    indexes = {
        row[1]: [info[2] for info in conn.execute(f"PRAGMA index_info({row[1]!r})")]
        for row in conn.execute("PRAGMA index_list(mcp_execution_log)")
    }
    assert indexes["idx_mcp_execution_log_started_at"] == ["started_at", "id"]
    assert indexes["idx_mcp_execution_log_call_seq"] == ["call_id", "sequence_no"]
    view_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'view' AND name = 'v_mcp_execution_today'"
    ).fetchone()[0]
    assert "date('now'" in view_sql
    assert "+8 hours" in view_sql
    assert "started_at >=" in view_sql and "started_at <" in view_sql
    assert "create_function" not in view_sql.lower()


def test_today_view_uses_taiwan_midnight_and_keeps_every_status():
    conn = sqlite3.connect(":memory:")
    initialize_recording_schema(conn)
    # UTC 2026-10-15 16:00:00 is Taiwan 2026-10-16 00:00:00.
    _freeze_date(conn, "2026-10-15 16:00:00")
    rows = [
        ("before", _stamp("2026-10-15", "23:59:59.999999"), "failed", "sql", "db"),
        ("start", _stamp("2026-10-16", "00:00:00.000000"), "running", "tool", None),
        ("mid", _stamp("2026-10-16", "15:00:00.000000"), "success", "sql", "cache"),
        ("end", _stamp("2026-10-17", "00:00:00.000000"), "cancelled", "tool", None),
        ("unknown", _stamp("2026-10-16", "08:00:00.000000"), "unknown", "sql", "db"),
    ]
    for row_id, started_at, status, record_type, source_type in rows:
        _insert(
            conn,
            row_id,
            started_at,
            status=status,
            record_type=record_type,
            source_type=source_type,
            sequence_no=1,
        )

    found = conn.execute("SELECT id FROM v_mcp_execution_today ORDER BY started_at DESC, id DESC").fetchall()
    assert [row[0] for row in found] == ["mid", "unknown", "start"]
    plan = "\n".join(str(row) for row in conn.execute("EXPLAIN QUERY PLAN SELECT * FROM v_mcp_execution_today"))
    assert "idx_mcp_execution_log_started_at" in plan


def test_retention_keeps_today_and_six_previous_calendar_days():
    assert RETENTION_PREVIOUS_DAYS == 6
    noon = datetime(2026, 10, 16, 12, 0, tzinfo=TAIWAN)
    assert retention_cutoff(noon) == "2026-10-10T00:00:00.000000+08:00"
    just_after_midnight = datetime(2026, 10, 16, 0, 0, tzinfo=TAIWAN)
    just_before_midnight = datetime(2026, 10, 15, 23, 59, 59, 999999, tzinfo=TAIWAN)
    assert retention_cutoff(just_after_midnight) == "2026-10-10T00:00:00.000000+08:00"
    assert retention_cutoff(just_before_midnight) == "2026-10-09T00:00:00.000000+08:00"
    # Host zone is irrelevant: this instant is still Taiwan 2026-10-16 00:00.
    utc_midnight_boundary = datetime(2026, 10, 15, 16, 0, tzinfo=UTC)
    assert retention_cutoff(utc_midnight_boundary) == "2026-10-10T00:00:00.000000+08:00"

    conn = sqlite3.connect(":memory:")
    initialize_recording_schema(conn)
    kept = ["2026-10-10", "2026-10-16"]
    dropped = ["2026-10-09"]
    for day in kept + dropped:
        _insert(conn, day, _stamp(day, "00:00:00.000000"))
    _insert(conn, "late-previous", _stamp("2026-10-09", "23:59:59.999999"))
    conn.execute("DELETE FROM mcp_execution_log WHERE started_at < ?", (retention_cutoff(noon),))
    remaining = {row[0] for row in conn.execute("SELECT id FROM mcp_execution_log")}
    assert remaining == {"2026-10-10", "2026-10-16"}
    assert "late-previous" not in remaining


def test_retention_cutoff_rejects_naive_datetime():
    with pytest.raises(ValueError):
        retention_cutoff(datetime(2026, 10, 16, 12, 0))
