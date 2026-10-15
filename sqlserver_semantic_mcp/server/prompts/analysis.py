"""Object / impact analysis prompts."""

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
    name="trace_data_impact",
    description=(
        "Trace the downstream impact of changing a view/procedure/function "
        "without dumping raw SQL bodies into the context."
    ),
    arguments=[
        PromptArgument(name="schema", required=True),
        PromptArgument(name="name", required=True),
        PromptArgument(
            name="type",
            description="VIEW | PROCEDURE | FUNCTION",
            required=True,
        ),
    ],
)


_BODY = """You need to understand the impact of modifying {type} {schema}.{name}. Follow the impact chain:

1. `describe_object(schema={schema!r}, name={name!r}, type={type!r})` — returns reads / writes / depends_on in compact form.
2. For a cheaper repeat look, read the `semantic://summary/object/{type}/{schema}.{name}` resource instead of re-calling the tool.

Only request the full definition (`describe_object(schema={schema!r}, name={name!r}, type={type!r}, include_definition=true)`) if the summary leaves a concrete gap.
"""


async def _handler(arguments: dict) -> GetPromptResult:
    schema = arguments.get("schema", "")
    name = arguments.get("name", "")
    obj_type = (arguments.get("type") or "VIEW").upper()
    text = _BODY.format(schema=schema, name=name, type=obj_type)
    return GetPromptResult(
        description="Impact analysis chain for schema objects.",
        messages=[
            PromptMessage(
                role="user",
                content=TextContent(type="text", text=text),
            ),
        ],
    )


def register() -> None:
    register_prompt(_PROMPT, _handler)
