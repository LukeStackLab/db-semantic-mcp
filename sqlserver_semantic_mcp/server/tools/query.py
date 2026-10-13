from mcp.types import Tool

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
    "description": "summary=columns+count; rows=full page; "
                   "sample=columns+first N; count_only=row_count only.",
}
_AFFECTED_POLICY_PROP = {
    "type": "string", "enum": ["strict", "report"],
    "description": "strict = roll back if affected rows exceed cap; "
                   "report = execute and report exceeded_cap.",
}


def register() -> None:
    register_tool(
        Tool(
            name="plan_or_execute_query",
            description=(
                "v0.5 main entry for SQL-ready agents. mode=auto validates then "
                "executes if safe; mode=validate validates and reports a risk "
                "breakdown without executing; mode=dry_run returns a preview "
                "without side effects. Do not use this for schema discovery — "
                "use discover_relevant_tables first when the target tables are "
                "unknown."
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
    return ctx.workflow.plan_or_execute_query(
        args["query"],
        mode=args.get("mode", "auto"),
        max_rows=args.get("max_rows"),
        return_mode=args.get("return_mode"),
        detail=args.get("detail", "brief"),
        token_budget_hint=args.get("token_budget_hint"),
        affected_rows_policy=args.get("affected_rows_policy"),
    )
