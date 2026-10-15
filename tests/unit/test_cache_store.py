import aiosqlite
import pytest

from sqlserver_semantic_mcp.infrastructure.cache.store import (
    SCHEMA_TABLES,
    init_store,
)


@pytest.mark.asyncio
async def test_init_store_creates_all_tables(tmp_path):
    db_path = tmp_path / "test.db"
    await init_store(str(db_path))
    async with aiosqlite.connect(str(db_path)) as db:
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = {row[0] for row in await cursor.fetchall()}
    for table in SCHEMA_TABLES:
        assert table in names, f"missing table: {table}"


@pytest.mark.asyncio
async def test_init_store_idempotent(tmp_path):
    db_path = tmp_path / "test.db"
    await init_store(str(db_path))
    await init_store(str(db_path))  # must not error


@pytest.mark.asyncio
async def test_migrate_pre_hardening_cache_preserves_rows(tmp_path):
    path = str(tmp_path / "legacy.db")
    async with aiosqlite.connect(path) as db:
        await db.executescript("""
            CREATE TABLE sc_columns (
                database_name TEXT, schema_name TEXT, table_name TEXT,
                column_name TEXT, data_type TEXT, max_length INTEGER,
                is_nullable INTEGER, column_default TEXT, ordinal_position INTEGER
            );
            CREATE TABLE sc_foreign_keys (
                database_name TEXT, schema_name TEXT, table_name TEXT,
                column_name TEXT, ref_schema TEXT, ref_table TEXT, ref_column TEXT
            );
            CREATE TABLE sc_indexes (
                database_name TEXT, schema_name TEXT, table_name TEXT,
                index_name TEXT, is_unique INTEGER, is_primary_key INTEGER,
                columns TEXT
            );
            CREATE TABLE sem_table_analysis (
                database_name TEXT, schema_name TEXT, table_name TEXT,
                structural_hash TEXT, status TEXT
            );
            INSERT INTO sc_foreign_keys VALUES ('db','dbo','Child','PId','dbo','Parent','Id');
            INSERT INTO sem_table_analysis VALUES ('db','dbo','Child','hash','error');
        """)
        await db.commit()
    await init_store(path)
    await init_store(path)
    async with aiosqlite.connect(path) as db:
        columns = {r[1] for r in await (await db.execute("PRAGMA table_info(sc_columns)")).fetchall()}
        assert {"numeric_precision", "numeric_scale", "datetime_precision", "collation_name"} <= columns
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM sc_foreign_keys")).fetchone()
        assert row["column_name"] == "PId"
        assert row["constraint_id"] is None
        assert row["constraint_column_id"] is None
        retry = await (await db.execute("SELECT * FROM sem_table_analysis")).fetchone()
        assert retry["attempt_count"] == 0
        assert retry["next_retry_at"] is None
        assert retry["last_attempt_at"] is None
