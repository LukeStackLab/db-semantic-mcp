"""Call context for one tool invocation and the SQL it directly causes."""

import asyncio
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

from . import events
from .events import RecordingEvent
from .schema import format_taiwan_timestamp

EXPLORATION_TOOLS = frozenset(
    {
        "get_tables",
        "describe_table",
        "describe_object",
        "get_table_relationships",
        "get_dependency_chain",
        "detect_lookup_tables",
        "find_join_path",
        "discover_relevant_tables",
    }
)
VERIFICATION_TOOLS = frozenset({"plan_or_execute_query"})
SUMMARY_LIMIT = 200

_call: ContextVar[object | None] = ContextVar("execution_recording_call", default=None)
_suppress: ContextVar[bool] = ContextVar("execution_recording_suppress", default=False)


def recording_active() -> bool:
    return _call.get() is not None and not _suppress.get()


def _action_for(tool_name: str) -> str | None:
    if tool_name in EXPLORATION_TOOLS:
        return "探索"
    if tool_name in VERIFICATION_TOOLS:
        return "驗證"
    return None


def _clip(summary: str) -> str:
    return (summary or "")[:SUMMARY_LIMIT]


class _NoopSpan:
    call_id = None

    def finish(self, status: str, summary: str) -> None:
        return None


class _Call:
    def __init__(self, recorder, sink_path: str, call_id: str, tool_name: str, action_type: str | None):
        self.recorder = recorder
        self.sink_path = sink_path
        self.call_id = call_id
        self.tool_name = tool_name
        self.action_type = action_type
        self._sequence = 0
        self._lock = threading.Lock()

    def next_sequence(self) -> int:
        with self._lock:
            self._sequence += 1
            return self._sequence


class _Span:
    def __init__(self, call_id: str):
        self.call_id = call_id
        self.finished = False
        self.status = "unknown"
        self.summary = "結果未知"

    def finish(self, status: str, summary: str) -> None:
        self.finished = True
        self.status = status
        self.summary = _clip(summary)


def _emit(recorder, event: RecordingEvent) -> None:
    try:
        recorder.try_emit(event)
    except Exception:  # noqa: BLE001 - recording must not escape into the tool
        recorder.note("serialize")


def _event(call: _Call, **fields) -> RecordingEvent:
    base = {
        "sink_path": call.sink_path,
        "id": fields.pop("id"),
        "call_id": call.call_id,
        "sequence_no": fields.pop("sequence_no"),
        "record_type": fields.pop("record_type"),
        "tool_name": call.tool_name,
        "step_name": fields.pop("step_name"),
        "action_type": call.action_type,
        "source_type": fields.pop("source_type"),
        "started_at": fields.pop("started_at"),
        "finished_at": fields.pop("finished_at", None),
        "duration_ms": fields.pop("duration_ms", None),
        "status": fields.pop("status"),
        "sql_text": fields.pop("sql_text", None),
        "parameters_json": fields.pop("parameters_json", None),
        "summary": fields.pop("summary"),
        "phase": fields.pop("phase"),
    }
    return RecordingEvent(**base)


@asynccontextmanager
async def record_tool(cfg, tool_name: str):
    if not getattr(cfg, "execution_recording_enabled", False):
        yield _NoopSpan()
        return
    from .writer import get_recorder

    recorder = get_recorder()
    call = _Call(recorder, str(cfg.cache_path), str(uuid.uuid4()), tool_name, _action_for(tool_name))
    token = _call.set(call)
    started_at = format_taiwan_timestamp(recorder._clock())
    mono = time.monotonic()
    identity = {
        "id": str(uuid.uuid4()),
        "sequence_no": call.next_sequence(),
        "record_type": "tool",
        "step_name": tool_name,
        "source_type": None,
        "started_at": started_at,
        "sql_text": None,
        "parameters_json": None,
    }
    _emit(
        recorder,
        _event(
            call, phase="start", status="running", summary="running", finished_at=None, duration_ms=None, **identity
        ),
    )
    span = _Span(call.call_id)
    try:
        yield span
    except asyncio.CancelledError:
        if not span.finished:
            span.finish("cancelled", "已取消")
        raise
    except Exception as exc:  # noqa: BLE001 - record the class, then preserve the original error
        if not span.finished:
            span.finish("failed", f"失敗：{type(exc).__name__}")
        raise
    finally:
        if not span.finished:
            span.finish("unknown", "結果未知")
        _emit(
            recorder,
            _event(
                call,
                phase="end",
                status=span.status,
                summary=span.summary,
                finished_at=format_taiwan_timestamp(recorder._clock()),
                duration_ms=(time.monotonic() - mono) * 1000,
                **identity,
            ),
        )
        _call.reset(token)


@contextmanager
def record_sql(source_type: str, sql: str, parameters: object = None):
    call = _call.get()
    if call is None or _suppress.get():
        yield _NoopSpan()
        return
    try:
        parameters_json = events.snapshot_parameters(parameters, limit=call.recorder.max_bytes)
    except events.SnapshotTooLarge:
        call.recorder.note("budget")
        yield _NoopSpan()
        return
    except TypeError:
        call.recorder.note("serialize")
        yield _NoopSpan()
        return
    except Exception:  # noqa: BLE001 - recording must not escape into the caller
        call.recorder.note("serialize")
        yield _NoopSpan()
        return
    started_at = format_taiwan_timestamp(call.recorder._clock())
    mono = time.monotonic()
    identity = {
        "id": str(uuid.uuid4()),
        "sequence_no": call.next_sequence(),
        "record_type": "sql",
        "step_name": "sql",
        "source_type": source_type,
        "started_at": started_at,
        "sql_text": sql,
        "parameters_json": parameters_json,
    }
    _emit(
        call.recorder,
        _event(
            call,
            phase="start",
            status="running",
            summary="running",
            finished_at=None,
            duration_ms=None,
            **identity,
        ),
    )
    span = _Span(call.call_id)
    error: BaseException | None = None
    try:
        yield span
    except BaseException as exc:
        error = exc
        raise
    finally:
        if not span.finished:
            if error is None:
                span.finish("success", "SQL 執行完成")
            elif isinstance(error, asyncio.CancelledError):
                span.finish("unknown", "結果未知")
            else:
                span.finish("failed", f"失敗：{type(error).__name__}")
        _emit(
            call.recorder,
            _event(
                call,
                phase="end",
                status=span.status,
                summary=span.summary,
                finished_at=format_taiwan_timestamp(call.recorder._clock()),
                duration_ms=(time.monotonic() - mono) * 1000,
                **identity,
            ),
        )


@contextmanager
def suppress_recording():
    token = _suppress.set(True)
    try:
        yield
    finally:
        _suppress.reset(token)
