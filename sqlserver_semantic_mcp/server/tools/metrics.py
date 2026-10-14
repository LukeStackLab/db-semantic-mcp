from mcp.types import Tool

from ...services import metrics_service
from ..app import get_context, register_tool


def register() -> None:
    register_tool(
        Tool(
            name="tool_metrics",
            description=(
                "action=get returns per-tool payload metrics (call_count, "
                "total_bytes, avg_bytes, p95_bytes, max_bytes) ordered by "
                "total_bytes desc, plus p50/p95 elapsed_ms, worker/DB timing, "
                "observed cache hits and error_count. Bytes are not token counts. action=reset deletes all "
                "recorded metrics and returns the deleted count."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["get", "reset"],
                        "default": "get",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
                },
            },
        ),
        _metrics,
    )


async def _metrics(args: dict):
    ctx = get_context()
    if args.get("action") == "reset":
        n = await metrics_service.clear_metrics(ctx.cfg.cache_path)
        return {"deleted": n}
    return await metrics_service.query_top_tools(
        ctx.cfg.cache_path, limit=int(args.get("limit", 10)),
    )
