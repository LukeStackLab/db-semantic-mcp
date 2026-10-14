from typing import Optional

from mcp.types import Tool

from ...services import semantic_service
from ..app import get_context, register_tool


def register() -> None:
    register_tool(
        Tool(
            name="detect_lookup_tables",
            description=(
                "Scan DB and return likely lookup tables. Supports schema / "
                "keyword / confidence_min filters to limit the sweep."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "schema":         {"oneOf": [{"type": "string"},
                                                 {"type": "array",
                                                  "items": {"type": "string"}}]},
                    "keyword":        {"type": "string"},
                    "confidence_min": {"type": "number",
                                       "minimum": 0.0, "maximum": 1.0,
                                       "default": 0.0},
                },
            },
        ),
        _lookups,
    )


def _normalize_schema_filter(raw) -> Optional[list[str]]:
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw] if raw else None
    if isinstance(raw, list):
        vals = [s for s in raw if isinstance(s, str) and s]
        return vals or None
    return None


async def _lookups(args: dict) -> list[dict]:
    ctx = get_context()
    schemas = _normalize_schema_filter(args.get("schema"))
    keyword = args.get("keyword") or None
    confidence_min = float(args.get("confidence_min", 0.0))
    return await semantic_service.detect_lookup_tables(
        ctx.cfg.cache_path, ctx.cfg.mssql_database,
        schemas=schemas, keyword=keyword, confidence_min=confidence_min, policy=ctx.policy,
    )
