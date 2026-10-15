from mcp.types import Tool

from ...infrastructure.workers import run_db_work
from ..app import get_context, register_tool

_AFFECTED_POLICY_PROP = {
    "type": "string",
    "enum": ["strict", "report"],
    "description": "Over-cap writes: strict rolls back; report commits. Fixed profiles always use strict.",
}


def register() -> None:
    register_tool(
        Tool(
            name="plan_or_execute_query",
            description=(
                "Run policy-approved T-SQL. auto/execute_if_safe execute; validate/dry_run inspect only. "
                "SELECT output is capped; check truncated."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": _required_query(),
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "validate", "dry_run", "execute_if_safe"],
                        "default": "auto",
                    },
                    "max_rows": {"type": "integer", "minimum": 1},
                    "affected_rows_policy": _AFFECTED_POLICY_PROP,
                    "validate_result_schema": {
                        "type": "boolean",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
        ),
        _plan_or_execute,
    )


def _required_query() -> dict:
    return {"type": "string", "description": "T-SQL statement."}


async def _plan_or_execute(args: dict) -> dict:
    ctx = get_context()
    return await run_db_work(
        ctx.cfg,
        ctx.workflow.plan_or_execute_query,
        args["query"],
        mode=args.get("mode", "auto"),
        max_rows=args.get("max_rows"),
        affected_rows_policy=args.get("affected_rows_policy"),
        validate_result_schema=args.get("validate_result_schema", False),
    )
