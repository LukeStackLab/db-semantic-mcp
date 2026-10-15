from mcp.types import Tool

from ...services import semantic_service
from ..app import get_context, register_tool
from ._shared import normalize_schema_filter


def register() -> None:
    register_tool(
        Tool(
            name="detect_lookup_tables",
            description="Detect likely lookup tables with optional schema, keyword, or confidence filters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "schema": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                    "keyword": {"type": "string"},
                    "confidence_min": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.0},
                },
            },
        ),
        _lookups,
    )


async def _lookups(args: dict) -> list[dict]:
    ctx = get_context()
    schemas = normalize_schema_filter(args.get("schema"))
    keyword = args.get("keyword") or None
    confidence_min = float(args.get("confidence_min", 0.0))
    return await semantic_service.detect_lookup_tables(
        ctx.cfg.cache_path,
        ctx.cfg.database,
        schemas=schemas,
        keyword=keyword,
        confidence_min=confidence_min,
        policy=ctx.policy,
    )
