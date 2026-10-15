import pytest

from db_semantic_mcp.infrastructure.cache.store import init_store
from db_semantic_mcp.infrastructure.cache.structural import (
    StructuralSnapshot,
    write_structural_snapshot,
)
from db_semantic_mcp.services.semantic_service import (
    analyze_columns,
    classify_table,
    detect_lookup_tables,
)


async def _setup(tmp_path):
    db_path = str(tmp_path / "t.db")
    await init_store(db_path)
    snap = StructuralSnapshot(
        tables=[("dbo", "Users"), ("dbo", "OrderItems"), ("dbo", "StatusCode"), ("dbo", "AuditLog")],
        columns=[
            ("dbo", "OrderItems", "Id", "int", None, 0, None, 1),
            ("dbo", "OrderItems", "OrderId", "int", None, 0, None, 2),
            ("dbo", "OrderItems", "ProductId", "int", None, 0, None, 3),
            ("dbo", "OrderItems", "Quantity", "int", None, 0, None, 4),
            ("dbo", "StatusCode", "Code", "nvarchar", 10, 0, None, 1),
            ("dbo", "StatusCode", "Name", "nvarchar", 50, 0, None, 2),
            ("dbo", "AuditLog", "Id", "int", None, 0, None, 1),
            ("dbo", "AuditLog", "CreatedAt", "datetime", None, 0, None, 2),
            ("dbo", "AuditLog", "CreatedBy", "nvarchar", 100, 0, None, 3),
            ("dbo", "Users", "Id", "int", None, 0, None, 1),
            ("dbo", "Users", "Name", "nvarchar", 100, 0, None, 2),
            ("dbo", "Users", "Email", "nvarchar", 200, 0, None, 3),
        ],
        primary_keys=[
            ("dbo", "OrderItems", "Id"),
            ("dbo", "StatusCode", "Code"),
            ("dbo", "AuditLog", "Id"),
            ("dbo", "Users", "Id"),
        ],
        foreign_keys=[
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
async def test_classify_fact_table(tmp_path):
    db_path = await _setup(tmp_path)
    result = await classify_table(db_path, "testdb", "dbo", "OrderItems")
    assert result["type"] in ("fact", "bridge")
    assert result["confidence"] > 0


@pytest.mark.asyncio
async def test_classify_lookup(tmp_path):
    db_path = await _setup(tmp_path)
    r = await classify_table(db_path, "testdb", "dbo", "StatusCode")
    assert r["type"] == "lookup"


@pytest.mark.asyncio
async def test_classify_audit(tmp_path):
    db_path = await _setup(tmp_path)
    r = await classify_table(db_path, "testdb", "dbo", "AuditLog")
    assert r["type"] == "audit"


@pytest.mark.asyncio
async def test_analyze_columns(tmp_path):
    db_path = await _setup(tmp_path)
    cols = await analyze_columns(db_path, "testdb", "dbo", "AuditLog")
    created_at = next(c for c in cols if c["column"] == "CreatedAt")
    assert created_at["semantic_type"] == "audit_timestamp"
    created_by = next(c for c in cols if c["column"] == "CreatedBy")
    assert created_by["semantic_type"] == "audit_user"


@pytest.mark.asyncio
async def test_detect_lookup_tables(tmp_path):
    db_path = await _setup(tmp_path)
    lookups = await detect_lookup_tables(db_path, "testdb")
    names = [(row["schema_name"], row["table_name"]) for row in lookups]
    assert ("dbo", "StatusCode") in names


async def _scoped_setup(tmp_path):
    from db_semantic_mcp.config import Config
    from db_semantic_mcp.policy.models import PolicyProfile
    from db_semantic_mcp.services.policy_service import PolicyService

    path = str(tmp_path / "scope.db")
    await init_store(path)
    await write_structural_snapshot(
        path,
        "db",
        StructuralSnapshot(
            tables=[("dbo", "T"), ("private", "A"), ("private", "B")],
            columns=[
                ("dbo", "T", name, "int", None, 0, None, i)
                for i, name in enumerate(["Code", "Name", "Hidden1", "Hidden2"], 1)
            ],
            primary_keys=[],
            indexes=[],
            objects=[],
            comments=[],
            foreign_keys=[
                ("dbo", "T", name, "private", target, "Id", i, f"FK_{i}", 1, 0, 0)
                for i, (name, target) in enumerate([("Hidden1", "A"), ("Hidden2", "B")], 1)
            ],
        ),
    )
    cfg = Config.model_construct(server="fake", database="db", cache_path=path)
    policy = PolicyService(cfg)
    policy._policy = PolicyProfile(profile_name="readonly", scope={"allowed_schemas": ["dbo"]})
    return path, cfg, policy


@pytest.mark.asyncio
@pytest.mark.parametrize("warm_trusted", [False, True])
@pytest.mark.parametrize(
    "scope",
    [{"allowed_schemas": ["dbo"]}, {"allowed_tables": ["dbo.T"]}, {"denied_tables": ["private.A", "private.B"]}],
)
async def test_scope_classification_resource_order_does_not_reuse_or_poison_cache(
    tmp_path, monkeypatch, warm_trusted, scope
):
    import json
    from types import SimpleNamespace

    from pydantic import AnyUrl

    from db_semantic_mcp.infrastructure.cache.semantic import get_table_analysis
    from db_semantic_mcp.server.resources import schema as resources
    from db_semantic_mcp.services import metadata_service

    path, cfg, policy = await _scoped_setup(tmp_path)
    from db_semantic_mcp.policy.models import PolicyProfile

    policy._policy = PolicyProfile(profile_name="readonly", scope=scope)
    if warm_trusted:
        assert (await classify_table(path, "db", "dbo", "T"))["type"] == "bridge"
    baseline = await get_table_analysis(path, "db", "dbo", "T")
    before = await metadata_service.describe_table(path, "db", "dbo", "T", policy=policy)
    monkeypatch.setattr(resources, "get_context", lambda: SimpleNamespace(cfg=cfg, policy=policy))
    classified = json.loads(await resources._read_resource_payload(AnyUrl("semantic://analysis/classification/dbo.T")))
    after = await metadata_service.describe_table(path, "db", "dbo", "T", policy=policy)
    assert before["classification"] == classified == after["classification"]
    assert classified["type"] == "lookup"
    assert after["foreign_keys"] == []
    assert await get_table_analysis(path, "db", "dbo", "T") == baseline
    assert (await classify_table(path, "db", "dbo", "T"))["type"] == "bridge"


@pytest.mark.asyncio
async def test_scoped_lookup_summary_columns_and_join_score_use_authorized_fks(tmp_path):
    from types import SimpleNamespace

    from db_semantic_mcp.infrastructure.cache.semantic import get_table_analysis
    from db_semantic_mcp.server.tools.relationship import _score_path
    from db_semantic_mcp.services.semantic_service import summarize_for_joining
    from db_semantic_mcp.workflows.discovery_flow import discover_relevant_tables

    path, cfg, policy = await _scoped_setup(tmp_path)
    await classify_table(path, "db", "dbo", "T")
    baseline = await get_table_analysis(path, "db", "dbo", "T")
    summary = await summarize_for_joining(path, "db", "dbo", "T", policy=policy)
    assert summary["classification"] == "lookup"
    assert summary["join_candidates"] == []
    lookups = await detect_lookup_tables(path, "db", policy=policy)
    assert [row["table_name"] for row in lookups] == ["T"]
    assert [c["column"] for c in await analyze_columns(path, "db", "dbo", "T", policy=policy)] == [
        "Code",
        "Name",
        "Hidden1",
        "Hidden2",
    ]
    scored = await _score_path(SimpleNamespace(cfg=cfg, policy=policy), [{"to_schema": "dbo", "to_table": "T"}])
    assert scored["confidence"] == 0.9
    assert scored["penalties"][0]["classification"] == "lookup"
    discovered = await discover_relevant_tables("dbo.T", classify=True, cfg=cfg, policy=policy)
    assert discovered["data"]["candidates"][0]["classification"] == "lookup"
    assert await get_table_analysis(path, "db", "dbo", "T") == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_scoped_classification_and_columns_do_not_read_shared_analysis(tmp_path, monkeypatch, force):
    from db_semantic_mcp.services import semantic_service

    path, _, policy = await _scoped_setup(tmp_path)

    async def forbidden(*args, **kwargs):
        pytest.fail("scoped request accessed shared classification cache")

    monkeypatch.setattr(semantic_service, "get_table_analysis", forbidden)
    monkeypatch.setattr(semantic_service, "upsert_table_analysis", forbidden)
    assert (await classify_table(path, "db", "dbo", "T", policy=policy, force=force))["type"] == "lookup"
    assert len(await analyze_columns(path, "db", "dbo", "T", policy=policy)) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("database_only", [False, True])
async def test_unrestricted_policy_reuses_classification_cache(tmp_path, monkeypatch, database_only):
    from db_semantic_mcp.policy.models import PolicyProfile
    from db_semantic_mcp.services import metadata_service, semantic_service

    path, _, policy = await _scoped_setup(tmp_path)
    policy._policy = PolicyProfile(
        profile_name="readonly", scope={"allowed_databases": ["db"]} if database_only else {}
    )
    expected = await classify_table(path, "db", "dbo", "T", policy=policy)

    def forbidden(*args, **kwargs):
        pytest.fail("ready unrestricted classification should be reused")

    monkeypatch.setattr(semantic_service, "_classify", forbidden)
    assert await classify_table(path, "db", "dbo", "T", policy=policy) == expected
    assert (await metadata_service.describe_table(path, "db", "dbo", "T", policy=policy))["classification"] == expected
    assert await detect_lookup_tables(path, "db", schemas=["dbo"], policy=policy) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("warm_trusted", [False, True])
@pytest.mark.parametrize("entry", ["classify", "force", "columns"])
async def test_policy_reload_during_classification_does_not_poison_shared_cache(
    tmp_path, monkeypatch, warm_trusted, entry
):
    import asyncio

    from db_semantic_mcp.infrastructure.cache.semantic import get_table_analysis
    from db_semantic_mcp.policy.models import PolicyProfile
    from db_semantic_mcp.services import semantic_service

    path, _, policy = await _scoped_setup(tmp_path)
    policy._policy = PolicyProfile(profile_name="readonly")
    if warm_trusted:
        assert (await classify_table(path, "db", "dbo", "T"))["type"] == "bridge"
    baseline = await get_table_analysis(path, "db", "dbo", "T")
    paused, resume = asyncio.Event(), asyncio.Event()
    # Force skips the analysis read, so pause its structural read instead.
    # Both wrappers execute the real SQLite operation before yielding.
    pause_name = "_load_table_structure" if entry == "force" else "get_table_analysis"
    original = getattr(semantic_service, pause_name)
    reads = []

    async def controlled_read(*args, **kwargs):
        reads.append(args)
        result = await original(*args, **kwargs)
        paused.set()
        await resume.wait()
        return result

    monkeypatch.setattr(semantic_service, pause_name, controlled_read)
    operation = (
        analyze_columns(path, "db", "dbo", "T", policy=policy)
        if entry == "columns"
        else classify_table(path, "db", "dbo", "T", policy=policy, force=entry == "force")
    )
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(paused.wait(), timeout=5)
        policy._policy = PolicyProfile(profile_name="readonly", scope={"allowed_schemas": ["dbo"]})
        resume.set()
        result = await asyncio.wait_for(task, timeout=5)
    finally:
        resume.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    if entry == "columns":
        assert [row["column"] for row in result] == ["Code", "Name", "Hidden1", "Hidden2"]
    else:
        assert result["type"] == "lookup"
    assert await get_table_analysis(path, "db", "dbo", "T") == baseline
    assert len(reads) == 1
    monkeypatch.setattr(semantic_service, pause_name, original)
    assert (await classify_table(path, "db", "dbo", "T"))["type"] == "bridge"


@pytest.mark.asyncio
@pytest.mark.parametrize("warm_trusted", [False, True])
@pytest.mark.parametrize("entry", ["classify", "force", "columns", "scoped_columns", "columns_cache"])
async def test_database_scope_reload_reauthorizes_source_without_poisoning_cache(
    tmp_path, monkeypatch, warm_trusted, entry
):
    import asyncio

    from db_semantic_mcp.infrastructure.cache.semantic import get_table_analysis
    from db_semantic_mcp.policy.models import PolicyProfile
    from db_semantic_mcp.services import semantic_service

    path, _, policy = await _scoped_setup(tmp_path)
    policy._policy = PolicyProfile(
        profile_name="readonly", scope={"allowed_schemas": ["dbo"]} if entry == "scoped_columns" else {}
    )
    if warm_trusted:
        assert (await classify_table(path, "db", "dbo", "T"))["type"] == "bridge"
    baseline = await get_table_analysis(path, "db", "dbo", "T")
    paused, resume = asyncio.Event(), asyncio.Event()
    pause_name = "_load_table_structure" if entry in {"force", "scoped_columns"} else "get_table_analysis"
    original = getattr(semantic_service, pause_name)
    reads = []

    async def controlled_read(*args, **kwargs):
        result = await original(*args, **kwargs)
        reads.append(args)
        if len(reads) == (2 if entry == "columns_cache" else 1):
            paused.set()
            await resume.wait()
        return result

    monkeypatch.setattr(semantic_service, pause_name, controlled_read)
    operation = (
        analyze_columns(path, "db", "dbo", "T", policy=policy)
        if entry in {"columns", "scoped_columns", "columns_cache"}
        else classify_table(path, "db", "dbo", "T", policy=policy, force=entry == "force")
    )
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(paused.wait(), timeout=5)
        # The columns cache read can follow an authorized cold-cache write.
        # Preserve the snapshot present immediately before authorization changes.
        baseline = await get_table_analysis(path, "db", "dbo", "T")
        policy._policy = PolicyProfile(profile_name="readonly", scope={"allowed_databases": ["other"]})
        resume.set()
        with pytest.raises(PermissionError, match="denied by policy scope"):
            await asyncio.wait_for(task, timeout=5)
    finally:
        resume.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert await get_table_analysis(path, "db", "dbo", "T") == baseline
    monkeypatch.setattr(semantic_service, pause_name, original)
    policy._policy = PolicyProfile(profile_name="readonly")
    assert (await classify_table(path, "db", "dbo", "T", policy=policy))["type"] == "bridge"
