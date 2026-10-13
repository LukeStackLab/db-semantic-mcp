from typing import Optional

from mcp.types import Tool

from ...services import metadata_service, semantic_service
from ..app import get_context, register_tool
from .shape import (
    project_describe_table, resolve_detail,
)


_DETAIL_PROP = {
    "type": "string", "enum": ["brief", "standard", "full"],
    "default": "brief",
    "description": "Response verbosity. brief = minimal identification + counts; "
                   "standard = columns+FKs; full = indexes+descriptions.",
}


_BUDGET_PROP = {
    "type": "string", "enum": ["tiny", "low", "medium", "high"],
    "description": "Optional override. Omit to use the server budget "
                   "(medium by default).",
}


_BUDGET_LIMITS = {
    "tiny":   20,
    "low":    100,
    "medium": 500,
    "high":   2000,
}


def register() -> None:
    register_tool(
        Tool(
            name="get_tables",
            description=(
                "List tables in the database. Supports schema / keyword filters "
                "so the response stays small on large DBs. Pass limit or "
                "token_budget_hint to further cap the payload."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "schema":  {"oneOf": [{"type": "string"},
                                          {"type": "array",
                                           "items": {"type": "string"}}]},
                    "keyword": {"type": "string"},
                    "limit":   {"type": "integer", "minimum": 1},
                    "token_budget_hint": _BUDGET_PROP,
                },
            },
        ),
        _get_tables,
    )
    register_tool(
        Tool(
            name="describe_table",
            description=(
                "Return table metadata. detail=brief (default) returns a compact "
                "summary {table, column_count, pk, fk_to, important_columns, "
                "classification}. standard adds columns+FKs; full adds indexes "
                "and descriptions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "schema": {"type": "string"},
                    "table":  {"type": "string"},
                    "detail": _DETAIL_PROP,
                },
                "required": ["schema", "table"],
            },
        ),
        _describe_table,
    )


def _normalize_schema_filter(raw) -> Optional[list[str]]:
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw] if raw else None
    if isinstance(raw, list):
        vals = [s for s in raw if isinstance(s, str) and s]
        return vals or None
    return None


def _resolve_list_limit(args: dict, cfg_default: str) -> int | None:
    explicit = args.get("limit")
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    hint = args.get("token_budget_hint") or cfg_default
    return _BUDGET_LIMITS.get(hint)


async def _get_tables(args: dict) -> list[dict]:
    ctx = get_context()
    schemas = _normalize_schema_filter(args.get("schema"))
    keyword = args.get("keyword") or None
    rows = await metadata_service.list_tables(
        ctx.cfg.cache_path, ctx.cfg.mssql_database,
        schemas=schemas, keyword=keyword,
    )
    cap = _resolve_list_limit(args, ctx.cfg.default_token_budget_hint)
    if cap is not None and len(rows) > cap:
        return rows[:cap]
    return rows


async def _describe_table(args: dict) -> Optional[dict]:
    ctx = get_context()
    detail = resolve_detail(args)
    cp = ctx.cfg.cache_path
    db = ctx.cfg.mssql_database
    schema = args["schema"]
    table = args["table"]

    full = await metadata_service.describe_table(cp, db, schema, table)
    if full is None:
        return None

    classification = await semantic_service.classify_table(cp, db, schema, table)
    semantic_map = {
        c["column_name"]: (semantic_service._column_semantic(c) or "generic")
        for c in full.get("columns", [])
    }
    return project_describe_table(
        full, detail=detail,
        classification=classification, column_semantics=semantic_map,
    )
