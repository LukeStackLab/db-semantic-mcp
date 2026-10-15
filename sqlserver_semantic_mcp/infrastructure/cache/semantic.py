import json
from datetime import UTC, datetime, timedelta

import aiosqlite


async def get_table_analysis(
    db_path: str,
    database: str,
    schema: str,
    table: str,
) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM sem_table_analysis WHERE database_name=? AND schema_name=? AND table_name=?",
            (database, schema, table),
        )
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("classification", "column_analysis"):
            if d.get(k):
                d[k] = json.loads(d[k])
        d["is_lookup"] = bool(d["is_lookup"]) if d["is_lookup"] is not None else None
        return d


async def upsert_table_analysis(
    db_path: str,
    database: str,
    schema: str,
    table: str,
    *,
    structural_hash: str,
    status: str,
    classification: dict | None = None,
    column_analysis: list | None = None,
    is_lookup: bool | None = None,
    error_message: str | None = None,
) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO sem_table_analysis "
            "(database_name, schema_name, table_name, structural_hash, status, "
            " classification, column_analysis, is_lookup, computed_at, error_message, "
            " attempt_count, next_retry_at, last_attempt_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,0,NULL,?)",
            (
                database,
                schema,
                table,
                structural_hash,
                status,
                json.dumps(classification) if classification else None,
                json.dumps(column_analysis) if column_analysis else None,
                int(is_lookup) if is_lookup is not None else None,
                datetime.now(UTC).isoformat(),
                error_message,
                datetime.now(UTC).isoformat(),
            ),
        )
        await db.commit()


async def get_object_definition(
    db_path: str,
    database: str,
    schema: str,
    obj_name: str,
    obj_type: str,
) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM sem_object_definitions "
            "WHERE database_name=? AND schema_name=? "
            "AND object_name=? AND object_type=?",
            (database, schema, obj_name, obj_type),
        )
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("dependencies", "affected_tables"):
            if d.get(k):
                d[k] = json.loads(d[k])
        return d


async def upsert_object_definition(
    db_path: str,
    database: str,
    schema: str,
    obj_name: str,
    obj_type: str,
    *,
    object_hash: str,
    status: str,
    definition: str | None = None,
    dependencies: list | None = None,
    affected_tables: list | None = None,
    error_message: str | None = None,
) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO sem_object_definitions "
            "(database_name, schema_name, object_name, object_type, object_hash, "
            " status, definition, dependencies, affected_tables, "
            " computed_at, error_message) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                database,
                schema,
                obj_name,
                obj_type,
                object_hash,
                status,
                definition,
                json.dumps(dependencies) if dependencies else None,
                json.dumps(affected_tables) if affected_tables else None,
                datetime.now(UTC).isoformat(),
                error_message,
            ),
        )
        await db.commit()


async def list_pending_table_analyses(
    db_path: str,
    database: str,
    limit: int,
) -> list[tuple[str, str]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT schema_name, table_name FROM sem_table_analysis "
            "WHERE database_name=? AND (status IN ('pending','dirty') "
            "OR (status='error' AND (next_retry_at IS NULL OR next_retry_at<=?))) "
            "ORDER BY COALESCE(last_attempt_at,''), schema_name, table_name LIMIT ?",
            (database, datetime.now(UTC).isoformat(), limit),
        )
        return [(r["schema_name"], r["table_name"]) for r in await cur.fetchall()]


async def record_table_analysis_failure(
    db_path: str,
    database: str,
    schema: str,
    table: str,
    *,
    error_message: str,
    retry_base_s: float,
    retry_max_s: float,
) -> None:
    """Preserve prior analysis while persisting fair, bounded retry scheduling."""
    now = datetime.now(UTC)
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT attempt_count FROM sem_table_analysis WHERE database_name=? AND schema_name=? AND table_name=?",
            (database, schema, table),
        )
        row = await cur.fetchone()
        attempts = int(row[0] or 0) + 1 if row else 1
        delay = min(retry_max_s, retry_base_s * (2 ** min(attempts - 1, 30)))
        await db.execute(
            "UPDATE sem_table_analysis SET status='error', error_message=?, "
            "attempt_count=?, next_retry_at=?, last_attempt_at=?, "
            "structural_hash=COALESCE((SELECT structural_hash FROM sc_tables "
            "WHERE database_name=? AND schema_name=? AND table_name=?), structural_hash) "
            "WHERE database_name=? AND schema_name=? AND table_name=?",
            (
                error_message,
                attempts,
                (now + timedelta(seconds=delay)).isoformat(),
                now.isoformat(),
                database,
                schema,
                table,
                database,
                schema,
                table,
            ),
        )
        await db.commit()


async def enqueue_all_tables(db_path: str, database: str) -> int:
    """Queue analysis for cached tables that have no semantic row yet.

    Each row carries its own per-table structural hash from sc_tables.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO sem_table_analysis "
            "(database_name, schema_name, table_name, structural_hash, status) "
            "SELECT database_name, schema_name, table_name, "
            "       COALESCE(structural_hash, ''), 'pending' "
            "FROM sc_tables WHERE database_name=?",
            (database,),
        )
        await db.commit()
        cur = await db.execute("SELECT changes()")
        row = await cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0
