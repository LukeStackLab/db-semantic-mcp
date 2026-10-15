from pathlib import Path

import aiosqlite

SCHEMA_TABLES = [
    "schema_version",
    "sc_tables",
    "sc_columns",
    "sc_primary_keys",
    "sc_foreign_keys",
    "sc_indexes",
    "sc_objects",
    "sc_comments",
    "sc_fingerprints",
    "sem_table_analysis",
    "sem_object_definitions",
]

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    database_name   TEXT PRIMARY KEY,
    structural_hash TEXT NOT NULL,
    object_hash     TEXT NOT NULL,
    comment_hash    TEXT NOT NULL,
    captured_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sc_tables (
    database_name   TEXT NOT NULL,
    schema_name     TEXT NOT NULL,
    table_name      TEXT NOT NULL,
    structural_hash TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (database_name, schema_name, table_name)
);

CREATE TABLE IF NOT EXISTS sc_columns (
    database_name    TEXT NOT NULL,
    schema_name      TEXT NOT NULL,
    table_name       TEXT NOT NULL,
    column_name      TEXT NOT NULL,
    data_type        TEXT NOT NULL,
    max_length       INTEGER,
    is_nullable      INTEGER NOT NULL,
    column_default   TEXT,
    ordinal_position INTEGER NOT NULL,
    numeric_precision INTEGER,
    numeric_scale     INTEGER,
    datetime_precision INTEGER,
    collation_name    TEXT,
    PRIMARY KEY (database_name, schema_name, table_name, column_name)
);

CREATE TABLE IF NOT EXISTS sc_primary_keys (
    database_name TEXT NOT NULL,
    schema_name   TEXT NOT NULL,
    table_name    TEXT NOT NULL,
    column_name   TEXT NOT NULL,
    PRIMARY KEY (database_name, schema_name, table_name, column_name)
);

CREATE TABLE IF NOT EXISTS sc_foreign_keys (
    database_name TEXT NOT NULL,
    schema_name   TEXT NOT NULL,
    table_name    TEXT NOT NULL,
    column_name   TEXT NOT NULL,
    ref_schema    TEXT NOT NULL,
    ref_table     TEXT NOT NULL,
    ref_column    TEXT NOT NULL,
    constraint_id INTEGER,
    constraint_name TEXT,
    constraint_column_id INTEGER,
    is_disabled INTEGER,
    is_not_trusted INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fk_from
    ON sc_foreign_keys (database_name, schema_name, table_name);
CREATE INDEX IF NOT EXISTS idx_fk_to
    ON sc_foreign_keys (database_name, ref_schema, ref_table);

CREATE TABLE IF NOT EXISTS sc_indexes (
    database_name  TEXT NOT NULL,
    schema_name    TEXT NOT NULL,
    table_name     TEXT NOT NULL,
    index_name     TEXT NOT NULL,
    is_unique      INTEGER NOT NULL,
    is_primary_key INTEGER NOT NULL,
    columns        TEXT NOT NULL,
    has_filter     INTEGER,
    is_disabled    INTEGER,
    PRIMARY KEY (database_name, schema_name, table_name, index_name)
);

CREATE TABLE IF NOT EXISTS sc_objects (
    database_name TEXT NOT NULL,
    schema_name   TEXT NOT NULL,
    object_name   TEXT NOT NULL,
    object_type   TEXT NOT NULL,
    PRIMARY KEY (database_name, schema_name, object_name, object_type)
);

CREATE TABLE IF NOT EXISTS sc_comments (
    database_name TEXT NOT NULL,
    schema_name   TEXT NOT NULL,
    object_name   TEXT NOT NULL,
    column_name   TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL,
    PRIMARY KEY (database_name, schema_name, object_name, column_name)
);

CREATE TABLE IF NOT EXISTS sc_fingerprints (
    database_name TEXT NOT NULL,
    kind          TEXT NOT NULL,           -- 'table' | 'object'
    schema_name   TEXT NOT NULL,
    name          TEXT NOT NULL,
    object_type   TEXT NOT NULL DEFAULT '',
    fingerprint   TEXT NOT NULL,
    PRIMARY KEY (database_name, kind, schema_name, name, object_type)
);

CREATE TABLE IF NOT EXISTS sem_table_analysis (
    database_name   TEXT NOT NULL,
    schema_name     TEXT NOT NULL,
    table_name      TEXT NOT NULL,
    structural_hash TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    classification  TEXT,
    column_analysis TEXT,
    is_lookup       INTEGER,
    computed_at     TEXT,
    error_message   TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    next_retry_at   TEXT,
    last_attempt_at TEXT,
    PRIMARY KEY (database_name, schema_name, table_name)
);
CREATE INDEX IF NOT EXISTS idx_sem_table_status
    ON sem_table_analysis (status);

CREATE TABLE IF NOT EXISTS sem_object_definitions (
    database_name   TEXT NOT NULL,
    schema_name     TEXT NOT NULL,
    object_name     TEXT NOT NULL,
    object_type     TEXT NOT NULL,
    object_hash     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    definition      TEXT,
    dependencies    TEXT,
    affected_tables TEXT,
    computed_at     TEXT,
    error_message   TEXT,
    PRIMARY KEY (database_name, schema_name, object_name, object_type)
);
CREATE INDEX IF NOT EXISTS idx_sem_obj_status
    ON sem_object_definitions (status);

CREATE TABLE IF NOT EXISTS tool_metrics (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name          TEXT NOT NULL,
    response_bytes     INTEGER NOT NULL,
    array_length       INTEGER,
    fields_returned    INTEGER,
    route_type         TEXT,
    was_direct_execute INTEGER,
    bundle_used        INTEGER,
    next_action        TEXT,
    recorded_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_metrics_name
    ON tool_metrics (tool_name);
"""


async def _migrate(db: aiosqlite.Connection) -> None:
    # per-table structural_hash on sc_tables (pre-existing cache files)
    cur = await db.execute("PRAGMA table_info(sc_tables)")
    cols = {row[1] for row in await cur.fetchall()}
    if "structural_hash" not in cols:
        await db.execute("ALTER TABLE sc_tables ADD COLUMN structural_hash TEXT NOT NULL DEFAULT ''")

    additions = {
        "sc_columns": {
            "numeric_precision": "INTEGER",
            "numeric_scale": "INTEGER",
            "datetime_precision": "INTEGER",
            "collation_name": "TEXT",
        },
        "sc_foreign_keys": {
            "constraint_id": "INTEGER",
            "constraint_name": "TEXT",
            "constraint_column_id": "INTEGER",
            "is_disabled": "INTEGER",
            "is_not_trusted": "INTEGER",
        },
        "sc_indexes": {"has_filter": "INTEGER", "is_disabled": "INTEGER"},
        "sem_table_analysis": {
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "next_retry_at": "TEXT",
            "last_attempt_at": "TEXT",
        },
    }
    for table, definitions in additions.items():
        cur = await db.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in await cur.fetchall()}
        for name, definition in definitions.items():
            if name not in existing:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


async def init_store(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA_DDL)
        await _migrate(db)
        from ...services.metrics_service import migrate_metrics

        await migrate_metrics(db)
        await db.commit()
