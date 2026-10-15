"""Prompts for the direct-execution path."""

from __future__ import annotations

from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    TextContent,
)

from .registry import register_prompt

_PROMPT = Prompt(
    name="safe_sql_execution",
    description="Execute an agent-authored SQL in the shortest safe path via plan_or_execute_query.",
    arguments=[
        PromptArgument(
            name="query",
            description="SQL the agent already believes is ready to run.",
            required=True,
        ),
    ],
)


_BODY = """You have a SQL query already drafted. Prefer the shortest safe path:

1. Call `plan_or_execute_query` with mode="auto".
2. If the response has `path="direct_execute"` and `executed=true`, you are done — present the rows as-is. If `truncated` is true, narrow the query (fewer columns, WHERE, TOP, or aggregate) instead of asking for more rows.
3. If the response has `path="direct_validate"` and `allowed=false`, read `reason` and either:
   - revise the SQL, or
   - call `plan_or_execute_query` with mode="validate" for a fuller risk breakdown before revising.
4. When the SQL and its schema are already verified, execute directly. If schema evidence is missing, switch to a profile that provides discovery before drafting SQL.

Query:
```sql
{query}
```
"""


async def _handler(arguments: dict) -> GetPromptResult:
    query = arguments.get("query", "")
    text = _BODY.format(query=query)
    return GetPromptResult(
        description="Shortest safe path for SQL-ready agents.",
        messages=[
            PromptMessage(
                role="user",
                content=TextContent(type="text", text=text),
            ),
        ],
    )


def register() -> None:
    register_prompt(_PROMPT, _handler)
