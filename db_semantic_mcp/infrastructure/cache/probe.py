"""L1 schema probe: per-table / per-object fingerprints and drift detection.

The probe answers "did the schema change?" using catalog-only queries
(see queries/probe_queries.py) so the expensive full structural snapshot
runs only when something actually drifted. Fingerprints are persisted in
SQLite (sc_fingerprints) as the comparison baseline.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from ...config import Config
from ..connection import open_connection
from ..queries.probe_queries import (
    PROBE_COLUMN_FINGERPRINTS,
    PROBE_DEPENDENCIES,
    PROBE_INDEX_FINGERPRINTS,
    PROBE_MODULES,
    PROBE_TABLES,
)

logger = logging.getLogger(__name__)

TableKey = tuple[str, str]  # (schema, table)
ObjectKey = tuple[str, str, str]  # (schema, name, type_code)


@dataclass(frozen=True)
class SchemaFingerprints:
    tables: dict[TableKey, str]
    objects: dict[ObjectKey, str]
    # table -> modules (views/procs/functions) that reference it
    table_dependents: dict[TableKey, list[tuple[str, str]]] = field(
        default_factory=dict,
    )


def filter_fingerprints(cfg: Config, fingerprints: "SchemaFingerprints") -> "SchemaFingerprints":
    """Never persist or compare out-of-scope catalog identities."""
    from ...services.policy_service import PolicyService

    policy = PolicyService(cfg)

    def allowed(schema, name):
        return policy.object_allowed(schema, name, cfg.database)

    return SchemaFingerprints(
        tables={key: value for key, value in fingerprints.tables.items() if allowed(*key)},
        objects={key: value for key, value in fingerprints.objects.items() if allowed(*key[:2])},
        table_dependents={
            key: [obj for obj in values if allowed(*obj)]
            for key, values in fingerprints.table_dependents.items()
            if allowed(*key)
        },
    )


@dataclass(frozen=True)
class FingerprintDiff:
    changed_tables: list[TableKey]
    added_tables: list[TableKey]
    removed_tables: list[TableKey]
    changed_objects: list[ObjectKey]
    added_objects: list[ObjectKey]
    removed_objects: list[ObjectKey]

    @property
    def has_changes(self) -> bool:
        return bool(
            self.changed_tables
            or self.added_tables
            or self.removed_tables
            or self.changed_objects
            or self.added_objects
            or self.removed_objects
        )

    def summary(self) -> dict[str, int]:
        return {
            "tables_changed": len(self.changed_tables),
            "tables_added": len(self.added_tables),
            "tables_removed": len(self.removed_tables),
            "objects_changed": len(self.changed_objects),
            "objects_added": len(self.added_objects),
            "objects_removed": len(self.removed_objects),
        }


def _fetch(conn: Any, sql: str) -> list[tuple]:
    cursor = conn.cursor()
    try:
        cursor.execute(sql)
        return list(cursor.fetchall())
    finally:
        cursor.close()


def fetch_fingerprints(
    cfg: Config,
    conn: Any | None = None,
) -> SchemaFingerprints:
    """Run the catalog probe queries and assemble fingerprints.

    Synchronous (pymssql); call via asyncio.to_thread from async code.
    """
    if cfg.database_type == "oracle":
        from ..backends import backend_for

        return backend_for(cfg).fetch_fingerprints(cfg, conn)

    if conn is None:
        with open_connection(cfg) as owned:
            return fetch_fingerprints(cfg, conn=owned)

    col_rows = _fetch(conn, PROBE_COLUMN_FINGERPRINTS)
    idx_rows = _fetch(conn, PROBE_INDEX_FINGERPRINTS)
    table_rows = _fetch(conn, PROBE_TABLES)
    module_rows = _fetch(conn, PROBE_MODULES)
    dep_rows = _fetch(conn, PROBE_DEPENDENCIES)

    col_fps = {r[0]: f"{r[1]}:{r[2]}" for r in col_rows}
    idx_fps = {r[0]: f"{r[1]}:{r[2]}" for r in idx_rows}

    table_ids: dict[int, TableKey] = {}
    tables: dict[TableKey, str] = {}
    for obj_id, schema, name, modify_date in table_rows:
        key = (schema, name)
        table_ids[obj_id] = key
        tables[key] = "|".join(
            (
                str(modify_date),
                col_fps.get(obj_id, "-"),
                idx_fps.get(obj_id, "-"),
            )
        )

    module_ids: dict[int, ObjectKey] = {}
    objects: dict[ObjectKey, str] = {}
    for obj_id, schema, name, type_code, modify_date, def_hash in module_rows:
        key = (schema, name, type_code)
        module_ids[obj_id] = key
        objects[key] = "|".join(
            (
                str(modify_date),
                def_hash or "-",
                col_fps.get(obj_id, "-"),
            )
        )

    table_dependents: dict[TableKey, list[tuple[str, str]]] = {}
    for referencing_id, referenced_id in dep_rows:
        tkey = table_ids.get(referenced_id)
        mkey = module_ids.get(referencing_id)
        if tkey is not None and mkey is not None:
            table_dependents.setdefault(tkey, []).append(mkey[:2])

    return filter_fingerprints(
        cfg,
        SchemaFingerprints(
            tables=tables,
            objects=objects,
            table_dependents=table_dependents,
        ),
    )


def diff_fingerprints(
    stored_tables: dict[TableKey, str],
    stored_objects: dict[ObjectKey, str],
    fresh: SchemaFingerprints,
) -> FingerprintDiff:
    def _diff(old: dict, new: dict) -> tuple[list, list, list]:
        changed = sorted(k for k, v in new.items() if k in old and old[k] != v)
        added = sorted(k for k in new if k not in old)
        removed = sorted(k for k in old if k not in new)
        return changed, added, removed

    tc, ta, tr = _diff(stored_tables, fresh.tables)
    oc, oa, orm = _diff(stored_objects, fresh.objects)
    return FingerprintDiff(
        changed_tables=tc,
        added_tables=ta,
        removed_tables=tr,
        changed_objects=oc,
        added_objects=oa,
        removed_objects=orm,
    )


async def read_fingerprints(
    db_path: str,
    database: str,
) -> tuple[dict[TableKey, str], dict[ObjectKey, str]]:
    tables: dict[TableKey, str] = {}
    objects: dict[ObjectKey, str] = {}
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT kind, schema_name, name, object_type, fingerprint FROM sc_fingerprints WHERE database_name=?",
            (database,),
        )
        for kind, schema, name, obj_type, fp in await cur.fetchall():
            if kind == "table":
                tables[(schema, name)] = fp
            else:
                objects[(schema, name, obj_type)] = fp
    return tables, objects


async def write_fingerprints(
    db: aiosqlite.Connection,
    database: str,
    fps: SchemaFingerprints,
) -> None:
    """Replace the fingerprint baseline. Runs inside the caller's transaction."""
    await db.execute(
        "DELETE FROM sc_fingerprints WHERE database_name=?",
        (database,),
    )
    await db.executemany(
        "INSERT INTO sc_fingerprints "
        "(database_name, kind, schema_name, name, object_type, fingerprint) "
        "VALUES (?,?,?,?,?,?)",
        [(database, "table", s, n, "", fp) for (s, n), fp in fps.tables.items()]
        + [(database, "object", s, n, t, fp) for (s, n, t), fp in fps.objects.items()],
    )
