"""Transport-layer response shaping helper.

See docs/superpowers/specs/2026-04-19-p0-token-optimization-design.md for rules.
"""
from typing import Any

NULLABLE_FALSE_KEEP: frozenset[str] = frozenset({
    "is_nullable", "allowed", "executed", "found", "truncated",
    "readonly", "exceeded_cap",
    "metadata_complete", "ambiguous", "result_complete", "rows_omitted",
    "columns_omitted", "stale", "refreshing", "refresh_failed", "hit",
    "definition_withheld", "dependencies_omitted", "nullable", "optional",
    "automatic_retry_allowed",
})

NULL_KEEP = frozenset({"total_row_count"})
EMPTY_KEEP = frozenset({"rows", "sample_rows", "candidates", "column_pairs", "data"})


def _is_falsy_strippable(value: Any) -> bool:
    return value is None or value == [] or value == {} or value is False


def _merge_table_id(d: dict) -> dict:
    schema = d.get("schema_name")
    table = d.get("table_name")
    if not (isinstance(schema, str) and isinstance(table, str) and schema and table):
        return d
    out: dict[str, Any] = {}
    merged = False
    for k, v in d.items():
        if k == "schema_name":
            if not merged:
                out["table"] = f"{schema}.{table}"
                merged = True
        elif k == "table_name":
            continue
        else:
            out[k] = v
    return out


def _merge_object_id(d: dict) -> dict:
    schema = d.get("schema")
    name = d.get("object_name")
    if not (isinstance(schema, str) and isinstance(name, str) and schema and name):
        return d
    out: dict[str, Any] = {}
    merged = False
    for k, v in d.items():
        if k == "schema":
            if not merged:
                out["object"] = f"{schema}.{name}"
                merged = True
        elif k == "object_name":
            continue
        elif k == "object_type":
            out["type"] = v
        else:
            out[k] = v
    return out


def compact(obj: Any) -> Any:
    """Recursively strip falsy values and merge identifier pairs.

    Application order within a dict: R2 (table merge) -> R3 (object merge) -> R1 (strip).
    """
    if isinstance(obj, dict):
        merged = _merge_table_id(obj)
        merged = _merge_object_id(merged)
        out: dict[str, Any] = {}
        for k, v in merged.items():
            v = compact(v)
            if (k in NULL_KEEP and v is None) or (k in EMPTY_KEEP and v in ([], {})):
                out[k] = v
                continue
            if k in NULLABLE_FALSE_KEEP and v is False:
                out[k] = v
                continue
            if _is_falsy_strippable(v):
                continue
            out[k] = v
        return out
    if isinstance(obj, list):
        return [compact(x) for x in obj]
    return obj
