import pytest
import aiosqlite
from unittest.mock import MagicMock

from sqlserver_semantic_mcp.infrastructure.cache.store import init_store
from sqlserver_semantic_mcp.infrastructure.cache.probe import (
    SchemaFingerprints,
    diff_fingerprints,
    fetch_fingerprints,
    read_fingerprints,
    write_fingerprints,
)


def _fps(tables=None, objects=None, dependents=None) -> SchemaFingerprints:
    return SchemaFingerprints(
        tables=tables or {},
        objects=objects or {},
        table_dependents=dependents or {},
    )


def test_diff_no_changes():
    tables = {("dbo", "Users"): "t1|100:3|-"}
    objects = {("dbo", "vw_X", "V"): "t2|abc|200:2"}
    diff = diff_fingerprints(tables, objects, _fps(dict(tables), dict(objects)))
    assert not diff.has_changes


def test_diff_detects_column_type_change():
    # nvarchar(4) -> nvarchar(10): col_fp portion changes
    old = {("dbo", "Users"): "2026-01-01|111:3|-"}
    new = {("dbo", "Users"): "2026-01-01|999:3|-"}
    diff = diff_fingerprints(old, {}, _fps(new))
    assert diff.changed_tables == [("dbo", "Users")]
    assert diff.has_changes


def test_diff_added_and_removed():
    old_t = {("dbo", "A"): "x"}
    new_t = {("dbo", "B"): "y"}
    old_o = {("dbo", "vw_Old", "V"): "x"}
    new_o = {("dbo", "vw_New", "V"): "y", ("dbo", "vw_Old", "V"): "z"}
    diff = diff_fingerprints(old_t, old_o, _fps(new_t, new_o))
    assert diff.added_tables == [("dbo", "B")]
    assert diff.removed_tables == [("dbo", "A")]
    assert diff.changed_objects == [("dbo", "vw_Old", "V")]
    assert diff.added_objects == [("dbo", "vw_New", "V")]


def test_diff_summary_counts():
    diff = diff_fingerprints(
        {("dbo", "A"): "1"}, {}, _fps({("dbo", "A"): "2"}),
    )
    assert diff.summary()["tables_changed"] == 1
    assert diff.summary()["objects_changed"] == 0


def test_fetch_fingerprints_assembles_maps():
    from sqlserver_semantic_mcp.config import Config
    cfg = Config.model_construct(mssql_server="fake", mssql_database="db")
    conn = MagicMock()
    fetch_results = iter([
        # PROBE_COLUMN_FINGERPRINTS: (object_id, col_fp, col_count)
        [(1, 111, 3), (2, 222, 2)],
        # PROBE_INDEX_FINGERPRINTS: (object_id, idx_fp, idx_count)
        [(1, 555, 1)],
        # PROBE_TABLES: (object_id, schema, name, modify_date)
        [(1, "dbo", "Users", "2026-01-01T00:00:00")],
        # PROBE_MODULES: (object_id, schema, name, type, modify_date, def_hash)
        [(2, "dbo", "vw_Users", "V", "2026-01-02T00:00:00", "DEADBEEF")],
        # PROBE_DEPENDENCIES: (referencing_id, referenced_id)
        [(2, 1)],
    ])

    def cursor_factory():
        c = MagicMock()
        c.fetchall.return_value = next(fetch_results)
        return c

    conn.cursor.side_effect = cursor_factory

    fps = fetch_fingerprints(cfg, conn=conn)

    assert fps.tables == {("dbo", "Users"): "2026-01-01T00:00:00|111:3|555:1"}
    assert fps.objects == {
        ("dbo", "vw_Users", "V"): "2026-01-02T00:00:00|DEADBEEF|222:2",
    }
    assert fps.table_dependents == {("dbo", "Users"): [("dbo", "vw_Users")]}


def test_fetch_fingerprints_handles_missing_parts():
    from sqlserver_semantic_mcp.config import Config
    cfg = Config.model_construct(mssql_server="fake", mssql_database="db")
    conn = MagicMock()
    fetch_results = iter([
        [],                                     # columns
        [],                                     # indexes
        [(1, "dbo", "Empty", "2026-01-01")],    # tables
        [(2, "dbo", "fn_X", "FN", "2026-01-01", None)],  # encrypted module
        [],                                     # dependencies
    ])

    def cursor_factory():
        c = MagicMock()
        c.fetchall.return_value = next(fetch_results)
        return c

    conn.cursor.side_effect = cursor_factory
    fps = fetch_fingerprints(cfg, conn=conn)
    assert fps.tables == {("dbo", "Empty"): "2026-01-01|-|-"}
    assert fps.objects == {("dbo", "fn_X", "FN"): "2026-01-01|-|-"}


@pytest.mark.asyncio
async def test_fingerprints_roundtrip(tmp_path):
    db_path = str(tmp_path / "t.db")
    await init_store(db_path)
    fps = _fps(
        tables={("dbo", "Users"): "fp-t"},
        objects={("dbo", "vw_X", "V"): "fp-o"},
    )
    async with aiosqlite.connect(db_path) as db:
        await write_fingerprints(db, "testdb", fps)
        await db.commit()

    tables, objects = await read_fingerprints(db_path, "testdb")
    assert tables == {("dbo", "Users"): "fp-t"}
    assert objects == {("dbo", "vw_X", "V"): "fp-o"}

    # Replacement semantics: a second write fully supersedes the baseline
    fps2 = _fps(tables={("dbo", "Other"): "fp-2"})
    async with aiosqlite.connect(db_path) as db:
        await write_fingerprints(db, "testdb", fps2)
        await db.commit()
    tables, objects = await read_fingerprints(db_path, "testdb")
    assert tables == {("dbo", "Other"): "fp-2"}
    assert objects == {}
