"""Immutable recording events and bounded parameter snapshots."""

import base64
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

_COLUMNS = (
    "sink_path",
    "phase",
    "id",
    "call_id",
    "record_type",
    "tool_name",
    "step_name",
    "action_type",
    "source_type",
    "started_at",
    "finished_at",
    "status",
    "sql_text",
    "parameters_json",
    "summary",
)


class SnapshotTooLarge(Exception):
    """Parameter snapshot crossed the caller-supplied byte budget."""


@dataclass(frozen=True, slots=True)
class RecordingEvent:
    sink_path: str
    phase: str
    id: str
    call_id: str
    sequence_no: int
    record_type: str
    tool_name: str
    step_name: str
    action_type: str | None
    source_type: str | None
    started_at: str
    finished_at: str | None
    duration_ms: float | None
    status: str
    sql_text: str | None
    parameters_json: str | None
    summary: str


def event_weight(event: RecordingEvent) -> int:
    """UTF-8 size of the stored text, counted without copying the SQL into one buffer."""
    total = 16
    for name in _COLUMNS:
        value = getattr(event, name)
        if isinstance(value, str) and value:
            total += len(value.encode())
    if event.duration_ms is not None:
        total += 8
    return total


class _Budget:
    def __init__(self, limit: int):
        self.limit = limit
        self.parts: list[str] = []
        self.size = 0

    def write(self, text: str) -> None:
        step = 4096
        for offset in range(0, len(text), step):
            chunk = text[offset : offset + step]
            self.size += len(chunk.encode())
            if self.size > self.limit:
                raise SnapshotTooLarge
            self.parts.append(chunk)

    def finish(self) -> str:
        return "".join(self.parts)


def _write_json_string(buf: _Budget, value: str) -> None:
    buf.write('"')
    piece: list[str] = []
    pending = 0
    for char in value:
        if char == '"':
            piece.append('\\"')
        elif char == "\\":
            piece.append("\\\\")
        elif char == "\n":
            piece.append("\\n")
        elif char == "\r":
            piece.append("\\r")
        elif char == "\t":
            piece.append("\\t")
        elif ord(char) < 32:
            piece.append(f"\\u{ord(char):04x}")
        else:
            piece.append(char)
        pending += 1
        if pending >= 256:
            buf.write("".join(piece))
            piece.clear()
            pending = 0
    if piece:
        buf.write("".join(piece))
    buf.write('"')


def _write_value(buf: _Budget, value: object) -> None:
    if value is None:
        buf.write('{"t": "null"}')
        return
    if isinstance(value, bool):
        buf.write('{"t": "bool", "v": true}' if value else '{"t": "bool", "v": false}')
        return
    if isinstance(value, int) and not isinstance(value, bool):
        buf.write('{"t": "int", "v": ')
        buf.write(str(value))
        buf.write("}")
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise TypeError("non-finite floats are not recorded")
        buf.write('{"t": "float", "v": ')
        buf.write(repr(value))
        buf.write("}")
        return
    if isinstance(value, str):
        buf.write('{"t": "str", "v": ')
        _write_json_string(buf, value)
        buf.write("}")
        return
    if isinstance(value, Decimal):
        buf.write('{"t": "Decimal", "v": ')
        _write_json_string(buf, format(value, "f"))
        buf.write("}")
        return
    if isinstance(value, datetime):
        buf.write('{"t": "datetime", "v": ')
        _write_json_string(buf, value.isoformat())
        buf.write("}")
        return
    if isinstance(value, date):
        buf.write('{"t": "date", "v": ')
        _write_json_string(buf, value.isoformat())
        buf.write("}")
        return
    if isinstance(value, time):
        buf.write('{"t": "time", "v": ')
        _write_json_string(buf, value.isoformat())
        buf.write("}")
        return
    if isinstance(value, (bytes, bytearray, memoryview)):
        buf.write('{"t": "bytes", "v": "')
        buf.write(base64.b64encode(bytes(value)).decode("ascii"))
        buf.write('"}')
        return
    if isinstance(value, dict):
        buf.write('{"t": "dict", "v": {')
        first = True
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("parameter names must be strings")
            if not first:
                buf.write(", ")
            first = False
            _write_json_string(buf, key)
            buf.write(": ")
            _write_value(buf, item)
        buf.write("}}")
        return
    if isinstance(value, (list, tuple)):
        kind = "list" if isinstance(value, list) else "tuple"
        buf.write(f'{{"t": "{kind}", "v": [')
        for index, item in enumerate(value):
            if index:
                buf.write(", ")
            _write_value(buf, item)
        buf.write("]}")
        return
    raise TypeError("parameter value cannot be represented without consuming it")


def snapshot_parameters(value: object, *, limit: int) -> str | None:
    """Serialize driver parameters. ``None`` means the call had no parameters.

    Streams, iterators, and unknown objects raise ``TypeError`` without being read.
    Output stops as soon as ``limit`` bytes would be exceeded.
    """
    if value is None:
        return None
    buf = _Budget(limit)
    _write_value(buf, value)
    return buf.finish()
