import hashlib
import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from ...config import Config
from ..connection import open_connection
from ..queries.comment_queries import GET_COMMENTS
from ..queries.metadata_queries import (
    GET_COLUMNS,
    GET_FOREIGN_KEYS,
    GET_INDEXES,
    GET_OBJECTS,
    GET_PRIMARY_KEYS,
    GET_TABLES,
)
from ..workers import run_db_work
from .probe import (
    SchemaFingerprints,
    diff_fingerprints,
    fetch_fingerprints,
    filter_fingerprints,
    write_fingerprints,
)

logger = logging.getLogger(__name__)


@dataclass
class StructuralSnapshot:
    tables: list[tuple]  # (schema, table)
    columns: list[tuple]  # 8..12 fields: base 8 + precision, scale, datetime precision, collation
    primary_keys: list[tuple]  # (schema, table, col)
    foreign_keys: list[tuple]  # 6..11 fields: base 6 + constraint id/name/ordinal, disabled, untrusted
    indexes: list[tuple]  # 6..8 fields: base 6 + has_filter, is_disabled
    objects: list[tuple]  # (schema, name, type)
    comments: list[tuple]  # (schema, object, column, description)


def _sha256(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _pad_rows(rows, minimum: int, width: int) -> list[tuple]:
    """Keep old snapshots usable without inventing missing catalog evidence."""
    normalized = []
    for row in rows:
        if not minimum <= len(row) <= width:
            raise ValueError(f"Expected {minimum}..{width} metadata fields; got {len(row)}")
        normalized.append((*row, *((None,) * (width - len(row)))))
    return normalized


def _sorted_rows(rows) -> list[list]:
    # JSON ordering also handles equal short-row prefixes with nullable trailing fields.
    return sorted([list(row) for row in rows], key=lambda row: json.dumps(row, default=str))


def compute_structural_hash(
    tables,
    columns,
    primary_keys,
    foreign_keys,
    indexes,
) -> str:
    return _sha256(
        {
            "tables": _sorted_rows(tables),
            "columns": _sorted_rows(_pad_rows(columns, 8, 12)),
            "primary_keys": _sorted_rows(primary_keys),
            "foreign_keys": _sorted_rows(_pad_rows(foreign_keys, 6, 11)),
            "indexes": _sorted_rows(_pad_rows(indexes, 6, 8)),
        }
    )


def compute_table_hashes(snap: "StructuralSnapshot") -> dict[tuple[str, str], str]:
    """Per-table structural hash over that table's columns/PKs/FKs/indexes.

    Enables targeted invalidation: only tables whose own structure changed
    get their semantic analysis marked dirty.
    """
    parts: dict[tuple[str, str], dict[str, list]] = {
        (s, t): {"columns": [], "primary_keys": [], "foreign_keys": [], "indexes": []} for (s, t) in snap.tables
    }

    def _bucket(rows, kind: str) -> None:
        for row in rows:
            key = (row[0], row[1])
            if key in parts:
                parts[key][kind].append(list(row))

    _bucket(_pad_rows(snap.columns, 8, 12), "columns")
    _bucket(snap.primary_keys, "primary_keys")
    _bucket(_pad_rows(snap.foreign_keys, 6, 11), "foreign_keys")
    _bucket(_pad_rows(snap.indexes, 6, 8), "indexes")

    return {key: _sha256({k: _sorted_rows(v) for k, v in buckets.items()}) for key, buckets in parts.items()}


def compute_object_hash(objects) -> str:
    return _sha256({"objects": sorted([list(o) for o in objects])})


def compute_comment_hash(comments) -> str:
    return _sha256({"comments": sorted([list(c) for c in comments])})


async def read_schema_version(db_path: str, database: str) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM schema_version WHERE database_name = ?",
            (database,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def _invalidate_fingerprint_changes(
    db: aiosqlite.Connection,
    database: str,
    fresh: SchemaFingerprints,
) -> int:
    """Invalidate definitions against the baseline in this snapshot transaction."""
    cursor = await db.execute(
        "SELECT kind,schema_name,name,object_type,fingerprint FROM sc_fingerprints WHERE database_name=?",
        (database,),
    )
    tables, objects = {}, {}
    for kind, schema, name, object_type, fingerprint in await cursor.fetchall():
        if kind == "table":
            tables[(schema, name)] = fingerprint
        else:
            objects[(schema, name, object_type)] = fingerprint
    diff = diff_fingerprints(tables, objects, fresh)
    # Added fingerprints can belong to cached definitions written before
    # module baselines existed, whose bodies were never verified against one.
    keys = {(schema, name) for schema, name, _type in (*diff.changed_objects, *diff.added_objects)}
    for table in (*diff.changed_tables, *diff.removed_tables):
        keys.update(fresh.table_dependents.get(table, []))
    marked = 0
    for schema, name in sorted(keys):
        cursor = await db.execute(
            "UPDATE sem_object_definitions SET status='dirty' "
            "WHERE database_name=? AND schema_name=? AND object_name=? AND status<>'dirty'",
            (database, schema, name),
        )
        marked += cursor.rowcount or 0
    return marked


async def _replace_snapshot_rows(db, database: str, snap: StructuralSnapshot, table_hashes: dict) -> None:
    for tbl in [
        "sc_tables",
        "sc_columns",
        "sc_primary_keys",
        "sc_foreign_keys",
        "sc_indexes",
        "sc_objects",
        "sc_comments",
    ]:
        await db.execute(
            f"DELETE FROM {tbl} WHERE database_name = ?",
            (database,),
        )

    await db.executemany(
        "INSERT INTO sc_tables (database_name, schema_name, table_name, structural_hash) VALUES (?,?,?,?)",
        [(database, s, t, table_hashes[(s, t)]) for (s, t) in snap.tables],
    )
    await db.executemany(
        "INSERT INTO sc_columns "
        "(database_name, schema_name, table_name, column_name, data_type, "
        "max_length, is_nullable, column_default, ordinal_position, "
        "numeric_precision, numeric_scale, datetime_precision, collation_name) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(database, *row) for row in snap.columns],
    )
    await db.executemany(
        "INSERT INTO sc_primary_keys (database_name, schema_name, table_name, column_name) VALUES (?,?,?,?)",
        [(database, *row) for row in snap.primary_keys],
    )
    await db.executemany(
        "INSERT INTO sc_foreign_keys "
        "(database_name, schema_name, table_name, column_name, "
        "ref_schema, ref_table, ref_column, constraint_id, constraint_name, "
        "constraint_column_id, is_disabled, is_not_trusted) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(database, *row) for row in snap.foreign_keys],
    )
    await db.executemany(
        "INSERT INTO sc_indexes "
        "(database_name, schema_name, table_name, index_name, "
        "is_unique, is_primary_key, columns, has_filter, is_disabled) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [(database, *row) for row in snap.indexes],
    )
    await db.executemany(
        "INSERT INTO sc_objects (database_name, schema_name, object_name, object_type) VALUES (?,?,?,?)",
        [(database, *row) for row in snap.objects],
    )
    await db.executemany(
        "INSERT INTO sc_comments "
        "(database_name, schema_name, object_name, column_name, description) "
        "VALUES (?,?,?,?,?)",
        [(database, *row) for row in snap.comments],
    )


async def _cascade_analysis_invalidation(db, database: str, object_hash: str) -> None:
    # Targeted cascade: dirty only tables whose own structure changed
    await db.execute(
        "UPDATE sem_table_analysis SET status='dirty',attempt_count=0,"
        "last_attempt_at=NULL,next_retry_at=NULL,error_message=NULL "
        "WHERE database_name=:db AND EXISTS ("
        "  SELECT 1 FROM sc_tables t"
        "  WHERE t.database_name = sem_table_analysis.database_name"
        "    AND t.schema_name = sem_table_analysis.schema_name"
        "    AND t.table_name = sem_table_analysis.table_name"
        "    AND t.structural_hash <> sem_table_analysis.structural_hash)",
        {"db": database},
    )
    # Drop semantic rows for tables/objects no longer present
    await db.execute(
        "DELETE FROM sem_table_analysis "
        "WHERE database_name=:db AND NOT EXISTS ("
        "  SELECT 1 FROM sc_tables t"
        "  WHERE t.database_name = sem_table_analysis.database_name"
        "    AND t.schema_name = sem_table_analysis.schema_name"
        "    AND t.table_name = sem_table_analysis.table_name)",
        {"db": database},
    )
    await db.execute(
        "UPDATE sem_object_definitions SET status='dirty' WHERE database_name=? AND object_hash<>?",
        (database, object_hash),
    )
    await db.execute(
        "DELETE FROM sem_object_definitions "
        "WHERE database_name=:db AND NOT EXISTS ("
        "  SELECT 1 FROM sc_objects o"
        "  WHERE o.database_name = sem_object_definitions.database_name"
        "    AND o.schema_name = sem_object_definitions.schema_name"
        "    AND o.object_name = sem_object_definitions.object_name)",
        {"db": database},
    )


async def write_structural_snapshot(
    db_path: str,
    database: str,
    snap: StructuralSnapshot,
    fingerprints: SchemaFingerprints | None = None,
) -> dict:
    snap = replace(
        snap,
        columns=_pad_rows(snap.columns, 8, 12),
        foreign_keys=_pad_rows(snap.foreign_keys, 6, 11),
        indexes=_pad_rows(snap.indexes, 6, 8),
    )
    structural_hash = compute_structural_hash(
        snap.tables,
        snap.columns,
        snap.primary_keys,
        snap.foreign_keys,
        snap.indexes,
    )
    table_hashes = compute_table_hashes(snap)
    object_hash = compute_object_hash(snap.objects)
    comment_hash = compute_comment_hash(snap.comments)
    captured_at = datetime.now(UTC).isoformat()
    objects_marked = 0

    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN")
        try:
            await _replace_snapshot_rows(db, database, snap, table_hashes)

            await db.execute(
                "INSERT OR REPLACE INTO schema_version "
                "(database_name, structural_hash, object_hash, comment_hash, "
                " captured_at) VALUES (?,?,?,?,?)",
                (database, structural_hash, object_hash, comment_hash, captured_at),
            )

            await _cascade_analysis_invalidation(db, database, object_hash)

            if fingerprints is not None:
                objects_marked = await _invalidate_fingerprint_changes(db, database, fingerprints)
                await write_fingerprints(db, database, fingerprints)

            await db.commit()
        except Exception:  # noqa: BLE001 (cache write is best-effort)
            await db.rollback()
            raise

    return {
        "structural_hash": structural_hash,
        "object_hash": object_hash,
        "comment_hash": comment_hash,
        "captured_at": captured_at,
        "objects_marked_dirty": objects_marked,
    }


def fetch_snapshot_from_server(
    cfg: Config,
    conn: Any | None = None,
) -> StructuralSnapshot:
    if cfg.database_type == "oracle":
        from ..backends import backend_for

        return backend_for(cfg).fetch_snapshot(cfg, conn)

    if conn is None:
        with open_connection(cfg) as owned:
            return fetch_snapshot_from_server(cfg, conn=owned)

    queries = (
        GET_TABLES,
        GET_COLUMNS,
        GET_PRIMARY_KEYS,
        GET_FOREIGN_KEYS,
        GET_INDEXES,
        GET_OBJECTS,
        GET_COMMENTS,
    )
    results: list[list[tuple]] = []
    cursor = conn.cursor()
    try:
        for sql in queries:
            cursor.execute(sql)
            results.append(list(cursor.fetchall()))
    finally:
        cursor.close()

    return StructuralSnapshot(
        tables=results[0],
        columns=results[1],
        primary_keys=results[2],
        foreign_keys=results[3],
        indexes=results[4],
        objects=results[5],
        comments=results[6],
    )


def _fetch_warmup_data(cfg: Config) -> tuple[SchemaFingerprints, StructuralSnapshot]:
    # Fingerprints first: a change landing between the two fetches then
    # shows up as drift on the next probe instead of being missed.
    with open_connection(cfg) as conn:
        fps = fetch_fingerprints(cfg, conn=conn)
        snap = fetch_snapshot_from_server(cfg, conn=conn)
    return fps, snap


async def warmup_structural_cache(
    cfg: Config,
    fingerprints: SchemaFingerprints | None = None,
) -> dict:
    """Fetch snapshot (and probe fingerprints) from SQL Server into SQLite.

    Returns the new hashes. A pre-fetched fingerprint set (from the probe)
    can be passed in to avoid re-running the probe queries.
    """
    if fingerprints is None:
        fingerprints, snap = await run_db_work(cfg, _fetch_warmup_data, cfg)
    else:
        snap = await run_db_work(cfg, fetch_snapshot_from_server, cfg)
    from ...services.policy_service import PolicyService

    policy = PolicyService(cfg)

    def allowed(row):
        return policy.object_allowed(row[0], row[1], cfg.database)

    snap = replace(
        snap,
        tables=[row for row in snap.tables if allowed(row)],
        columns=[row for row in snap.columns if allowed(row)],
        primary_keys=[row for row in snap.primary_keys if allowed(row)],
        foreign_keys=[row for row in snap.foreign_keys if allowed(row) and allowed(row[3:5])],
        indexes=[row for row in snap.indexes if allowed(row)],
        objects=[row for row in snap.objects if allowed(row)],
        comments=[row for row in snap.comments if allowed(row)],
    )
    fingerprints = filter_fingerprints(cfg, fingerprints)
    logger.info(
        "Structural snapshot: %d tables, %d columns, %d FKs, %d objects",
        len(snap.tables),
        len(snap.columns),
        len(snap.foreign_keys),
        len(snap.objects),
    )
    return await write_structural_snapshot(
        cfg.cache_path,
        cfg.database,
        snap,
        fingerprints=fingerprints,
    )
