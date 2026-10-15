from unittest.mock import MagicMock, patch

import aiosqlite
import pytest

from db_semantic_mcp.infrastructure.cache.semantic import (
    get_table_analysis,
    upsert_table_analysis,
)
from db_semantic_mcp.infrastructure.cache.store import init_store
from db_semantic_mcp.infrastructure.cache.structural import (
    StructuralSnapshot,
    compute_comment_hash,
    compute_object_hash,
    compute_structural_hash,
    compute_table_hashes,
    fetch_snapshot_from_server,
    read_schema_version,
    write_structural_snapshot,
)


def test_structural_hash_stable():
    tables = [("dbo", "Users"), ("dbo", "Orders")]
    columns = [("dbo", "Users", "Id", "int", None, 0, None, 1)]
    pks = [("dbo", "Users", "Id")]
    fks: list = []
    indexes: list = []

    h1 = compute_structural_hash(tables, columns, pks, fks, indexes)
    h2 = compute_structural_hash(list(reversed(tables)), columns, pks, fks, indexes)
    assert h1 == h2


def test_structural_hash_differs():
    h1 = compute_structural_hash([("dbo", "A")], [], [], [], [])
    h2 = compute_structural_hash([("dbo", "B")], [], [], [], [])
    assert h1 != h2


def test_object_hash():
    objs = [("dbo", "v1", "VIEW"), ("dbo", "p1", "PROCEDURE")]
    h1 = compute_object_hash(objs)
    h2 = compute_object_hash(list(reversed(objs)))
    assert h1 == h2


def test_comment_hash():
    comments = [("dbo", "Users", "", "table desc"), ("dbo", "Users", "Id", "id desc")]
    h1 = compute_comment_hash(comments)
    h2 = compute_comment_hash(list(reversed(comments)))
    assert h1 == h2


@pytest.mark.asyncio
async def test_write_and_read_snapshot(tmp_path):
    db_path = str(tmp_path / "test.db")
    await init_store(db_path)

    snap = StructuralSnapshot(
        tables=[("dbo", "Users")],
        columns=[("dbo", "Users", "Id", "int", None, 0, None, 1)],
        primary_keys=[("dbo", "Users", "Id")],
        foreign_keys=[],
        indexes=[("dbo", "Users", "PK_Users", 1, 1, "Id")],
        objects=[("dbo", "vw_X", "VIEW")],
        comments=[("dbo", "Users", "", "user table")],
    )
    await write_structural_snapshot(db_path, "testdb", snap)

    ver = await read_schema_version(db_path, "testdb")
    assert ver is not None
    assert ver["database_name"] == "testdb"
    assert ver["structural_hash"]
    assert ver["object_hash"]
    assert ver["comment_hash"]

    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT COUNT(*) FROM sc_tables")
        assert (await cur.fetchone())[0] == 1
        cur = await db.execute("SELECT COUNT(*) FROM sc_comments")
        assert (await cur.fetchone())[0] == 1


def _snap(tables, columns) -> StructuralSnapshot:
    return StructuralSnapshot(
        tables=tables,
        columns=columns,
        primary_keys=[],
        foreign_keys=[],
        indexes=[],
        objects=[],
        comments=[],
    )


def test_table_hashes_are_per_table():
    snap = _snap(
        tables=[("dbo", "A"), ("dbo", "B")],
        columns=[
            ("dbo", "A", "Id", "int", None, 0, None, 1),
            ("dbo", "B", "Id", "int", None, 0, None, 1),
        ],
    )
    hashes = compute_table_hashes(snap)
    assert set(hashes) == {("dbo", "A"), ("dbo", "B")}

    # changing A's column type must not affect B's hash
    snap2 = _snap(
        tables=[("dbo", "A"), ("dbo", "B")],
        columns=[
            ("dbo", "A", "Id", "bigint", None, 0, None, 1),
            ("dbo", "B", "Id", "int", None, 0, None, 1),
        ],
    )
    hashes2 = compute_table_hashes(snap2)
    assert hashes2[("dbo", "A")] != hashes[("dbo", "A")]
    assert hashes2[("dbo", "B")] == hashes[("dbo", "B")]


@pytest.mark.asyncio
async def test_targeted_invalidation_only_dirties_changed_table(tmp_path):
    db_path = str(tmp_path / "t.db")
    await init_store(db_path)

    snap_v1 = _snap(
        tables=[("dbo", "A"), ("dbo", "B")],
        columns=[
            ("dbo", "A", "Name", "nvarchar", 4, 0, None, 1),
            ("dbo", "B", "Id", "int", None, 0, None, 1),
        ],
    )
    await write_structural_snapshot(db_path, "testdb", snap_v1)
    hashes_v1 = compute_table_hashes(snap_v1)
    for t in ("A", "B"):
        await upsert_table_analysis(
            db_path,
            "testdb",
            "dbo",
            t,
            structural_hash=hashes_v1[("dbo", t)],
            status="ready",
            classification={"type": "dimension", "confidence": 0.5},
        )

    # nvarchar(4) -> nvarchar(10) on A only
    snap_v2 = _snap(
        tables=[("dbo", "A"), ("dbo", "B")],
        columns=[
            ("dbo", "A", "Name", "nvarchar", 10, 0, None, 1),
            ("dbo", "B", "Id", "int", None, 0, None, 1),
        ],
    )
    await write_structural_snapshot(db_path, "testdb", snap_v2)

    a = await get_table_analysis(db_path, "testdb", "dbo", "A")
    b = await get_table_analysis(db_path, "testdb", "dbo", "B")
    assert a["status"] == "dirty"
    assert b["status"] == "ready"


@pytest.mark.asyncio
async def test_dropped_table_analysis_removed(tmp_path):
    db_path = str(tmp_path / "t.db")
    await init_store(db_path)

    snap_v1 = _snap(
        tables=[("dbo", "A"), ("dbo", "Gone")],
        columns=[("dbo", "A", "Id", "int", None, 0, None, 1)],
    )
    await write_structural_snapshot(db_path, "testdb", snap_v1)
    hashes = compute_table_hashes(snap_v1)
    await upsert_table_analysis(
        db_path,
        "testdb",
        "dbo",
        "Gone",
        structural_hash=hashes[("dbo", "Gone")],
        status="ready",
    )

    snap_v2 = _snap(
        tables=[("dbo", "A")],
        columns=[("dbo", "A", "Id", "int", None, 0, None, 1)],
    )
    await write_structural_snapshot(db_path, "testdb", snap_v2)

    assert await get_table_analysis(db_path, "testdb", "dbo", "Gone") is None


def test_fetch_snapshot_from_server_uses_single_connection():
    cfg = MagicMock()
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    cursor.fetchall.side_effect = [
        [("dbo", "Users")],
        [("dbo", "Users", "Id", "int", None, 0, None, 1)],
        [("dbo", "Users", "Id")],
        [],
        [],
        [],
        [],
    ]

    with patch(
        "db_semantic_mcp.infrastructure.cache.structural.open_connection",
    ) as mock_open:
        mock_open.return_value.__enter__.return_value = conn
        snap = fetch_snapshot_from_server(cfg)

    assert snap.tables == [("dbo", "Users")]
    assert snap.columns[0][2] == "Id"
    assert cursor.execute.call_count == 7
    conn.cursor.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_mode", ["manual", "startup"])
@pytest.mark.parametrize("object_type,type_code", [("VIEW", "V"), ("PROCEDURE", "P")])
async def test_full_warmup_invalidates_changed_module_before_baseline(
    tmp_path,
    monkeypatch,
    refresh_mode,
    object_type,
    type_code,
):
    from db_semantic_mcp.config import Config
    from db_semantic_mcp.infrastructure.cache import revalidation, structural
    from db_semantic_mcp.infrastructure.cache.probe import SchemaFingerprints, read_fingerprints
    from db_semantic_mcp.infrastructure.cache.semantic import get_object_definition, upsert_object_definition
    from db_semantic_mcp.services import object_service

    db_path = str(tmp_path / "full.db")
    cfg = Config.model_construct(server="fake", database="testdb", cache_path=db_path)
    await init_store(db_path)
    snap = StructuralSnapshot(
        tables=[],
        columns=[],
        primary_keys=[],
        foreign_keys=[],
        indexes=[],
        objects=[("dbo", "module", object_type)],
        comments=[],
    )
    old = SchemaFingerprints(tables={}, objects={("dbo", "module", type_code): "old"})
    fresh = SchemaFingerprints(tables={}, objects={("dbo", "module", type_code): "new"})
    hashes = await write_structural_snapshot(db_path, "testdb", snap, fingerprints=old)
    await upsert_object_definition(
        db_path,
        "testdb",
        "dbo",
        "module",
        object_type,
        object_hash=hashes["object_hash"],
        status="ready",
        definition="SELECT 1",
    )
    original_write = structural.write_fingerprints

    async def assert_dirty_then_advance(db, database, fps):
        status = await (await db.execute("SELECT status FROM sem_object_definitions")).fetchone()
        assert status == ("dirty",)
        baseline = await (await db.execute("SELECT fingerprint FROM sc_fingerprints")).fetchone()
        assert baseline == ("old",)
        await original_write(db, database, fps)

    monkeypatch.setattr(structural, "_fetch_warmup_data", lambda _cfg: (fresh, snap))
    monkeypatch.setattr(structural, "write_fingerprints", assert_dirty_then_advance)
    revalidation.reset_state()
    try:
        if refresh_mode == "manual":
            await revalidation.refresh_now(cfg)
        else:
            await structural.warmup_structural_cache(cfg)
        assert (await get_object_definition(db_path, "testdb", "dbo", "module", object_type))["status"] == "dirty"
        assert await read_fingerprints(db_path, "testdb") == ({}, fresh.objects)
        with patch.object(object_service, "_fetch_object", return_value=("SELECT 2", [])) as fetch:
            result = await object_service.describe_object("dbo", "module", object_type, cfg)
        assert result["definition"] == "SELECT 2"
        fetch.assert_called_once()
    finally:
        revalidation.reset_state()


@pytest.mark.asyncio
async def test_full_snapshot_fingerprint_invalidation_rolls_back_with_baseline(tmp_path, monkeypatch):
    from db_semantic_mcp.infrastructure.cache import structural
    from db_semantic_mcp.infrastructure.cache.probe import SchemaFingerprints, read_fingerprints
    from db_semantic_mcp.infrastructure.cache.semantic import get_object_definition, upsert_object_definition

    db_path = str(tmp_path / "rollback.db")
    await init_store(db_path)
    snap = StructuralSnapshot(
        tables=[("dbo", "Users")],
        columns=[],
        primary_keys=[],
        foreign_keys=[],
        indexes=[],
        objects=[("dbo", "module", "VIEW")],
        comments=[],
    )
    old = SchemaFingerprints(tables={("dbo", "Users"): "old-table"}, objects={("dbo", "module", "V"): "old-body"})
    fresh = SchemaFingerprints(
        tables={("dbo", "Users"): "new-table"},
        objects={("dbo", "module", "V"): "new-body"},
        table_dependents={("dbo", "Users"): [("dbo", "module")]},
    )
    hashes = await write_structural_snapshot(db_path, "testdb", snap, fingerprints=old)
    await upsert_object_definition(
        db_path,
        "testdb",
        "dbo",
        "module",
        "VIEW",
        object_hash=hashes["object_hash"],
        status="ready",
        definition="SELECT 1",
    )
    original_write = structural.write_fingerprints

    async def fail_after_baseline(db, database, fps):
        status = await (await db.execute("SELECT status FROM sem_object_definitions")).fetchone()
        assert status == ("dirty",)
        await original_write(db, database, fps)
        raise RuntimeError("injected snapshot failure")

    monkeypatch.setattr(structural, "write_fingerprints", fail_after_baseline)
    with pytest.raises(RuntimeError, match="injected snapshot failure"):
        await write_structural_snapshot(db_path, "testdb", snap, fingerprints=fresh)
    assert await read_fingerprints(db_path, "testdb") == (old.tables, old.objects)
    assert (await get_object_definition(db_path, "testdb", "dbo", "module", "VIEW"))["status"] == "ready"
    assert (await read_schema_version(db_path, "testdb"))["captured_at"] == hashes["captured_at"]


@pytest.mark.asyncio
async def test_full_warmup_dirties_only_changed_table_dependents(tmp_path, monkeypatch):
    from db_semantic_mcp.config import Config
    from db_semantic_mcp.infrastructure.cache import structural
    from db_semantic_mcp.infrastructure.cache.probe import SchemaFingerprints, read_fingerprints
    from db_semantic_mcp.infrastructure.cache.semantic import get_object_definition, upsert_object_definition

    db_path = str(tmp_path / "dependents.db")
    cfg = Config.model_construct(server="fake", database="testdb", cache_path=db_path)
    await init_store(db_path)
    snap = StructuralSnapshot(
        tables=[("dbo", "Users")],
        columns=[],
        primary_keys=[],
        foreign_keys=[],
        indexes=[],
        objects=[("dbo", "dependent", "VIEW"), ("dbo", "unrelated", "VIEW")],
        comments=[],
    )
    objects = {("dbo", "dependent", "V"): "same-body", ("dbo", "unrelated", "V"): "same-body"}
    old = SchemaFingerprints(tables={("dbo", "Users"): "old-table"}, objects=objects)
    fresh = SchemaFingerprints(
        tables={("dbo", "Users"): "new-table"},
        objects=objects,
        table_dependents={("dbo", "Users"): [("dbo", "dependent"), ("dbo", "dependent")]},
    )
    hashes = await write_structural_snapshot(db_path, "testdb", snap, fingerprints=old)
    for name in ("dependent", "unrelated"):
        await upsert_object_definition(
            db_path,
            "testdb",
            "dbo",
            name,
            "VIEW",
            object_hash=hashes["object_hash"],
            status="ready",
            definition="SELECT 1",
        )
    monkeypatch.setattr(structural, "_fetch_warmup_data", lambda _cfg: (fresh, snap))
    result = await structural.warmup_structural_cache(cfg)
    assert result["objects_marked_dirty"] == 1
    assert (await get_object_definition(db_path, "testdb", "dbo", "dependent", "VIEW"))["status"] == "dirty"
    assert (await get_object_definition(db_path, "testdb", "dbo", "unrelated", "VIEW"))["status"] == "ready"
    assert await read_fingerprints(db_path, "testdb") == (fresh.tables, fresh.objects)


@pytest.mark.asyncio
@pytest.mark.parametrize("baseline_mode", ["empty", "partial"])
@pytest.mark.parametrize("object_type,type_code", [("VIEW", "V"), ("PROCEDURE", "P")])
@pytest.mark.parametrize("rollback", [False, True])
async def test_full_warmup_invalidates_legacy_definition_missing_module_baseline(
    tmp_path,
    monkeypatch,
    baseline_mode,
    object_type,
    type_code,
    rollback,
):
    from db_semantic_mcp.config import Config
    from db_semantic_mcp.infrastructure.cache import structural
    from db_semantic_mcp.infrastructure.cache.probe import SchemaFingerprints, read_fingerprints
    from db_semantic_mcp.infrastructure.cache.semantic import get_object_definition, upsert_object_definition
    from db_semantic_mcp.services import object_service

    db_path = str(tmp_path / "legacy.db")
    cfg = Config.model_construct(server="fake", database="testdb", cache_path=db_path)
    await init_store(db_path)
    snap = StructuralSnapshot(
        tables=[("dbo", "Users")],
        columns=[],
        primary_keys=[],
        foreign_keys=[],
        indexes=[],
        objects=[("dbo", "module", object_type), ("dbo", "known", "VIEW")],
        comments=[],
    )
    partial = SchemaFingerprints(tables={("dbo", "Users"): "same-table"}, objects={("dbo", "known", "V"): "same-body"})
    hashes = await write_structural_snapshot(
        db_path, "testdb", snap, fingerprints=partial if baseline_mode == "partial" else None
    )
    await upsert_object_definition(
        db_path,
        "testdb",
        "dbo",
        "module",
        object_type,
        object_hash=hashes["object_hash"],
        status="ready",
        definition="SELECT 1",
    )
    if baseline_mode == "partial":
        await upsert_object_definition(
            db_path,
            "testdb",
            "dbo",
            "known",
            "VIEW",
            object_hash=hashes["object_hash"],
            status="ready",
            definition="SELECT 0",
        )
    old_baseline = await read_fingerprints(db_path, "testdb")
    fresh = SchemaFingerprints(
        tables=partial.tables, objects={**partial.objects, ("dbo", "module", type_code): "new-definition"}
    )
    original_write = structural.write_fingerprints

    async def verify_dirty_then_write(db, database, fps):
        status = await (
            await db.execute(
                "SELECT status FROM sem_object_definitions WHERE object_name='module'",
            )
        ).fetchone()
        assert status == ("dirty",)
        await original_write(db, database, fps)
        if rollback:
            raise RuntimeError("injected legacy baseline failure")

    monkeypatch.setattr(structural, "_fetch_warmup_data", lambda _cfg: (fresh, snap))
    monkeypatch.setattr(structural, "write_fingerprints", verify_dirty_then_write)
    if rollback:
        with pytest.raises(RuntimeError, match="injected legacy baseline failure"):
            await structural.warmup_structural_cache(cfg)
        assert await read_fingerprints(db_path, "testdb") == old_baseline
        definition = await get_object_definition(db_path, "testdb", "dbo", "module", object_type)
        assert definition["status"] == "ready"
        assert definition["definition"] == "SELECT 1"
        assert (await read_schema_version(db_path, "testdb"))["captured_at"] == hashes["captured_at"]
    else:
        result = await structural.warmup_structural_cache(cfg)
        assert result["objects_marked_dirty"] == 1
        assert await read_fingerprints(db_path, "testdb") == (fresh.tables, fresh.objects)
        assert (await get_object_definition(db_path, "testdb", "dbo", "module", object_type))["status"] == "dirty"
        with patch.object(object_service, "_fetch_object", return_value=("SELECT 2", [])) as fetch:
            definition = await object_service.describe_object("dbo", "module", object_type, cfg)
        assert definition["definition"] == "SELECT 2"
        fetch.assert_called_once()
    if baseline_mode == "partial":
        assert (await get_object_definition(db_path, "testdb", "dbo", "known", "VIEW"))["status"] == "ready"
