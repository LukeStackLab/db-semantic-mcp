"""Offline database selection contract."""

from mcp.types import Tool

from ..app import register_tool
from ..database_context import _root_context, available_databases, configured_databases


def register():
    register_tool(
        Tool(
            name="list_databases",
            description="List configured, policy-authorized databases. Pass a returned name as database to subsequent tools. Availability does not guarantee connectivity.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            outputSchema={
                "type": "object",
                "properties": {
                    "databases": {"type": "array", "items": {"type": "string"}},
                    "selection_required": {"type": "boolean"},
                },
                "required": ["databases", "selection_required"],
            },
        ),
        _list_databases,
    )


async def _list_databases(arguments):
    root = _root_context()
    return {
        "databases": available_databases(root.cfg, root.policy.current_policy()),
        "selection_required": len(configured_databases(root.cfg)) > 1,
    }
