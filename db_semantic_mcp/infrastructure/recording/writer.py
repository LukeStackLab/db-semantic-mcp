"""Single daemon writer. Callers only enqueue; SQLite failures stay on this thread."""

import os
import queue
import sqlite3
import sys
import threading
import time
from datetime import datetime

from .events import RecordingEvent, event_weight
from .schema import TAIWAN, initialize_recording_schema, retention_cutoff

MAX_QUEUE_EVENTS = 1024
MAX_QUEUE_BYTES = 8 * 1024 * 1024
_STOP = object()

_state_lock = threading.Lock()
_recorder: "Recorder | None" = None


class _SchemaDisabled(Exception):
    pass


def _is_lock(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _category(exc: BaseException) -> str:
    if _is_lock(exc):
        return "locked"
    message = str(exc).lower()
    if "readonly" in message or "read-only" in message:
        return "readonly"
    if "disk" in message or "full" in message or "i/o" in message:
        return "disk"
    return "disk"


class Recorder:
    def __init__(
        self,
        *,
        max_events: int = MAX_QUEUE_EVENTS,
        max_bytes: int = MAX_QUEUE_BYTES,
        connector=None,
        before_write=None,
        clock=None,
        monotonic=None,
    ):
        self.max_events = max_events
        self.max_bytes = max_bytes
        self._connector = connector or self._default_connect
        self._before_write = before_write
        self._clock = clock or (lambda: datetime.now(TAIWAN))
        self._monotonic = monotonic or time.monotonic
        self._queue: queue.Queue = queue.Queue()
        self._queued = 0
        self._bytes = 0
        self._lock = threading.Lock()
        self._closed = False
        self._disabled: set[str] = set()
        self._ready: set[str] = set()
        self._cleaned: dict[str, str] = {}
        self._conns: dict[str, object] = {}
        self._warn_lock = threading.Lock()
        self._pending = 0
        self._last_warn = -1e300
        self._thread = threading.Thread(target=self._run, name="mcp-execution-recorder", daemon=True)
        self._thread.start()

    def _default_connect(self, path: str):
        conn = sqlite3.connect(path, timeout=0)
        conn.execute("PRAGMA busy_timeout=0")
        return conn

    def note(self, category: str) -> None:
        """Rate-limit one stderr line per 60s. The line never includes SQL or parameters."""
        try:
            with self._warn_lock:
                self._pending += 1
                now = self._monotonic()
                if now - self._last_warn < 60:
                    return
                count = self._pending
                self._pending = 0
                self._last_warn = now
            print(
                f"execution recording dropped {count} events ({category})",
                file=sys.stderr,
                flush=True,
            )
        except Exception:  # noqa: BLE001 - a warning failure must not escape
            return

    def try_emit(self, event: RecordingEvent) -> bool:
        try:
            if self._closed:
                return False
            weight = event_weight(event)
            if weight > self.max_bytes:
                self.note("budget")
                return False
            with self._lock:
                if self._closed or self._queued >= self.max_events or self._bytes + weight > self.max_bytes:
                    rejected = True
                else:
                    self._queue.put_nowait((event, weight))
                    self._queued += 1
                    self._bytes += weight
                    rejected = False
            if rejected:
                self.note("queue")
                return False
            return True
        except Exception:  # noqa: BLE001 - enqueue failures are drops, not caller errors
            self.note("queue")
            return False

    def close(self) -> None:
        """Stop accepting events. Do not wait for the queue to drain."""
        self._closed = True
        try:
            self._queue.put_nowait(_STOP)
        except Exception:  # noqa: BLE001 - shutdown must stay non-blocking
            return

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                self._close_conns()
                return
            event, weight = item
            with self._lock:
                self._queued -= 1
            try:
                self._persist(event)
            except Exception:  # noqa: BLE001 - writer thread must survive a bad event
                self.note("disk")
            finally:
                with self._lock:
                    self._bytes -= weight

    def _persist(self, event: RecordingEvent) -> None:
        if self._before_write is not None:
            self._before_write(event)
        path = event.sink_path
        if path in self._disabled:
            self.note("schema")
            return
        if not os.path.isfile(path):
            self.note("missing")
            return
        conn = self._conns.get(path)
        if conn is None:
            try:
                conn = self._connector(path)
            except Exception as exc:  # noqa: BLE001 - connect failures stay on the writer thread
                self.note(_category(exc))
                return
            self._conns[path] = conn
        try:
            self._ensure_schema(conn, path)
            self._maybe_cleanup(conn, path)
            self._write_event(conn, event)
        except _SchemaDisabled:
            return
        except Exception as exc:  # noqa: BLE001 - persist failures drop the event
            self._rollback(conn)
            self.note(_category(exc))

    def _ensure_schema(self, conn, path: str) -> None:
        if path in self._ready:
            return
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'mcp_execution_log'"
            ).fetchone()
        except Exception as exc:  # noqa: BLE001 - schema lookup failures disable only this file
            if _is_lock(exc):
                raise
            self._disabled.add(path)
            self.note("schema")
            raise _SchemaDisabled from exc
        if row:
            self._ready.add(path)
            return
        try:
            initialize_recording_schema(conn)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - DDL failure disables this file until restart
            if _is_lock(exc):
                raise
            self._disabled.add(path)
            self.note("schema")
            raise _SchemaDisabled from exc
        self._ready.add(path)

    def _maybe_cleanup(self, conn, path: str) -> None:
        day = self._clock().astimezone(TAIWAN).date().isoformat()
        if self._cleaned.get(path) == day:
            return
        self._cleaned[path] = day
        try:
            conn.execute("DELETE FROM mcp_execution_log WHERE started_at < ?", (retention_cutoff(self._clock()),))
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - cleanup runs at most once per day
            self._rollback(conn)
            self.note("locked" if _is_lock(exc) else "cleanup")

    def _write_event(self, conn, event: RecordingEvent) -> None:
        cutoff = retention_cutoff(self._clock())
        values = (
            event.id,
            event.call_id,
            event.sequence_no,
            event.record_type,
            event.tool_name,
            event.step_name,
            event.action_type,
            event.source_type,
            event.started_at,
            event.finished_at,
            event.duration_ms,
            event.status,
            event.sql_text,
            event.parameters_json,
            event.summary,
        )
        placeholders = ", ".join("?" for _ in values)
        conflict = (
            "ON CONFLICT(id) DO NOTHING"
            if event.phase == "start" or event.status == "running"
            else """
            ON CONFLICT(id) DO UPDATE SET
                finished_at = excluded.finished_at,
                duration_ms = excluded.duration_ms,
                status = excluded.status,
                summary = excluded.summary
            WHERE mcp_execution_log.status = 'running'
              AND excluded.status <> 'running'
            """
        )
        conn.execute(
            f"""
            INSERT INTO mcp_execution_log (
                id, call_id, sequence_no, record_type, tool_name, step_name, action_type,
                source_type, started_at, finished_at, duration_ms, status, sql_text,
                parameters_json, summary
            )
            SELECT {placeholders}
            WHERE ? >= ?
            {conflict}
            """,
            (*values, event.started_at, cutoff),
        )
        conn.commit()

    def _rollback(self, conn) -> None:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001 - rollback is best-effort after a failed write
            return

    def _close_conns(self) -> None:
        for conn in self._conns.values():
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - close every connection even if one fails
                continue
        self._conns.clear()


def get_recorder() -> Recorder:
    global _recorder
    with _state_lock:
        if _recorder is None:
            _recorder = Recorder()
        return _recorder


def stop_recording() -> None:
    """Stop accepting new events without waiting for queued writes."""
    global _recorder
    with _state_lock:
        recorder = _recorder
        _recorder = None
    if recorder is not None:
        recorder.close()


def reset_recording() -> None:
    """Drop the process recorder. Tests use this between cases."""
    stop_recording()
