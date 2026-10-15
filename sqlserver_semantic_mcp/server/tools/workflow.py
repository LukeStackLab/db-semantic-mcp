"""Workflow-layer MCP tools."""

from __future__ import annotations

from mcp.types import Tool

from ..app import get_context, register_tool


def register() -> None:
    register_tool(
        Tool(
            name="discover_relevant_tables",
            description="Find tables for a natural-language goal or keyword.",
            inputSchema={
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "schemas": {"type": "array", "items": {"type": "string"}},
                    "keyword": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "default": 10},
                    "classify": {"type": "boolean", "default": False},
                },
                "required": ["goal"],
            },
        ),
        _discover,
    )


# ---- handlers ---------------------------------------------------------------


async def _discover(args: dict) -> dict:
    ctx = get_context()
    schemas = args.get("schemas") or None
    return await ctx.workflow.discover_relevant_tables(
        args["goal"],
        schemas=schemas,
        keyword=args.get("keyword"),
        limit=int(args.get("limit", 10)),
        classify=bool(args.get("classify", False)),
    )
