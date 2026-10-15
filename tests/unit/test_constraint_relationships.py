"""Constraint identity, complete join predicates, and bounded alternatives."""

import aiosqlite
import pytest

from db_semantic_mcp.infrastructure.cache.store import init_store
from db_semantic_mcp.infrastructure.cache.structural import (
    StructuralSnapshot,
    compute_structural_hash,
    write_structural_snapshot,
)
from db_semantic_mcp.infrastructure.queries.metadata_queries import GET_FOREIGN_KEYS
from db_semantic_mcp.services import relationship_service as rel


def _fk(source, column, target, ref_column, identity, ordinal=1, schema="dbo"):
    return (schema, source, column, schema, target, ref_column, identity, f"FK_{identity}", ordinal, 0, 0)


async def _cache(tmp_path, fks, *, columns=(), indexes=(), primary_keys=()):
    tables = sorted({(row[0], row[1]) for row in fks} | {(row[3], row[4]) for row in fks})
    snap = StructuralSnapshot(
        tables=tables,
        columns=list(columns),
        primary_keys=list(primary_keys),
        foreign_keys=fks,
        indexes=list(indexes),
        objects=[],
        comments=[],
    )
    path = str(tmp_path / "constraints.db")
    await init_store(path)
    await write_structural_snapshot(path, "db", snap)
    return path


def test_fk_query_uses_object_identity_and_column_ordinals():
    query = GET_FOREIGN_KEYS.lower()
    assert "sys.foreign_keys" in query
    assert "sys.foreign_key_columns" in query
    assert "constraint_column_id" in query
    assert "constraint_object_id" in query
    assert "referential_constraints" not in query


@pytest.mark.asyncio
async def test_compound_fk_is_one_edge_with_all_pairs(tmp_path):
    path = await _cache(
        tmp_path,
        [
            _fk("Lines", "OrderId", "Orders", "Id", 11, 2),
            _fk("Lines", "TenantId", "Orders", "TenantId", 11, 1),
        ],
    )
    relationships = await rel.get_table_relationships(path, "db", "dbo", "Lines")
    assert len(relationships) == 1
    edge = relationships[0]
    assert edge["constraint_id"] == 11
    assert edge["metadata_complete"] is True
    assert edge["column_pairs"] == [
        {"from_column": "TenantId", "to_column": "TenantId", "ordinal": 1},
        {"from_column": "OrderId", "to_column": "Id", "ordinal": 2},
    ]
    assert "from_column" not in edge
    reverse = await rel.find_join_candidates(path, "db", "dbo", "Orders", "dbo", "Lines")
    assert reverse["path"][0]["column_pairs"][1] == {
        "from_column": "Id",
        "to_column": "OrderId",
        "ordinal": 2,
    }


@pytest.mark.asyncio
async def test_same_constraint_name_across_schemas_stays_separate(tmp_path):
    first = list(_fk("Child", "ParentId", "Parent", "Id", 11, schema="sales"))
    second = list(_fk("Child", "ParentId", "Parent", "Id", 12, schema="audit"))
    first[7] = second[7] = "FK_Child_Parent"
    path = await _cache(tmp_path, [tuple(first), tuple(second)])
    sales = await rel.get_table_relationships(path, "db", "sales", "Child")
    audit = await rel.get_table_relationships(path, "db", "audit", "Child")
    assert sales[0]["constraint_id"] == 11
    assert audit[0]["constraint_id"] == 12
    assert sales[0]["to_schema"] == "sales"


@pytest.mark.asyncio
async def test_parallel_roles_are_ambiguous_and_candidate_cap_is_honest(tmp_path):
    path = await _cache(
        tmp_path,
        [
            _fk("Orders", "BuyerId", "Users", "Id", 11),
            _fk("Orders", "SellerId", "Users", "Id", 12),
        ],
    )
    result = await rel.find_join_candidates(path, "db", "dbo", "Orders", "dbo", "Users")
    assert result["found"] is True
    assert result["ambiguous"] is True
    assert result["path"] == []
    assert len(result["candidates"]) == 2
    assert await rel.find_join_path(path, "db", "dbo", "Orders", "dbo", "Users") is None
    capped = await rel.find_join_candidates(
        path,
        "db",
        "dbo",
        "Orders",
        "dbo",
        "Users",
        max_candidates=1,
    )
    assert capped["ambiguous"] is True
    assert capped["truncated"] is True
    assert len(capped["candidates"]) == 1


@pytest.mark.asyncio
async def test_all_shortest_paths_keep_equal_distance_predecessors(tmp_path):
    path = await _cache(
        tmp_path,
        [
            _fk("A", "BId", "B", "Id", 1),
            _fk("A", "CId", "C", "Id", 2),
            _fk("B", "DId", "D", "Id", 3),
            _fk("C", "DId", "D", "Id", 4),
        ],
    )
    result = await rel.find_join_candidates(path, "db", "dbo", "A", "dbo", "D")
    assert result["ambiguous"] is True
    assert {tuple(e["to_table"] for e in c["path"]) for c in result["candidates"]} == {
        ("B", "D"),
        ("C", "D"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "has_filter,disabled,expected",
    [
        (0, 0, "one_to_one"),
        (1, 0, "many_to_one"),
        (0, 1, "many_to_one"),
    ],
)
async def test_cardinality_uses_valid_unique_key_subset(tmp_path, has_filter, disabled, expected):
    path = await _cache(
        tmp_path,
        [
            _fk("Child", "TenantId", "Parent", "TenantId", 1, 1),
            _fk("Child", "ParentId", "Parent", "Id", 1, 2),
        ],
        columns=[
            ("dbo", "Child", "TenantId", "int", None, 0, None, 1),
            ("dbo", "Child", "ParentId", "int", None, 1, None, 2),
        ],
        indexes=[
            ("dbo", "Child", "UQ_Tenant", 1, 0, '["TenantId"]', has_filter, disabled),
            ("dbo", "Parent", "UQ_Parent", 1, 0, '["TenantId","Id"]', 0, 0),
        ],
    )
    edge = (await rel.get_table_relationships(path, "db", "dbo", "Child"))[0]
    assert edge["type"] == expected
    assert edge["nullable"] is True
    assert edge["cardinality"]["to_min"] == 0


@pytest.mark.asyncio
async def test_legacy_fk_rows_do_not_claim_complete_join_metadata(tmp_path):
    path = await _cache(tmp_path, [("dbo", "Orders", "UserId", "dbo", "Users", "Id")])
    result = await rel.find_join_candidates(path, "db", "dbo", "Orders", "dbo", "Users")
    assert result["path"][0]["metadata_complete"] is False
    assert result["path"][0]["type"] == "unknown"
    assert result["path"][0]["cardinality"]["to_max"] is None
    assert result["metadata_complete"] is False
    assert result["next_action"] != "execute"


@pytest.mark.asyncio
async def test_policy_denied_intermediate_is_not_traversed_or_returned(tmp_path):
    path = await _cache(
        tmp_path,
        [
            _fk("A", "BId", "Secret", "Id", 1),
            _fk("Secret", "CId", "C", "Id", 2),
        ],
    )

    class Policy:
        def object_allowed(self, schema, name, database=""):
            return name != "Secret"

    policy = Policy()
    result = await rel.find_join_candidates(path, "db", "dbo", "A", "dbo", "C", policy=policy)
    assert result["found"] is False
    assert await rel.get_table_relationships(path, "db", "dbo", "A", policy=policy) == []
    assert await rel.get_dependency_chain(path, "db", "dbo", "A", policy=policy) == []
    # A scoped request must not poison a later trusted graph-cache read.
    trusted = await rel.find_join_candidates(path, "db", "dbo", "A", "dbo", "C")
    assert trusted["found"] is True


def test_precision_and_constraint_state_are_structural_hash_inputs():
    column = ("dbo", "A", "Amount", "decimal", None, 0, None, 1, 19, 4, None, None)
    other = (*column[:9], 5, *column[10:])
    assert compute_structural_hash([], [column], [], [], []) != compute_structural_hash([], [other], [], [], [])
    fk = _fk("A", "BId", "B", "Id", 1)
    disabled = (*fk[:-2], 1, 0)
    assert compute_structural_hash([], [], [], [fk], []) != compute_structural_hash([], [], [], [disabled], [])


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled,untrusted,expected_min", [(0, 0, 1), (1, 0, 0), (0, 1, 0)])
async def test_required_parent_needs_enforced_trusted_fk(tmp_path, disabled, untrusted, expected_min):
    fk = (*_fk("Child", "ParentId", "Parent", "Id", 1)[:-2], disabled, untrusted)
    path = await _cache(
        tmp_path,
        [fk],
        columns=[
            ("dbo", "Child", "ParentId", "int", None, 0, None, 1),
        ],
        indexes=[("dbo", "Parent", "UQ_Parent", 1, 0, '["Id"]', 0, 0)],
    )
    edge = (await rel.get_table_relationships(path, "db", "dbo", "Child"))[0]
    assert edge["nullable"] is False
    assert edge["cardinality"]["to_min"] == expected_min


@pytest.mark.asyncio
async def test_missing_compound_ordinal_cannot_claim_complete_predicate(tmp_path):
    path = await _cache(tmp_path, [_fk("Child", "PartB", "Parent", "B", 1, 2)])
    edge = (await rel.get_table_relationships(path, "db", "dbo", "Child"))[0]
    assert edge["metadata_complete"] is False
    assert edge["type"] == "unknown"


@pytest.mark.asyncio
async def test_json_index_names_preserve_commas_and_legacy_does_not_prove_unique(tmp_path):
    assert rel.parse_index_columns('["A,B","Quoted\\"Name"]') == ["A,B", 'Quoted"Name']
    assert rel.parse_index_columns("A,B") == ["A", "B"]
    path = await _cache(
        tmp_path,
        [
            _fk("Child", "A", "Parent", "A", 1, 1),
            _fk("Child", "B", "Parent", "B", 1, 2),
        ],
        indexes=[
            ("dbo", "Child", "UQ_CommaName", 1, 0, '["A,B"]', 0, 0),
            ("dbo", "Child", "UQ_Legacy", 1, 0, "A,B", 0, 0),
            ("dbo", "Parent", "UQ_Parent", 1, 0, '["A","B"]', 0, 0),
        ],
    )
    edge = (await rel.get_table_relationships(path, "db", "dbo", "Child"))[0]
    assert edge["type"] == "many_to_one"


@pytest.mark.asyncio
async def test_precision_roundtrips_and_legacy_null_padding_is_hash_stable(tmp_path):
    column = ("dbo", "Child", "Amount", "decimal", None, 0, None, 1, 19, 4, None, "example")
    path = await _cache(tmp_path, [_fk("Child", "ParentId", "Parent", "Id", 1)], columns=[column])
    async with aiosqlite.connect(path) as db:
        row = await (
            await db.execute("SELECT numeric_precision,numeric_scale,datetime_precision,collation_name FROM sc_columns")
        ).fetchone()
    assert row == (19, 4, None, "example")
    legacy = column[:8]
    assert compute_structural_hash([], [legacy], [], [], []) == compute_structural_hash(
        [],
        [(*legacy, None, None, None, None)],
        [],
        [],
        [],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("limits", [{"max_candidates": 0}, {"max_candidates": 21}, {"max_hops": True}])
async def test_join_search_rejects_unbounded_or_invalid_limits(limits):
    with pytest.raises(ValueError):
        await rel.find_join_candidates("unused", "db", "dbo", "A", "dbo", "B", **limits)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target_key",
    [
        None,
        ("dbo", "Parent", "UQ_Parent", 1, 0, '["Id"]', 0, 1),
        ("dbo", "Parent", "UQ_Parent", 1, 0, '["Id"]', 1, 0),
        ("dbo", "Parent", "UQ_Parent", 1, 0, "Id", None, None),
        ("dbo", "Parent", "UQ_Parent", 1, 0, '["Id","OtherId"]', 0, 0),
    ],
)
async def test_target_uniqueness_needs_live_unfiltered_key_evidence(tmp_path, target_key):
    fk = (*_fk("Child", "ParentId", "Parent", "Id", 1)[:-2], 1, 1)
    path = await _cache(tmp_path, [fk], indexes=[target_key] if target_key else [])
    edge = (await rel.get_table_relationships(path, "db", "dbo", "Child"))[0]
    assert edge["metadata_complete"] is True
    assert edge["type"] == "unknown"
    assert edge["cardinality"]["to_max"] is None
    reversed_path = await rel.find_join_path(path, "db", "dbo", "Parent", "dbo", "Child")
    assert reversed_path[0]["cardinality"]["from_max"] is None


@pytest.mark.asyncio
async def test_xml_spatial_indexes_survive_snapshot_and_full_describe_without_unique_evidence(tmp_path):
    import sqlite3

    import sqlglot

    from db_semantic_mcp.config import Config
    from db_semantic_mcp.infrastructure.cache.structural import fetch_snapshot_from_server
    from db_semantic_mcp.infrastructure.queries import metadata_queries as queries
    from db_semantic_mcp.infrastructure.queries.comment_queries import GET_COMMENTS
    from db_semantic_mcp.services.metadata_service import describe_table

    # Evaluate the query's real WHERE and uniqueness expression against catalog
    # rows. No SQL Server connection is used. A malformed special-index unique
    # flag must not become key evidence when its key_ordinal is zero.
    catalog = sqlite3.connect(":memory:")
    catalog.execute("CREATE TABLE indexes(name TEXT, type INTEGER, is_unique INTEGER)")
    catalog.execute("CREATE TABLE index_columns(key_ordinal INTEGER, is_included_column INTEGER)")
    catalog.execute("INSERT INTO index_columns VALUES(0,0)")
    parsed = sqlglot.parse_one(queries.GET_INDEXES, read="tsql")
    predicate = parsed.args["where"].this.sql(dialect="sqlite")
    unique_expression = parsed.expressions[3].sql(dialect="sqlite")
    indexes = []
    for name, kind in [("IX_Xml", 3), ("IX_Spatial", 4), ("IX_Columnstore", 5)]:
        catalog.execute("DELETE FROM indexes")
        catalog.execute("INSERT INTO indexes VALUES(?,?,1)", (name, kind))
        result = catalog.execute(
            f"SELECT {unique_expression} FROM indexes i CROSS JOIN index_columns ic WHERE {predicate} GROUP BY i.name"
        ).fetchone()
        if result is not None and result[0] is not None:
            indexes.append(("dbo", "Child", name, result[0], 0, '["ParentId"]', 0, 0))
    catalog.close()

    class Cursor:
        def execute(self, query):
            self.rows = {
                queries.GET_TABLES: [("dbo", "Child"), ("dbo", "Parent")],
                queries.GET_COLUMNS: [("dbo", "Child", "ParentId", "int", None, 0, None, 1)],
                queries.GET_PRIMARY_KEYS: [],
                queries.GET_FOREIGN_KEYS: [_fk("Child", "ParentId", "Parent", "Id", 1)],
                queries.GET_INDEXES: indexes + [("dbo", "Parent", "UQ_Parent", 1, 0, '["Id"]', 0, 0)],
                queries.GET_OBJECTS: [],
                GET_COMMENTS: [],
            }[query]

        def fetchall(self):
            return self.rows

        def close(self):
            pass

    class Connection:
        def cursor(self):
            return Cursor()

    cfg = Config.model_construct(server="fake", database="db")
    snapshot = fetch_snapshot_from_server(cfg, conn=Connection())
    path = str(tmp_path / "special-indexes.db")
    await init_store(path)
    await write_structural_snapshot(path, "db", snapshot)
    full = await describe_table(path, "db", "dbo", "Child")
    assert {row["index_name"] for row in full["indexes"]} == {"IX_Xml", "IX_Spatial"}
    assert all(row["columns"] == ["ParentId"] for row in full["indexes"])
    assert all(row["is_unique"] is False for row in full["indexes"])
    edge = (await rel.get_table_relationships(path, "db", "dbo", "Child"))[0]
    assert edge["type"] == "many_to_one"
