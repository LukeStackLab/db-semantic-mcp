"""Authorized, detail-aware metadata reads from the local snapshot."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from .policy_service import PolicyService


def _allowed(policy, database: str, schema: str, name: str) -> bool:
    return policy is None or policy.object_allowed(schema, name, database)


def _authorize(policy, database: str, schema: str, name: str) -> None:
    if policy is not None:
        policy.authorize_object(schema, name, database)


def column_sql_type(column: dict) -> str:
    name = column.get("data_type", "unknown")
    if name in {"decimal", "numeric"} and column.get("numeric_precision") is not None:
        return f"{name}({column['numeric_precision']},{column.get('numeric_scale') or 0})"
    if name in {"char", "varchar", "nchar", "nvarchar", "binary", "varbinary"}:
        length = column.get("max_length")
        if length is not None:
            return f"{name}({'max' if length == -1 else length})"
    if name in {"datetime2", "datetimeoffset", "time"} and column.get("datetime_precision") is not None:
        return f"{name}({column['datetime_precision']})"
    return name


def _freshness(captured_at, policy=None) -> dict:
    ttl = getattr(getattr(policy, "_cfg", None), "schema_cache_ttl_s", 300.0)
    age = None
    if captured_at:
        try:
            captured = datetime.fromisoformat(captured_at)
            if captured.tzinfo is None:
                captured = captured.replace(tzinfo=timezone.utc)
            age = max(0.0, (datetime.now(timezone.utc) - captured).total_seconds())
        except (TypeError, ValueError):
            pass
    return {"hit": True, "captured_at": captured_at,
            "stale": age is None or age >= ttl}


async def _list_columns_from_db(db, database, schema, table, *, descriptions=True):
    cur = await db.execute(
        "SELECT column_name,data_type,max_length,is_nullable,column_default,ordinal_position,"
        "numeric_precision,numeric_scale,datetime_precision,collation_name "
        "FROM sc_columns WHERE database_name=? AND schema_name=? AND table_name=? "
        "ORDER BY ordinal_position", (database, schema, table))
    columns = [dict(row) for row in await cur.fetchall()]
    comments = {}
    if descriptions:
        cur = await db.execute(
            "SELECT column_name,description FROM sc_comments WHERE database_name=? "
            "AND schema_name=? AND object_name=? AND column_name<>''", (database, schema, table))
        comments = {row["column_name"]: row["description"] for row in await cur.fetchall()}
    for column in columns:
        column["is_nullable"] = bool(column["is_nullable"])
        column["default_value"] = column.pop("column_default", None)
        if descriptions:
            column["description"] = comments.get(column["column_name"])
        column["sql_type"] = column_sql_type(column)
    return columns


async def list_tables(db_path: str, database: str, *, schemas=None, keyword=None,
                      limit: Optional[int] = None, policy: Optional[PolicyService] = None) -> list[dict]:
    sql = "SELECT schema_name,table_name FROM sc_tables WHERE database_name=?"
    params = [database]
    if schemas:
        sql += f" AND schema_name IN ({','.join('?' for _ in schemas)})"
        params.extend(schemas)
    if keyword:
        sql += " AND LOWER(schema_name || '.' || table_name) LIKE ? ESCAPE '\\'"
        kw = keyword.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{kw}%")
    sql += " ORDER BY schema_name,table_name"
    # Scope filtering precedes limit so denied rows cannot hide allowed matches.
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        result = []
        async for row in cur:
            if _allowed(policy, database, row["schema_name"], row["table_name"]):
                result.append(dict(row))
                if limit is not None and len(result) >= limit:
                    break
        return result


async def describe_table(db_path: str, database: str, schema: str, table: str, *,
                         detail: str = "full", columns: Optional[list[str]] = None,
                         policy: Optional[PolicyService] = None) -> Optional[dict]:
    _authorize(policy, database, schema, table)
    if detail not in {"brief", "standard", "full"}:
        raise ValueError("Invalid metadata detail")
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT t.structural_hash,a.classification,a.status,a.structural_hash AS analysis_hash,"
            "v.captured_at FROM sc_tables t LEFT JOIN sem_table_analysis a "
            "ON a.database_name=t.database_name AND a.schema_name=t.schema_name AND a.table_name=t.table_name "
            "LEFT JOIN schema_version v ON v.database_name=t.database_name "
            "WHERE t.database_name=? AND t.schema_name=? AND t.table_name=?", (database, schema, table))
        state = await cur.fetchone()
        if state is None:
            return None
        all_columns = await _list_columns_from_db(db, database, schema, table, descriptions=detail == "full")
        cur = await db.execute("SELECT column_name FROM sc_primary_keys WHERE database_name=? "
                               "AND schema_name=? AND table_name=?", (database, schema, table))
        pk = [row["column_name"] for row in await cur.fetchall()]
        cur = await db.execute("SELECT * FROM sc_foreign_keys WHERE database_name=? "
                               "AND schema_name=? AND table_name=?", (database, schema, table))
        fks = [dict(row) for row in await cur.fetchall()
               if _allowed(policy, database, row["ref_schema"], row["ref_table"])]
        indexes, description = [], None
        if detail == "full":
            cur = await db.execute("SELECT index_name,is_unique,is_primary_key,columns,has_filter,is_disabled "
                                   "FROM sc_indexes WHERE database_name=? AND schema_name=? AND table_name=?",
                                   (database, schema, table))
            from .relationship_service import parse_index_columns
            indexes = [{**dict(row), "columns": parse_index_columns(row["columns"]),
                        "is_unique": bool(row["is_unique"]), "is_primary_key": bool(row["is_primary_key"])}
                       for row in await cur.fetchall()]
            cur = await db.execute("SELECT description FROM sc_comments WHERE database_name=? "
                                   "AND schema_name=? AND object_name=? AND column_name=''", (database, schema, table))
            row = await cur.fetchone()
            description = row["description"] if row else None
    from .semantic_service import _classify
    classification = None
    if state["status"] == "ready" and state["analysis_hash"] == state["structural_hash"] and state["classification"]:
        try:
            classification = json.loads(state["classification"])
            if not isinstance(classification, dict) or classification.get("algorithm") != "constraints-v1":
                classification = None
        except (ValueError, TypeError):
            pass
    if classification is None:
        classification = _classify({"columns": all_columns, "primary_key": pk, "foreign_keys": fks}, table)
    selected = all_columns
    if columns is not None:
        known = {col["column_name"] for col in all_columns}
        missing = set(columns) - known
        if missing:
            raise ValueError("Unknown column(s): " + ", ".join(sorted(missing)))
        selected = [col for col in all_columns if col["column_name"] in columns]
    from .relationship_service import group_foreign_keys
    joins = group_foreign_keys(fks)
    if columns is not None:
        joins = [fk for fk in joins if any(pair["from_column"] in columns for pair in fk["column_pairs"])]
    required = {pair["from_column"] for fk in joins for pair in fk["column_pairs"]}
    return {"schema_name": schema, "table_name": table, "columns": selected,
            "column_count": len(all_columns), "columns_omitted": len(selected) < len(all_columns),
            "primary_key": pk, "foreign_keys": fks, "indexes": indexes, "description": description,
            "classification": classification, "structural_hash": state["structural_hash"],
            "join_constraints": joins,
            "required_join_columns": [c for c in all_columns if c["column_name"] in required],
            "cache": _freshness(state["captured_at"], policy)}


async def search_documents(db_path: str, database: str, *, schemas=None, policy=None) -> list[dict]:
    """Read deterministic search documents in one connection, without external models."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT schema_name,table_name FROM sc_tables WHERE database_name=? "
                               "ORDER BY schema_name,table_name", (database,))
        docs = {(row["schema_name"], row["table_name"]): {**dict(row), "description": "", "column_names": [], "column_descriptions": []}
                for row in await cur.fetchall()
                if (not schemas or row["schema_name"] in schemas)
                and _allowed(policy, database, row["schema_name"], row["table_name"])}
        cur = await db.execute("SELECT schema_name,table_name,column_name FROM sc_columns WHERE database_name=?", (database,))
        for row in await cur.fetchall():
            doc = docs.get((row["schema_name"], row["table_name"]))
            if doc is not None:
                doc["column_names"].append(row["column_name"])
        cur = await db.execute("SELECT schema_name,object_name,column_name,description FROM sc_comments WHERE database_name=?", (database,))
        for row in await cur.fetchall():
            doc = docs.get((row["schema_name"], row["object_name"]))
            if doc is not None:
                if row["column_name"]:
                    doc["column_descriptions"].append(row["description"])
                else:
                    doc["description"] = row["description"]
    return list(docs.values())


async def database_summary(db_path: str, database: str, *, policy=None) -> dict:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        counts = {}
        for name in ("sc_tables", "sc_columns", "sc_foreign_keys", "sc_indexes", "sc_objects"):
            cur = await db.execute(f"SELECT * FROM {name} WHERE database_name=?", (database,))
            rows = [dict(row) for row in await cur.fetchall()]
            counts[name] = [row for row in rows if _allowed(policy, database, row["schema_name"], row.get("table_name", row.get("object_name", "")))
                            and (name != "sc_foreign_keys" or _allowed(policy, database, row["ref_schema"], row["ref_table"]))]
        objects_by_type = {}
        for row in counts["sc_objects"]:
            objects_by_type[row["object_type"]] = objects_by_type.get(row["object_type"], 0) + 1
        cur = await db.execute("SELECT * FROM schema_version WHERE database_name=?", (database,))
        version = await cur.fetchone()
    return {"database_name": database, "table_count": len(counts["sc_tables"]),
            "column_count": len(counts["sc_columns"]), "foreign_key_count": len(counts["sc_foreign_keys"]),
            "index_count": len(counts["sc_indexes"]), "object_count": len(counts["sc_objects"]),
            "objects_by_type": objects_by_type, "schema_version": dict(version) if version else None,
            "cache": _freshness(version["captured_at"] if version else None, policy)}
