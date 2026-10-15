import pytest

from sqlserver_semantic_mcp.infrastructure.cache.semantic import (
    enqueue_all_tables,
    get_object_definition,
    get_table_analysis,
    list_pending_table_analyses,
    upsert_object_definition,
    upsert_table_analysis,
)
from sqlserver_semantic_mcp.infrastructure.cache.store import init_store
from sqlserver_semantic_mcp.infrastructure.cache.structural import (
    StructuralSnapshot,
    write_structural_snapshot,
)


@pytest.mark.asyncio
async def test_table_analysis_roundtrip(tmp_path):
    db_path = str(tmp_path / "t.db")
    await init_store(db_path)
    await upsert_table_analysis(
        db_path,
        "db",
        "dbo",
        "T",
        structural_hash="h1",
        status="ready",
        classification={"type": "fact", "confidence": 0.9},
        column_analysis=[{"column": "Id", "semantic": "primary"}],
        is_lookup=False,
    )
    got = await get_table_analysis(db_path, "db", "dbo", "T")
    assert got["status"] == "ready"
    assert got["classification"]["type"] == "fact"
    assert got["is_lookup"] is False


@pytest.mark.asyncio
async def test_object_definition_roundtrip(tmp_path):
    db_path = str(tmp_path / "t.db")
    await init_store(db_path)
    await upsert_object_definition(
        db_path,
        "db",
        "dbo",
        "v1",
        "VIEW",
        object_hash="h1",
        status="ready",
        definition="CREATE VIEW ...",
        dependencies=["dbo.Users"],
        affected_tables=["dbo.Users"],
    )
    got = await get_object_definition(db_path, "db", "dbo", "v1", "VIEW")
    assert got["status"] == "ready"
    assert got["dependencies"] == ["dbo.Users"]


@pytest.mark.asyncio
async def test_enqueue_and_pending(tmp_path):
    db_path = str(tmp_path / "t.db")
    await init_store(db_path)
    snap = StructuralSnapshot(
        tables=[("dbo", "A"), ("dbo", "B")],
        columns=[],
        primary_keys=[],
        foreign_keys=[],
        indexes=[],
        objects=[],
        comments=[],
    )
    await write_structural_snapshot(db_path, "db", snap)
    inserted = await enqueue_all_tables(db_path, "db")
    assert inserted == 2
    inserted_again = await enqueue_all_tables(db_path, "db")
    assert inserted_again == 0
    pending = await list_pending_table_analyses(db_path, "db", 10)
    assert set(pending) == {("dbo", "A"), ("dbo", "B")}
