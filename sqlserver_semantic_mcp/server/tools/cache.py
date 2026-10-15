from mcp.types import Tool

from ...infrastructure.cache import revalidation
from ..app import get_context, register_tool


def register() -> None:
    register_tool(
        Tool(
            name="refresh_schema_cache",
            description="Refresh local structural schema cache from SQL Server.",
            inputSchema={"type": "object", "properties": {}},
        ),
        _refresh,
    )


async def _refresh(args: dict) -> dict:
    ctx = get_context()
    result = await revalidation.refresh_now(ctx.cfg)
    return {"refreshed": True, **result}
