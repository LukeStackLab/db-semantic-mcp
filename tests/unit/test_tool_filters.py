"""Tests for list-tool filter parameters (P1)."""

import pytest


@pytest.fixture
def cache_with_tables(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "testdb")
    monkeypatch.setenv("SEMANTIC_MCP_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_PASSWORD", "p")
    monkeypatch.setenv("SEMANTIC_MCP_CACHE_PATH", str(tmp_path / "t.db"))
    from db_semantic_mcp.config import reset_config

    reset_config()
    return str(tmp_path / "t.db")


@pytest.mark.asyncio
async def test_list_tables_schema_filter_single(cache_with_tables, monkeypatch):
    from db_semantic_mcp.infrastructure.cache.store import init_store
    from db_semantic_mcp.infrastructure.cache.structural import (
        StructuralSnapshot,
        write_structural_snapshot,
    )
    from db_semantic_mcp.services import metadata_service

    await init_store(cache_with_tables)
    snap = StructuralSnapshot(
        tables=[("dbo", "Users"), ("dbo", "Orders"), ("sales", "Invoice")],
        columns=[],
        primary_keys=[],
        foreign_keys=[],
        indexes=[],
        objects=[],
        comments=[],
    )
    await write_structural_snapshot(cache_with_tables, "testdb", snap)

    result = await metadata_service.list_tables(
        cache_with_tables,
        "testdb",
        schemas=["dbo"],
    )
    names = [(r["schema_name"], r["table_name"]) for r in result]
    assert ("dbo", "Users") in names
    assert ("dbo", "Orders") in names
    assert ("sales", "Invoice") not in names


@pytest.mark.asyncio
async def test_list_tables_schema_filter_multiple(cache_with_tables):
    from db_semantic_mcp.infrastructure.cache.store import init_store
    from db_semantic_mcp.infrastructure.cache.structural import (
        StructuralSnapshot,
        write_structural_snapshot,
    )
    from db_semantic_mcp.services import metadata_service

    await init_store(cache_with_tables)
    snap = StructuralSnapshot(
        tables=[("dbo", "Users"), ("sales", "Invoice"), ("archive", "Old")],
        columns=[],
        primary_keys=[],
        foreign_keys=[],
        indexes=[],
        objects=[],
        comments=[],
    )
    await write_structural_snapshot(cache_with_tables, "testdb", snap)

    result = await metadata_service.list_tables(
        cache_with_tables,
        "testdb",
        schemas=["dbo", "sales"],
    )
    names = {(r["schema_name"], r["table_name"]) for r in result}
    assert names == {("dbo", "Users"), ("sales", "Invoice")}


@pytest.mark.asyncio
async def test_list_tables_keyword_filter(cache_with_tables):
    from db_semantic_mcp.infrastructure.cache.store import init_store
    from db_semantic_mcp.infrastructure.cache.structural import (
        StructuralSnapshot,
        write_structural_snapshot,
    )
    from db_semantic_mcp.services import metadata_service

    await init_store(cache_with_tables)
    snap = StructuralSnapshot(
        tables=[("dbo", "Users"), ("dbo", "UserProfile"), ("dbo", "Orders")],
        columns=[],
        primary_keys=[],
        foreign_keys=[],
        indexes=[],
        objects=[],
        comments=[],
    )
    await write_structural_snapshot(cache_with_tables, "testdb", snap)

    result = await metadata_service.list_tables(
        cache_with_tables,
        "testdb",
        keyword="user",
    )
    names = {r["table_name"] for r in result}
    assert names == {"Users", "UserProfile"}


@pytest.mark.asyncio
async def test_list_tables_schema_plus_keyword_is_and(cache_with_tables):
    from db_semantic_mcp.infrastructure.cache.store import init_store
    from db_semantic_mcp.infrastructure.cache.structural import (
        StructuralSnapshot,
        write_structural_snapshot,
    )
    from db_semantic_mcp.services import metadata_service

    await init_store(cache_with_tables)
    snap = StructuralSnapshot(
        tables=[("dbo", "Users"), ("archive", "Users")],
        columns=[],
        primary_keys=[],
        foreign_keys=[],
        indexes=[],
        objects=[],
        comments=[],
    )
    await write_structural_snapshot(cache_with_tables, "testdb", snap)

    result = await metadata_service.list_tables(
        cache_with_tables,
        "testdb",
        schemas=["dbo"],
        keyword="user",
    )
    names = [(r["schema_name"], r["table_name"]) for r in result]
    assert names == [("dbo", "Users")]


@pytest.mark.asyncio
async def test_get_dependency_chain_schema_filter(cache_with_tables):
    from db_semantic_mcp.infrastructure.cache.store import init_store
    from db_semantic_mcp.infrastructure.cache.structural import (
        StructuralSnapshot,
        write_structural_snapshot,
    )
    from db_semantic_mcp.services import relationship_service

    await init_store(cache_with_tables)
    # A --fk--> B (same schema), A --fk--> X (cross schema)
    snap = StructuralSnapshot(
        tables=[("dbo", "A"), ("dbo", "B"), ("archive", "X")],
        columns=[],
        primary_keys=[],
        foreign_keys=[
            ("dbo", "A", "B_id", "dbo", "B", "Id"),
            ("dbo", "A", "X_id", "archive", "X", "Id"),
        ],
        indexes=[],
        objects=[],
        comments=[],
    )
    await write_structural_snapshot(cache_with_tables, "testdb", snap)

    # No filter: both reachable
    no_filter = await relationship_service.get_dependency_chain(
        cache_with_tables,
        "testdb",
        "dbo",
        "A",
    )
    reached = {(r["schema_name"], r["table_name"]) for r in no_filter}
    assert ("dbo", "B") in reached
    assert ("archive", "X") in reached

    # With filter: only dbo-schema targets
    filtered = await relationship_service.get_dependency_chain(
        cache_with_tables,
        "testdb",
        "dbo",
        "A",
        schemas=["dbo"],
    )
    reached_f = {(r["schema_name"], r["table_name"]) for r in filtered}
    assert ("dbo", "B") in reached_f
    assert ("archive", "X") not in reached_f


@pytest.mark.asyncio
async def test_detect_lookup_tables_confidence_min(cache_with_tables):
    from unittest.mock import patch

    from db_semantic_mcp.services import semantic_service

    async def fake_classify(db_path, database, schema, table, *, force=False):
        # Return varying confidences per table
        if table == "HighConf":
            return {"type": "lookup", "confidence": 0.9, "reasons": []}
        if table == "LowConf":
            return {"type": "lookup", "confidence": 0.3, "reasons": []}
        return {"type": "dimension", "confidence": 0.5, "reasons": []}

    from db_semantic_mcp.infrastructure.cache.store import init_store
    from db_semantic_mcp.infrastructure.cache.structural import (
        StructuralSnapshot,
        write_structural_snapshot,
    )

    await init_store(cache_with_tables)
    snap = StructuralSnapshot(
        tables=[("dbo", "HighConf"), ("dbo", "LowConf")],
        columns=[],
        primary_keys=[],
        foreign_keys=[],
        indexes=[],
        objects=[],
        comments=[],
    )
    await write_structural_snapshot(cache_with_tables, "testdb", snap)

    with patch.object(semantic_service, "classify_table", new=fake_classify):
        all_results = await semantic_service.detect_lookup_tables(
            cache_with_tables,
            "testdb",
        )
        assert len(all_results) == 2

        filtered = await semantic_service.detect_lookup_tables(
            cache_with_tables,
            "testdb",
            confidence_min=0.5,
        )
        assert {r["table_name"] for r in filtered} == {"HighConf"}
