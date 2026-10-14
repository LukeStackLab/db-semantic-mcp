"""Bundle prior tool results into a compact handoff for the next step."""
from __future__ import annotations

from typing import Optional

from ..config import Config, get_config
from ..services import metadata_service, object_service, semantic_service
from ..services.semantic_service import _column_semantic
from .contracts import ToolEnvelope


_JOIN_IMPORTANT = 6


async def _table_summary_for_joining(
    db_path: str, database: str, schema: str, table: str,
    *, policy=None,
) -> Optional[dict]:
    full = await metadata_service.describe_table(db_path, database, schema, table, detail="standard", policy=policy)
    if full is None:
        return None
    cls = full.get("classification") or {}
    pk = full.get("primary_key", []) or []
    fks = full.get("foreign_keys", []) or []

    important: list[str] = []
    seen: set[str] = set()

    def push(name: Optional[str]) -> None:
        if not name or name in seen:
            return
        seen.add(name)
        important.append(name)

    for col in pk:
        push(col)
    for fk in fks:
        push(fk.get("column_name"))
    for c in full.get("columns", []):
        if len(important) >= _JOIN_IMPORTANT:
            break
        sem = _column_semantic(c)
        if sem and sem != "generic":
            push(c["column_name"])
    for c in full.get("columns", []):
        if len(important) >= _JOIN_IMPORTANT:
            break
        push(c["column_name"])

    from ..services.relationship_service import group_foreign_keys
    fk_edges = [{"to_table": f"{fk['ref_schema']}.{fk['ref_table']}",
                 "constraint_name": fk.get("constraint_name"),
                 "column_pairs": fk["column_pairs"],
                 "metadata_complete": fk["metadata_complete"]}
                for fk in group_foreign_keys(fks)]
    required = set(pk) | {pair["from_column"] for fk in fk_edges for pair in fk["column_pairs"]}
    chosen = set(important[:_JOIN_IMPORTANT]) | required
    typed_columns = [{"name": c["column_name"], "type": c.get("sql_type", c.get("data_type")),
                      "is_nullable": bool(c.get("is_nullable"))}
                     for c in full.get("columns", []) if c["column_name"] in chosen]

    return {
        "table": f"{schema}.{table}",
        "classification": cls.get("type", "unknown"),
        "pk": list(pk),
        "important_columns": important[:_JOIN_IMPORTANT],
        "fk_edges": fk_edges,
        "columns": typed_columns,
        "columns_omitted": len(full.get("columns", [])) > len(typed_columns),
    }


async def _object_summary_for_impact(
    schema: str,
    object_name: str,
    object_type: str,
    cfg: Config,
    *, policy=None,
) -> Optional[dict]:
    obj = await object_service.describe_object(
        schema, object_name, object_type, cfg, policy=policy,
    )
    if not obj:
        return None
    return {
        "object": f"{schema}.{object_name}",
        "type": object_type,
        "reads": list(obj.get("read_tables", []) or []),
        "writes": list(obj.get("write_tables", []) or []),
        "depends_on": list(obj.get("dependencies", []) or []),
        "status": obj.get("status"),
        **{key: obj[key] for key in ("dependencies_omitted", "definition_withheld", "definition_omission_reason") if key in obj},
    }


async def bundle_context_for_next_step(
    items: list[dict],
    *,
    goal: str = "joining",
    detail: str = "brief",
    cfg: Optional[Config] = None,
    policy=None,
) -> dict:
    """Compress prior discoveries into the minimum context the next
    tool needs. Supported goals: ``joining``, ``object_impact``.
    """
    cfg = cfg or get_config()
    if policy is None:
        from ..services.policy_service import PolicyService
        policy = PolicyService(cfg)
    db_path = cfg.cache_path
    database = cfg.mssql_database

    if goal == "joining":
        tables: list[dict] = []
        for item in items or []:
            if item.get("kind") != "table":
                continue
            schema = item["schema"]
            table = item["table"]
            summary = await _table_summary_for_joining(
                db_path, database, schema, table, policy=policy,
            )
            if summary is not None:
                tables.append(summary)
        return ToolEnvelope(
            kind="bundle_context_for_next_step",
            detail=detail,
            next_action="find_or_score_join",
            recommended_tool="find_join_path",
            bundle_key="joining",
            data={
                "bundle_type": "joining",
                "tables": tables,
            },
        ).to_dict()

    if goal == "object_impact":
        objects: list[dict] = []
        for item in items or []:
            if item.get("kind") != "object":
                continue
            summary = await _object_summary_for_impact(
                item["schema"], item["object_name"], item["object_type"], cfg, policy=policy,
            )
            if summary is not None:
                objects.append(summary)
        return ToolEnvelope(
            kind="bundle_context_for_next_step",
            detail=detail,
            next_action="trace_impact",
            recommended_tool="describe_object",
            bundle_key="object_impact",
            data={
                "bundle_type": "object_impact",
                "objects": objects,
            },
        ).to_dict()

    return ToolEnvelope(
        kind="bundle_context_for_next_step",
        detail=detail,
        next_action="none",
        data={
            "bundle_type": goal,
            "error": f"unsupported goal '{goal}'",
            "supported_goals": ["joining", "object_impact"],
        },
    ).to_dict()
