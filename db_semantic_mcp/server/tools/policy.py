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
    if args.get("reload") and ctx.cfg._policy_snapshot is None:
        ctx.policy.reload()
    sources = [{"id": "local", "database_type": ctx.cfg.database_type}]
    policy = ctx.policy.current_policy()
    if policy.operations.select:
        sources.extend(
            {
                "id": name,
                "db_link": route.db_link,
                "readonly": route.readonly,
                "query_hint": 'Use native "OWNER"."OBJECT"@' + route.db_link,
            }
            for name, route in ctx.cfg.sources.items()
            if name in policy.scope.sources and policy.scope.sources[name].operations.select
        )
    if args.get("reload"):
        pol = ctx.policy.current_policy()
        return {"reloaded": True, "profile": pol.profile_name, **pol.model_dump(), "available_sources": sources}
    return {**ctx.policy.current_policy().model_dump(), "available_sources": sources}
