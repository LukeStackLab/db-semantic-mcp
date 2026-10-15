import pytest

from db_semantic_mcp.infrastructure.cache.store import init_store
from db_semantic_mcp.infrastructure.cache.structural import (
    StructuralSnapshot,
    write_structural_snapshot,
)
from db_semantic_mcp.services.relationship_service import (
    find_join_path,
    get_dependency_chain,
    get_table_relationships,
)


async def _setup(tmp_path):
    db_path = str(tmp_path / "t.db")
    await init_store(db_path)
    snap = StructuralSnapshot(
        tables=[("dbo", "Users"), ("dbo", "Orders"), ("dbo", "OrderItems"), ("dbo", "Products"), ("dbo", "Isolated")],
        columns=[],
        primary_keys=[
            ("dbo", "Users", "Id"),
            ("dbo", "Orders", "Id"),
            ("dbo", "OrderItems", "Id"),
            ("dbo", "Products", "Id"),
        ],
        foreign_keys=[
            ("dbo", "Orders", "UserId", "dbo", "Users", "Id"),
            ("dbo", "OrderItems", "OrderId", "dbo", "Orders", "Id"),
            ("dbo", "OrderItems", "ProductId", "dbo", "Products", "Id"),
        ],
        indexes=[],
        objects=[],
        comments=[],
    )
    await write_structural_snapshot(db_path, "testdb", snap)
    return db_path


@pytest.mark.asyncio
async def test_outbound_and_inbound(tmp_path):
    db_path = await _setup(tmp_path)
    rels = await get_table_relationships(db_path, "testdb", "dbo", "Orders")
    outbound = [r for r in rels if r["direction"] == "outbound"]
    inbound = [r for r in rels if r["direction"] == "inbound"]
    assert any(r["to_table"] == "Users" for r in outbound)
    assert any(r["from_table"] == "OrderItems" for r in inbound)


@pytest.mark.asyncio
async def test_find_join_path_direct(tmp_path):
    db_path = await _setup(tmp_path)
    path = await find_join_path(db_path, "testdb", "dbo", "Orders", "dbo", "Users")
    assert path is not None
    assert len(path) == 1
    assert path[0]["from_table"] == "Orders"
    assert path[0]["to_table"] == "Users"


@pytest.mark.asyncio
async def test_find_join_path_transitive(tmp_path):
    db_path = await _setup(tmp_path)
    path = await find_join_path(db_path, "testdb", "dbo", "OrderItems", "dbo", "Users")
    assert path is not None
    assert len(path) == 2


@pytest.mark.asyncio
async def test_find_join_path_missing(tmp_path):
    db_path = await _setup(tmp_path)
    # Isolated table has no FKs — truly unreachable
    path = await find_join_path(db_path, "testdb", "dbo", "Users", "dbo", "Isolated")
    assert path is None


@pytest.mark.asyncio
async def test_dependency_chain(tmp_path):
    db_path = await _setup(tmp_path)
    chain = await get_dependency_chain(db_path, "testdb", "dbo", "Users")
    reached = {(c["schema_name"], c["table_name"]) for c in chain}
    assert ("dbo", "Orders") in reached
    assert ("dbo", "OrderItems") in reached


@pytest.mark.asyncio
async def test_fk_graph_cache_refreshes_after_schema_change(tmp_path):
    db_path = await _setup(tmp_path)
    path = await find_join_path(
        db_path,
        "testdb",
        "dbo",
        "OrderItems",
        "dbo",
        "Isolated",
    )
    assert path is None

    snap = StructuralSnapshot(
        tables=[("dbo", "Users"), ("dbo", "Orders"), ("dbo", "OrderItems"), ("dbo", "Products"), ("dbo", "Isolated")],
        columns=[],
        primary_keys=[
            ("dbo", "Users", "Id"),
            ("dbo", "Orders", "Id"),
            ("dbo", "OrderItems", "Id"),
            ("dbo", "Products", "Id"),
            ("dbo", "Isolated", "Id"),
        ],
        foreign_keys=[
            ("dbo", "Orders", "UserId", "dbo", "Users", "Id"),
            ("dbo", "OrderItems", "OrderId", "dbo", "Orders", "Id"),
            ("dbo", "OrderItems", "ProductId", "dbo", "Products", "Id"),
            ("dbo", "Users", "IsolatedId", "dbo", "Isolated", "Id"),
        ],
        indexes=[],
        objects=[],
        comments=[],
    )
    await write_structural_snapshot(db_path, "testdb", snap)

    refreshed = await find_join_path(
        db_path,
        "testdb",
        "dbo",
        "OrderItems",
        "dbo",
        "Isolated",
    )
    assert refreshed is not None
    assert [edge["to_table"] for edge in refreshed] == ["Orders", "Users", "Isolated"]
