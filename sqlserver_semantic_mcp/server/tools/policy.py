from mcp.types import Tool

from ..app import get_context, register_tool


def register() -> None:
    register_tool(
        Tool(
            name="get_execution_policy",
            description="Return active policy rules and constraints. reload=true re-reads disk file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "reload": {"type": "boolean", "default": False},
                },
            },
        ),
        _get_policy,
    )


async def _get_policy(args: dict) -> dict:
    ctx = get_context()
    if args.get("reload"):
        ctx.policy.reload()
        pol = ctx.policy.current_policy()
        return {"reloaded": True, "profile": pol.profile_name, **pol.model_dump()}
    return ctx.policy.current_policy().model_dump()
