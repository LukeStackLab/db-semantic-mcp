import json
import re
from typing import Optional
import aiosqlite

from ..infrastructure.cache.semantic import (
    upsert_table_analysis, get_table_analysis,
)


_AUDIT_COL_PATTERNS = {
    "audit_timestamp": re.compile(
        r"^(created|updated|modified|deleted)(_?at|_?on|_?time|_?date)?$", re.I),
    "audit_user":      re.compile(
        r"^(created|updated|modified|deleted)_?by$", re.I),
    "soft_delete":     re.compile(r"^(is_)?deleted$|^deleted_at$", re.I),
    "status":          re.compile(r"^(status|state)(_?code|_?id)?$", re.I),
    "type":            re.compile(r"^(type|category|kind)(_?code|_?id)?$", re.I),
}

_LOOKUP_NAME_COLS = {"code", "name", "label", "description", "value"}

_COMMON_FILTER_SEMANTICS = frozenset({
    "status", "type", "audit_timestamp", "soft_delete",
})


async def _load_table_structure(
    db_path: str, database: str, schema: str, table: str,
) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT structural_hash FROM sc_tables WHERE database_name=? "
            "AND schema_name=? AND table_name=?",
            (database, schema, table),
        )
        row = await cur.fetchone()
        if not row:
            return None
        table_hash = row["structural_hash"] or ""

        cur = await db.execute(
            "SELECT column_name, data_type, max_length, is_nullable, ordinal_position "
            "FROM sc_columns WHERE database_name=? AND schema_name=? AND table_name=? "
            "ORDER BY ordinal_position",
            (database, schema, table),
        )
        columns = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute(
            "SELECT column_name FROM sc_primary_keys "
            "WHERE database_name=? AND schema_name=? AND table_name=?",
            (database, schema, table),
        )
        pk = [r["column_name"] for r in await cur.fetchall()]

        cur = await db.execute(
            "SELECT * FROM sc_foreign_keys "
            "WHERE database_name=? AND schema_name=? AND table_name=?",
            (database, schema, table),
        )
        fks = [dict(r) for r in await cur.fetchall()]

    return {"columns": columns, "primary_key": pk, "foreign_keys": fks,
            "structural_hash": table_hash}


def _column_semantic(col: dict) -> Optional[str]:
    name = col["column_name"]
    for sem, pat in _AUDIT_COL_PATTERNS.items():
        if pat.match(name):
            return sem
    return None


def _classify_rules(struct: dict, table: str) -> dict:
    cols = struct["columns"]
    fks = struct["foreign_keys"]
    from .relationship_service import group_foreign_keys
    fk_count = len(group_foreign_keys(fks))
    col_names = [c["column_name"].lower() for c in cols]

    reasons: list[str] = []

    # Audit heuristic
    audit_like = sum(1 for c in cols if _column_semantic(c) in (
        "audit_timestamp", "audit_user"))
    if audit_like >= 2 and len(cols) <= 6:
        reasons.append(f"{audit_like} audit-style columns dominate")
        return {"type": "audit", "confidence": 0.75, "reasons": reasons}

    # Bridge: 2+ FKs and almost all columns are FKs
    if fk_count >= 2 and len(cols) <= len(fks) + 2:
        reasons.append(f"{fk_count} FK constraints over {len(cols)} columns")
        return {"type": "bridge", "confidence": 0.8, "reasons": reasons}

    # Fact: >= 2 FKs
    if fk_count >= 2:
        reasons.append(f"{fk_count} FK constraints")
        return {"type": "fact", "confidence": 0.7, "reasons": reasons}

    # Lookup: few columns, contains code/name-like column names
    small = len(cols) <= 4
    lookup_cols = sum(1 for n in col_names if n in _LOOKUP_NAME_COLS)
    name_suggests_lookup = bool(re.search(
        r"(status|code|type|category|kind|lookup)$", table, re.I,
    ))
    if small and (lookup_cols >= 2 or name_suggests_lookup):
        reasons.append("small row width + lookup-like columns/name")
        return {"type": "lookup", "confidence": 0.75, "reasons": reasons}

    # Dimension fallback
    if fk_count <= 1 and len(cols) >= 3:
        reasons.append("few FKs with multiple descriptive columns")
        return {"type": "dimension", "confidence": 0.5, "reasons": reasons}

    return {"type": "unknown", "confidence": 0.2,
            "reasons": reasons or ["no rule matched"]}


def _classify(struct: dict, table: str) -> dict:
    return {**_classify_rules(struct, table), "confidence_kind": "heuristic",
            "algorithm": "constraints-v1"}


async def classify_table(
    db_path: str, database: str, schema: str, table: str,
    *, force: bool = False, policy=None,
) -> dict:
    if policy is not None:
        policy.authorize_object(schema, table, database)
    struct = await _load_table_structure(db_path, database, schema, table)
    if struct is None:
        return {"type": "unknown", "confidence": 0.0,
                "reasons": ["table not found"]}
    structural_hash = struct["structural_hash"]

    if not force:
        cached = await get_table_analysis(db_path, database, schema, table)
        if cached and cached["status"] == "ready" \
                and cached.get("structural_hash") == structural_hash \
                and (cached.get("classification") or {}).get("algorithm") == "constraints-v1":
            return cached["classification"]

    classification = _classify(struct, table)
    column_analysis = [
        {"column": c["column_name"],
         "semantic_type": _column_semantic(c) or "generic"}
        for c in struct["columns"]
    ]
    is_lookup = classification["type"] == "lookup"

    await upsert_table_analysis(
        db_path, database, schema, table,
        structural_hash=structural_hash, status="ready",
        classification=classification,
        column_analysis=column_analysis,
        is_lookup=is_lookup,
    )
    return classification


async def analyze_columns(
    db_path: str, database: str, schema: str, table: str,
    *, policy=None,
) -> list[dict]:
    await classify_table(db_path, database, schema, table, policy=policy)
    cached = await get_table_analysis(db_path, database, schema, table)
    return cached.get("column_analysis", []) if cached else []


async def summarize_for_joining(
    db_path: str, database: str, schema: str, table: str,
    *, policy=None,
) -> Optional[dict]:
    """Return reasoning-ready info for joining against this table.

    Shape: {table, pk, classification, join_candidates, common_filter_columns}
    """
    if policy is not None:
        policy.authorize_object(schema, table, database)
    struct = await _load_table_structure(db_path, database, schema, table)
    if struct is None:
        return None

    from .relationship_service import group_foreign_keys
    groups = group_foreign_keys([fk for fk in struct["foreign_keys"]
                                if policy is None or policy.object_allowed(fk["ref_schema"], fk["ref_table"], database)])
    join_candidates = []
    for fk in groups:
        candidate = {"to_table": f"{fk['ref_schema']}.{fk['ref_table']}",
                     "column_pairs": fk["column_pairs"],
                     "constraint_name": fk.get("constraint_name"),
                     "metadata_complete": fk["metadata_complete"]}
        if len(fk["column_pairs"]) == 1:
            candidate["via_column"] = fk["column_pairs"][0]["from_column"]
            candidate["to_column"] = fk["column_pairs"][0]["to_column"]
        join_candidates.append(candidate)

    common_filter_columns = [
        c["column_name"] for c in struct["columns"]
        if (_column_semantic(c) or "") in _COMMON_FILTER_SEMANTICS
    ]

    classification = _classify(struct, table)

    return {
        "table": f"{schema}.{table}",
        "pk": struct["primary_key"],
        "classification": classification["type"],
        "join_candidates": join_candidates,
        "common_filter_columns": common_filter_columns,
    }


async def detect_lookup_tables(
    db_path: str, database: str, *,
    schemas: Optional[list[str]] = None,
    keyword: Optional[str] = None,
    confidence_min: float = 0.0,
    policy=None,
) -> list[dict]:
    results: list[dict] = []
    need_classify: list[tuple[str, str]] = []
    kw_lower = keyword.lower() if keyword else None
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT t.schema_name,t.table_name,t.structural_hash,a.status,"
            "a.structural_hash AS analysis_hash,a.classification "
            "FROM sc_tables t LEFT JOIN sem_table_analysis a "
            "ON a.database_name=t.database_name AND a.schema_name=t.schema_name AND a.table_name=t.table_name "
            "WHERE t.database_name=? ORDER BY t.schema_name,t.table_name", (database,))
        for row in await cur.fetchall():
            schema, table = row["schema_name"], row["table_name"]
            if schemas and schema not in schemas:
                continue
            if kw_lower and kw_lower not in f"{schema}.{table}".lower():
                continue
            if policy is not None and not policy.object_allowed(schema, table, database):
                continue
            try:
                classification = json.loads(row["classification"] or "null")
            except (ValueError, TypeError):
                classification = None
            ready = (row["status"] == "ready" and row["analysis_hash"] == row["structural_hash"]
                     and isinstance(classification, dict) and classification.get("algorithm") == "constraints-v1")
            if not ready:
                need_classify.append((schema, table))
            elif classification.get("type") == "lookup" and classification.get("confidence", 0) >= confidence_min:
                results.append({"schema_name": schema, "table_name": table, "confidence": classification["confidence"]})
    for schema, table in need_classify:
        kwargs = {"policy": policy} if policy is not None else {}
        classification = await classify_table(db_path, database, schema, table, **kwargs)
        if classification.get("type") == "lookup" and classification.get("confidence", 0) >= confidence_min:
            results.append({"schema_name": schema, "table_name": table, "confidence": classification["confidence"]})
    return results
