import json
import re

import aiosqlite

from ..infrastructure.cache.semantic import (
    get_table_analysis,
    upsert_table_analysis,
)
from ..infrastructure.recording.sqlite import recording_connect

_AUDIT_COL_PATTERNS = {
    "audit_timestamp": re.compile(r"^(created|updated|modified|deleted)(_?at|_?on|_?time|_?date)?$", re.I),
    "audit_user": re.compile(r"^(created|updated|modified|deleted)_?by$", re.I),
    "soft_delete": re.compile(r"^(is_)?deleted$|^deleted_at$", re.I),
    "status": re.compile(r"^(status|state)(_?code|_?id)?$", re.I),
    "type": re.compile(r"^(type|category|kind)(_?code|_?id)?$", re.I),
}

_LOOKUP_NAME_COLS = {"code", "name", "label", "description", "value"}

_COMMON_FILTER_SEMANTICS = frozenset(
    {
        "status",
        "type",
        "audit_timestamp",
        "soft_delete",
    }
)


async def _load_table_structure(
    db_path: str,
    database: str,
    schema: str,
    table: str,
) -> dict | None:
    async with recording_connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT structural_hash FROM sc_tables WHERE database_name=? AND schema_name=? AND table_name=?",
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
            "SELECT column_name FROM sc_primary_keys WHERE database_name=? AND schema_name=? AND table_name=?",
            (database, schema, table),
        )
        pk = [r["column_name"] for r in await cur.fetchall()]

        cur = await db.execute(
            "SELECT * FROM sc_foreign_keys WHERE database_name=? AND schema_name=? AND table_name=?",
            (database, schema, table),
        )
        fks = [dict(r) for r in await cur.fetchall()]

    return {"columns": columns, "primary_key": pk, "foreign_keys": fks, "structural_hash": table_hash}


def _column_semantic(col: dict) -> str | None:
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
    audit_like = sum(1 for c in cols if _column_semantic(c) in ("audit_timestamp", "audit_user"))
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
    name_suggests_lookup = bool(
        re.search(
            r"(status|code|type|category|kind|lookup)$",
            table,
            re.I,
        )
    )
    if small and (lookup_cols >= 2 or name_suggests_lookup):
        reasons.append("small row width + lookup-like columns/name")
        return {"type": "lookup", "confidence": 0.75, "reasons": reasons}

    # Dimension fallback
    if fk_count <= 1 and len(cols) >= 3:
        reasons.append("few FKs with multiple descriptive columns")
        return {"type": "dimension", "confidence": 0.5, "reasons": reasons}

    return {"type": "unknown", "confidence": 0.2, "reasons": reasons or ["no rule matched"]}


def _classify(struct: dict, table: str) -> dict:
    return {**_classify_rules(struct, table), "confidence_kind": "heuristic", "algorithm": "constraints-v1"}


def _classification_cache_allowed(policy) -> bool:
    """Shared analyses describe all FK targets in this database.

    Database-only scope does not change local FK visibility after the source
    is authorized. Unknown policy adapters conservatively bypass the cache.
    """
    if policy is None:
        return True
    current_policy = getattr(policy, "current_policy", None)
    if current_policy is None:
        return False
    scope = current_policy().scope
    return not (scope.allowed_schemas or scope.allowed_tables or scope.denied_tables)


def _authorized_structure(struct: dict, database: str, policy=None) -> dict:
    return {
        **struct,
        "foreign_keys": [
            fk
            for fk in struct["foreign_keys"]
            if policy is None or policy.object_allowed(fk["ref_schema"], fk["ref_table"], database)
        ],
    }


def _cached_classification(cached, structural_hash: str, *, policy=None) -> dict | None:
    if not _classification_cache_allowed(policy) or not cached:
        return None
    classification = cached.get("classification")
    if isinstance(classification, str):
        try:
            classification = json.loads(classification)
        except (ValueError, TypeError):
            return None
    if (
        cached.get("status") == "ready"
        and cached.get("structural_hash") == structural_hash
        and isinstance(classification, dict)
        and classification.get("algorithm") == "constraints-v1"
    ):
        return classification
    return None


def _classification_for_structure(struct: dict, table: str, database: str, *, policy=None, cached=None) -> dict:
    classification = _cached_classification(cached, struct.get("structural_hash", ""), policy=policy)
    if classification is not None:
        return classification
    return _classify(_authorized_structure(struct, database, policy), table)


def _analyze_column_structure(struct: dict) -> list[dict]:
    return [{"column": c["column_name"], "semantic_type": _column_semantic(c) or "generic"} for c in struct["columns"]]


def _oracle_policy(policy) -> bool:
    return getattr(getattr(policy, "_cfg", None), "database_type", None) == "oracle"


async def _oracle_structure(policy, schema, table):
    from .oracle_metadata import describe_table

    return await describe_table(schema, table, policy=policy)


async def classify_table(
    db_path: str,
    database: str,
    schema: str,
    table: str,
    *,
    force: bool = False,
    policy=None,
) -> dict:
    if _oracle_policy(policy):
        struct = await _oracle_structure(policy, schema, table)
        # Live target metadata already filters FKs using its canonical source
        # policy. Do not filter those targets again using the alias's scope.
        return _classify(struct, table)
    if policy is not None:
        policy.authorize_object(schema, table, database)
    struct = await _load_table_structure(db_path, database, schema, table)
    if policy is not None:
        policy.authorize_object(schema, table, database)
    if struct is None:
        return {"type": "unknown", "confidence": 0.0, "reasons": ["table not found"]}
    structural_hash = struct["structural_hash"]

    cache_allowed = _classification_cache_allowed(policy)
    if not force and cache_allowed:
        cached = await get_table_analysis(db_path, database, schema, table)
        if policy is not None:
            policy.authorize_object(schema, table, database)
        classification = _cached_classification(cached, structural_hash, policy=policy)
        if classification is not None:
            return classification

    classification = _classification_for_structure(struct, table, database, policy=policy)
    # The cache read above can yield to a policy reload. Keep the write
    # decision in the same synchronous section as scope-dependent classification.
    if not cache_allowed or not _classification_cache_allowed(policy):
        return classification
    column_analysis = _analyze_column_structure(struct)
    is_lookup = classification["type"] == "lookup"

    await upsert_table_analysis(
        db_path,
        database,
        schema,
        table,
        structural_hash=structural_hash,
        status="ready",
        classification=classification,
        column_analysis=column_analysis,
        is_lookup=is_lookup,
    )
    return classification


async def analyze_columns(
    db_path: str,
    database: str,
    schema: str,
    table: str,
    *,
    policy=None,
) -> list[dict]:
    if _oracle_policy(policy):
        return _analyze_column_structure(await _oracle_structure(policy, schema, table))
    if _classification_cache_allowed(policy):
        await classify_table(db_path, database, schema, table, policy=policy)
    # Classification may have yielded to a reload and skipped persistence.
    if not _classification_cache_allowed(policy):
        policy.authorize_object(schema, table, database)
        struct = await _load_table_structure(db_path, database, schema, table)
        policy.authorize_object(schema, table, database)
        return _analyze_column_structure(struct) if struct is not None else []
    cached = await get_table_analysis(db_path, database, schema, table)
    if policy is not None:
        policy.authorize_object(schema, table, database)
    return cached.get("column_analysis", []) if cached else []


async def summarize_for_joining(
    db_path: str,
    database: str,
    schema: str,
    table: str,
    *,
    policy=None,
) -> dict | None:
    """Return reasoning-ready info for joining against this table.

    Shape: {table, pk, classification, join_candidates, common_filter_columns}
    """
    if policy is not None:
        policy.authorize_object(schema, table, database)
    oracle = _oracle_policy(policy)
    struct = (
        await _oracle_structure(policy, schema, table)
        if oracle
        else await _load_table_structure(db_path, database, schema, table)
    )
    if struct is None:
        return None

    from .relationship_service import group_foreign_keys

    groups = group_foreign_keys(_authorized_structure(struct, database, None if oracle else policy)["foreign_keys"])
    join_candidates = []
    for fk in groups:
        candidate = {
            "to_table": f"{fk['ref_schema']}.{fk['ref_table']}",
            "column_pairs": fk["column_pairs"],
            "constraint_name": fk.get("constraint_name"),
            "metadata_complete": fk["metadata_complete"],
        }
        if len(fk["column_pairs"]) == 1:
            candidate["via_column"] = fk["column_pairs"][0]["from_column"]
            candidate["to_column"] = fk["column_pairs"][0]["to_column"]
        join_candidates.append(candidate)

    common_filter_columns = [
        c["column_name"] for c in struct["columns"] if (_column_semantic(c) or "") in _COMMON_FILTER_SEMANTICS
    ]

    classification = _classification_for_structure(struct, table, database, policy=None if oracle else policy)

    result = {
        "table": f"{schema}.{table}",
        "pk": struct["primary_key"],
        "classification": classification["type"],
        "join_candidates": join_candidates,
        "common_filter_columns": common_filter_columns,
    }
    if oracle:
        from .oracle_metadata import qualified

        result["table"] = qualified(schema, table)
        result.update({key: struct[key] for key in ("chain", "target", "canonical_source", "query_reference")})
        for candidate, fk in zip(join_candidates, groups, strict=True):
            candidate["to_table"] = qualified(fk["ref_schema"], fk["ref_table"])
            candidate["source"] = struct["canonical_source"]
    return result


async def detect_lookup_tables(
    db_path: str,
    database: str,
    *,
    schemas: list[str] | None = None,
    keyword: str | None = None,
    confidence_min: float = 0.0,
    policy=None,
) -> list[dict]:
    results: list[dict] = []
    need_classify: list[tuple[str, str]] = []
    kw_lower = keyword.lower() if keyword else None
    cache_allowed = _classification_cache_allowed(policy)
    async with recording_connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        query = (
            "SELECT t.schema_name,t.table_name,t.structural_hash,a.status,"
            "a.structural_hash AS analysis_hash,a.classification "
            "FROM sc_tables t LEFT JOIN sem_table_analysis a "
            "ON a.database_name=t.database_name AND a.schema_name=t.schema_name AND a.table_name=t.table_name "
            "WHERE t.database_name=? ORDER BY t.schema_name,t.table_name"
            if cache_allowed
            else "SELECT schema_name,table_name FROM sc_tables WHERE database_name=? ORDER BY schema_name,table_name"
        )
        cur = await db.execute(query, (database,))
        for row in await cur.fetchall():
            schema, table = row["schema_name"], row["table_name"]
            if schemas and schema not in schemas:
                continue
            if kw_lower and kw_lower not in f"{schema}.{table}".lower():
                continue
            if policy is not None and not policy.object_allowed(schema, table, database):
                continue
            classification = (
                _cached_classification(
                    {
                        "status": row["status"],
                        "structural_hash": row["analysis_hash"],
                        "classification": row["classification"],
                    },
                    row["structural_hash"],
                    policy=policy,
                )
                if cache_allowed
                else None
            )
            if classification is None:
                need_classify.append((schema, table))
            elif classification.get("type") == "lookup" and classification.get("confidence", 0) >= confidence_min:
                results.append({"schema_name": schema, "table_name": table, "confidence": classification["confidence"]})
    for schema, table in need_classify:
        kwargs = {"policy": policy} if policy is not None else {}
        classification = await classify_table(db_path, database, schema, table, **kwargs)
        if classification.get("type") == "lookup" and classification.get("confidence", 0) >= confidence_min:
            results.append({"schema_name": schema, "table_name": table, "confidence": classification["confidence"]})
    return results
