from mcp.types import Tool

from ...services import metadata_service, semantic_service
from ..app import get_context, register_tool
from ._shared import normalize_schema_filter


def _project_describe_table(full: dict, column_semantics: dict[str, str], max_columns: int) -> dict:
    schema = full.get("schema_name", "")
    table = full.get("table_name", "")
    columns = full.get("columns", [])
    fks = full.get("foreign_keys", []) or []
    shown = columns[:max_columns]
    cols = []
    for c in shown:
        cols.append(
            {
                "name": c["column_name"],
                "type": c.get("sql_type", c.get("data_type")),
                "is_nullable": bool(c.get("is_nullable")),
                "max_length": c.get("max_length"),
                "numeric_precision": c.get("numeric_precision"),
                "numeric_scale": c.get("numeric_scale"),
                "datetime_precision": c.get("datetime_precision"),
                "collation": c.get("collation_name"),
                "default_value": c.get("default_value"),
                "description": c.get("description"),
                "semantic": column_semantics.get(c["column_name"]) or "generic",
            }
        )
    out: dict = {
        "table": f"{schema}.{table}",
        "column_count": full.get("column_count", len(columns)),
        "columns_omitted": bool(full.get("columns_omitted")) or len(shown) < len(columns),
        "pk": list(full.get("primary_key", []) or []),
        "classification": (full.get("classification") or {}).get("type", "unknown"),
        "columns": cols,
        "foreign_keys": list(fks),
        "join_constraints": full.get("join_constraints", []),
        "required_join_columns": full.get("required_join_columns", []),
        "indexes": full.get("indexes", []),
        "description": full.get("description"),
    }
    if full.get("query_reference"):
        from ...services.oracle_metadata import qualified

        out["table"] = qualified(schema, table)
    out.update({key: full[key] for key in ("chain", "target", "canonical_source", "query_reference") if key in full})
    if out["columns_omitted"] and len(shown) < len(columns):
        out["columns_hint"] = "Column list capped; pass `columns` to select specific columns."
    if full.get("cache"):
        out["cache"] = full["cache"]
    if full.get("structural_hash"):
        out["schema_version"] = full["structural_hash"]
    return out


def register() -> None:
    register_tool(
        Tool(
            name="get_tables",
            description="List tables in the database. Supports schema, keyword, and limit filters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "schema": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                    "keyword": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1},
                },
            },
        ),
        _get_tables,
    )
    register_tool(
        Tool(
            name="describe_table",
            description="Table columns with SQL types, keys, joins, indexes and descriptions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "schema": {"type": "string"},
                    "table": {"type": "string"},
                    "columns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                },
                "required": ["schema", "table"],
            },
        ),
        _describe_table,
    )


def _resolve_list_limit(args: dict, default: int) -> int:
    explicit = args.get("limit")
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    return default


async def _get_tables(args: dict) -> list[dict]:
    ctx = get_context()
    schemas = normalize_schema_filter(args.get("schema"))
    keyword = args.get("keyword") or None
    cap = _resolve_list_limit(args, ctx.cfg.max_list_items)
    rows = await metadata_service.list_tables(
        ctx.cfg.cache_path,
        ctx.cfg.database,
        schemas=schemas,
        keyword=keyword,
        limit=cap,
        policy=ctx.policy,
    )
    return rows[:cap]


async def _describe_table(args: dict) -> dict | None:
    ctx = get_context()
    full = await metadata_service.describe_table(
        ctx.cfg.cache_path,
        ctx.cfg.database,
        args["schema"],
        args["table"],
        columns=args.get("columns"),
        policy=ctx.policy,
    )
    if full is None:
        return None
    semantic_map = {
        c["column_name"]: (semantic_service._column_semantic(c) or "generic") for c in full.get("columns", [])
    }
    return _project_describe_table(full, semantic_map, ctx.cfg.max_list_items)
