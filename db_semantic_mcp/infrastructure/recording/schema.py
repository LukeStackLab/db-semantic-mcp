"""SQLite objects for best-effort MCP execution recording."""

from datetime import datetime, timedelta, timezone

TAIWAN = timezone(timedelta(hours=8))
RETENTION_PREVIOUS_DAYS = 6
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%f+08:00"

_DDL = """
CREATE TABLE IF NOT EXISTS mcp_execution_log (
    id               TEXT PRIMARY KEY,
    call_id          TEXT NOT NULL,
    sequence_no      INTEGER NOT NULL,
    record_type      TEXT NOT NULL,
    tool_name        TEXT NOT NULL,
    step_name        TEXT NOT NULL,
    action_type      TEXT,
    source_type      TEXT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    duration_ms      REAL,
    status           TEXT NOT NULL,
    sql_text         TEXT,
    parameters_json  TEXT,
    summary          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mcp_execution_log_started_at
    ON mcp_execution_log (started_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_mcp_execution_log_call_seq
    ON mcp_execution_log (call_id, sequence_no);

CREATE VIEW IF NOT EXISTS v_mcp_execution_today AS
SELECT *
FROM mcp_execution_log
WHERE started_at >= printf('%sT00:00:00.000000+08:00', date('now', '+8 hours'))
  AND started_at < printf('%sT00:00:00.000000+08:00', date('now', '+8 hours', '+1 day'));
"""


def initialize_recording_schema(conn) -> None:
    """Create the log table, indexes, and Taiwan-day view. Safe to repeat."""
    conn.executescript(_DDL)


def format_taiwan_timestamp(moment: datetime) -> str:
    """Format an aware instant as the fixed Taiwan timestamp stored in the log."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("timestamp requires an aware datetime")
    local = moment.astimezone(TAIWAN).replace(tzinfo=None)
    return local.strftime("%Y-%m-%dT%H:%M:%S.%f") + "+08:00"


def retention_cutoff(now: datetime) -> str:
    """Taiwan midnight of today, minus six calendar days, in the log timestamp format."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("retention_cutoff requires an aware datetime")
    local = now.astimezone(TAIWAN)
    today_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today_start - timedelta(days=RETENTION_PREVIOUS_DAYS)
    return format_taiwan_timestamp(cutoff)
