"""Discovery prompts."""
from __future__ import annotations

from mcp.types import (
    GetPromptResult, Prompt, PromptArgument, PromptMessage, TextContent,
)

from .registry import register_prompt


_PROMPT = Prompt(
    name="discover_tables_for_business_question",
    description=(
        "Translate a natural-language question into the shortest discovery "
        "chain — candidates → describe → join path."
    ),
    arguments=[
        PromptArgument(
            name="goal",
            description="Free-form business question.",
            required=True,
        ),
    ],
)


_BODY = """You have a business question but not the target tables. Follow the discovery chain:

1. `discover_relevant_tables(goal={goal!r})` — returns a small ranked candidate set.
2. For the top 2–3 candidates, call `describe_table(detail="standard")`. Use `columns` when the required columns are known. Verify exact column types and complete join_constraints before drafting SQL.
3. If the question joins concepts, call `find_join_path` with `score=true`. Resolve ambiguous roles and use every column_pair in a constraint. A heuristic score is not a correctness guarantee.
4. Draft SQL from verified columns and joins, then call `plan_or_execute_query` with mode="auto". For totals and small result sets, preserve actual values in the response.

Use brief for initial identification and standard for the columns and joins actually used. Request full only for a concrete metadata gap.

Question: {goal}
"""


async def _handler(arguments: dict) -> GetPromptResult:
    goal = arguments.get("goal", "")
    text = _BODY.format(goal=goal)
    return GetPromptResult(
        description="Discovery chain for unknown tables.",
        messages=[
            PromptMessage(
                role="user",
                content=TextContent(type="text", text=text),
            ),
        ],
    )


def register() -> None:
    register_prompt(_PROMPT, _handler)
