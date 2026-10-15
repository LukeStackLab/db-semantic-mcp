import pytest

from db_semantic_mcp.infrastructure.cache.store import init_store
from db_semantic_mcp.infrastructure.cache.structural import (
    StructuralSnapshot,
    write_structural_snapshot,
)
from db_semantic_mcp.services.metadata_service import (
    database_summary,
    describe_table,
    list_tables,
)


async def _setup(tmp_path):
    db_path = str(tmp_path / "t.db")
    await init_store(db_path)
    snap = StructuralSnapshot(
        tables=[("dbo", "Users"), ("dbo", "Orders")],
        columns=[
            ("dbo", "Users", "Id", "int", None, 0, None, 1),
            ("dbo", "Users", "Name", "nvarchar", 50, 1, None, 2),
            ("dbo", "Orders", "Id", "int", None, 0, None, 1),
            ("dbo", "Orders", "UserId", "int", None, 0, None, 2),
        ],
        primary_keys=[("dbo", "Users", "Id"), ("dbo", "Orders", "Id")],
        foreign_keys=[("dbo", "Orders", "UserId", "dbo", "Users", "Id")],
        indexes=[("dbo", "Users", "PK_Users", 1, 1, "Id")],
        objects=[],
        comments=[("dbo", "Users", "", "users table"), ("dbo", "Users", "Id", "pk")],
    )
    await write_structural_snapshot(db_path, "testdb", snap)
    return db_path


@pytest.mark.asyncio
async def test_list_tables(tmp_path):
    db_path = await _setup(tmp_path)
    rows = await list_tables(db_path, "testdb")
    names = [(r["schema_name"], r["table_name"]) for r in rows]
    assert ("dbo", "Users") in names
    assert ("dbo", "Orders") in names


@pytest.mark.asyncio
async def test_describe_table(tmp_path):
    db_path = await _setup(tmp_path)
    t = await describe_table(db_path, "testdb", "dbo", "Users")
    assert t is not None
    assert t["primary_key"] == ["Id"]
    assert len(t["columns"]) == 2
    assert t["description"] == "users table"
    id_col = next(c for c in t["columns"] if c["column_name"] == "Id")
    assert id_col["description"] == "pk"


@pytest.mark.asyncio
async def test_describe_missing_table(tmp_path):
    db_path = await _setup(tmp_path)
    t = await describe_table(db_path, "testdb", "dbo", "Ghost")
    assert t is None


@pytest.mark.asyncio
async def test_database_summary(tmp_path):
    db_path = await _setup(tmp_path)
    s = await database_summary(db_path, "testdb")
    assert s["table_count"] == 2
    assert s["column_count"] == 4
    assert s["foreign_key_count"] == 1
    assert s["schema_version"] is not None
