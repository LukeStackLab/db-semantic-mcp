from mcp.types import Tool

from ...infrastructure.workers import run_db_work
from ..app import get_context, register_tool


_DETAIL_PROP = {
    "type": "string", "enum": ["brief", "standard", "full"], "default": "brief",
}
_BUDGET_PROP = {
    "type": "string", "enum": ["tiny", "low", "medium", "high"],
    "description": "Optional override. Omit to use the server budget "
                   "(medium by default).",
}
_RESPONSE_MODE_PROP = {
    "type": "string", "enum": ["summary", "rows", "sample", "count_only"],
    "description": "summary=columns+up to 3 preview rows; rows=full page; "
                   "sample=budget-limited preview; count_only=bounded count. "
                   "total_row_count is null unless the result is complete.",
}
_AFFECTED_POLICY_PROP = {
    "type": "string", "enum": ["strict", "report"],
    "description": "strict = roll back if affected rows exceed cap; "
                   "report = report exceeded_cap only when server strict mode is disabled.",
}


def register() -> None:
    register_tool(
        Tool(
            name="plan_or_execute_query",
            description=(
                "Validate T-SQL and execute one policy-approved statement. "
                "auto executes; validate/dry_run inspect without execution. "
                "Discover tables and columns first when unknown."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query":                _required_query(),
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "validate", "dry_run",
                                 "execute_if_safe"],
                        "default": "auto",
                    },
                    "max_rows":             {"type": "integer", "minimum": 1},
                    "return_mode":          _RESPONSE_MODE_PROP,
                    "detail":               _DETAIL_PROP,
                    "token_budget_hint":    _BUDGET_PROP,
                    "affected_rows_policy": _AFFECTED_POLICY_PROP,
                    "validate_result_schema": {
                        "type": "boolean", "default": False,
                        "description": "Opt in to SQL Server first-result-set metadata validation before execution.",
                    },
                },
                "required": ["query"],
            },
        ),
        _plan_or_execute,
    )


def _required_query() -> dict:
    return {"type": "string", "description": "SQL to execute / validate."}


async def _plan_or_execute(args: dict) -> dict:
    ctx = get_context()
    return await run_db_work(
        ctx.cfg, ctx.workflow.plan_or_execute_query,
        args["query"],
        mode=args.get("mode", "auto"),
        max_rows=args.get("max_rows"),
        return_mode=args.get("return_mode"),
        detail=args.get("detail", "brief"),
        token_budget_hint=args.get("token_budget_hint"),
        affected_rows_policy=args.get("affected_rows_policy"),
        validate_result_schema=args.get("validate_result_schema", False),
    )
