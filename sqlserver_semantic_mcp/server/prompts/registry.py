"""Prompt registry wired up against ``server.app.app``."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from mcp.types import GetPromptResult, Prompt, PromptArgument

from ..app import app, _TOOL_REGISTRY


Handler = Callable[[dict[str, Any]], Awaitable[GetPromptResult]]
_REGISTRY: dict[str, tuple[Prompt, Handler]] = {}
_REQUIRED_TOOLS = {
    "safe_sql_execution": {"plan_or_execute_query"},
    "discover_tables_for_business_question": {"discover_relevant_tables", "describe_table", "find_join_path", "plan_or_execute_query"},
    "trace_data_impact": {"describe_object"},
}


def _enabled(name: str) -> bool:
    return _REQUIRED_TOOLS.get(name, set()).issubset(_TOOL_REGISTRY)


def register_prompt(prompt: Prompt, handler: Handler) -> None:
    _REGISTRY[prompt.name] = (prompt, handler)


@app.list_prompts()
async def _list_prompts() -> list[Prompt]:
    return [p for (p, _) in _REGISTRY.values() if _enabled(p.name)]


@app.get_prompt()
async def _get_prompt(name: str, arguments: dict | None = None) -> GetPromptResult:
    if name not in _REGISTRY or not _enabled(name):
        raise ValueError(f"Unknown prompt: {name}")
    _p, handler = _REGISTRY[name]
    return await handler(arguments or {})


def register_prompts() -> None:
    """Import prompt modules to trigger their registrations."""
    from . import execution, discovery, analysis  # noqa: F401

    execution.register()
    discovery.register()
    analysis.register()


__all__ = ["PromptArgument", "Prompt", "register_prompt", "register_prompts"]
