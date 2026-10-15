"""Tests for the join-summary heuristic folded into describe_table (P3)."""

import pytest


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "testdb")
    monkeypatch.setenv("SEMANTIC_MCP_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_PASSWORD", "p")
    monkeypatch.setenv("SEMANTIC_MCP_CACHE_PATH", str(tmp_path / "t.db"))
    from db_semantic_mcp.config import reset_config

    reset_config()
    return str(tmp_path / "t.db")


async def _seed(cache_path: str):
    from db_semantic_mcp.infrastructure.cache.store import init_store
    from db_semantic_mcp.infrastructure.cache.structural import (
        StructuralSnapshot,
        write_structural_snapshot,
    )

    await init_store(cache_path)
    # dbo.Orders (Id PK, UserId FK→Users.Id, ProductId FK→Product.Id,
    #             Status int, CreatedAt datetime, DeletedAt datetime)
    snap = StructuralSnapshot(
        tables=[("dbo", "Orders"), ("dbo", "Users"), ("dbo", "Product"), ("dbo", "Empty")],
        columns=[
            ("dbo", "Orders", "Id", "int", 4, 0, None, 1),
            ("dbo", "Orders", "UserId", "int", 4, 0, None, 2),
            ("dbo", "Orders", "ProductId", "int", 4, 0, None, 3),
            ("dbo", "Orders", "Status", "int", 4, 0, None, 4),
            ("dbo", "Orders", "CreatedAt", "datetime", 8, 0, None, 5),
            ("dbo", "Orders", "DeletedAt", "datetime", 8, 1, None, 6),
            ("dbo", "Empty", "Id", "int", 4, 0, None, 1),
        ],
        primary_keys=[("dbo", "Orders", "Id"), ("dbo", "Empty", "Id")],
        foreign_keys=[
            ("dbo", "Orders", "UserId", "dbo", "Users", "Id"),
            ("dbo", "Orders", "ProductId", "dbo", "Product", "Id"),
        ],
        indexes=[],
        objects=[],
        comments=[],
    )
    await write_structural_snapshot(cache_path, "testdb", snap)


@pytest.mark.asyncio
async def test_summarize_for_joining_shape(cache):
    await _seed(cache)
    from db_semantic_mcp.services import semantic_service

    result = await semantic_service.summarize_for_joining(
        cache,
        "testdb",
        "dbo",
        "Orders",
    )
    assert result["table"] == "dbo.Orders"
    assert result["pk"] == ["Id"]
    assert {c["via_column"] for c in result["join_candidates"]} == {"UserId", "ProductId"}
    # Common filters include Status (status tag) and CreatedAt/DeletedAt (audit_timestamp/soft_delete)
    cf = set(result["common_filter_columns"])
    assert "Status" in cf
    assert "CreatedAt" in cf
    assert "DeletedAt" in cf


@pytest.mark.asyncio
async def test_summarize_for_joining_table_with_no_fks(cache):
    await _seed(cache)
    from db_semantic_mcp.services import semantic_service

    result = await semantic_service.summarize_for_joining(
        cache,
        "testdb",
        "dbo",
        "Empty",
    )
    assert result["table"] == "dbo.Empty"
    assert result["join_candidates"] == []


@pytest.mark.asyncio
async def test_summarize_for_joining_nonexistent_table(cache):
    await _seed(cache)
    from db_semantic_mcp.services import semantic_service

    result = await semantic_service.summarize_for_joining(
        cache,
        "testdb",
        "dbo",
        "Missing",
    )
    assert result is None
