import aiosqlite
import pytest

from db_semantic_mcp.infrastructure.cache.store import init_store
from db_semantic_mcp.infrastructure.cache.structural import StructuralSnapshot, write_structural_snapshot
from db_semantic_mcp.services import metadata_service


class LimitedPolicy:
    def object_allowed(self, schema, name, database=""):
        return schema == "dbo" and name != "Secret"

    def authorize_object(self, schema, name, database=""):
        if not self.object_allowed(schema, name, database):
            raise PermissionError("Object is outside policy scope")


async def make_store(tmp_path):
    path = str(tmp_path / "metadata.db")
    await init_store(path)
    await write_structural_snapshot(
        path,
        "db",
        StructuralSnapshot(
            tables=[("dbo", "Public"), ("dbo", "Secret")],
            columns=[
                ("dbo", "Public", "Id", "int", None, 0, None, 1),
                ("dbo", "Public", "Title", "nvarchar", 50, 1, None, 2),
            ],
            primary_keys=[("dbo", "Public", "Id")],
            foreign_keys=[("dbo", "Public", "Id", "dbo", "Secret", "Id")],
            indexes=[],
            objects=[],
            comments=[],
        ),
    )
    return path


@pytest.mark.asyncio
async def test_metadata_scope_filters_indirect_references(tmp_path):
    path = await make_store(tmp_path)
    policy = LimitedPolicy()
    tables = await metadata_service.list_tables(path, "db", policy=policy)
    assert [t["table_name"] for t in tables] == ["Public"]
    table = await metadata_service.describe_table(path, "db", "dbo", "Public", policy=policy)
    assert table["foreign_keys"] == []
    with pytest.raises(PermissionError):
        await metadata_service.describe_table(path, "db", "dbo", "Secret", policy=policy)


@pytest.mark.asyncio
async def test_describe_table_uses_one_connection(tmp_path, monkeypatch):
    path = await make_store(tmp_path)
    connect = aiosqlite.connect
    queries, connections = [], []

    def tracked_connect(*args, **kwargs):
        connections.append(args[0])
        conn = connect(*args, **kwargs)
        execute = conn.execute

        def tracked_execute(sql, *params, **kw):
            queries.append(sql)
            return execute(sql, *params, **kw)

        conn.execute = tracked_execute
        return conn

    monkeypatch.setattr(metadata_service.aiosqlite, "connect", tracked_connect)
    result = await metadata_service.describe_table(path, "db", "dbo", "Public")
    assert result["classification"]["type"]
    assert len(connections) == 1
    assert any("sc_indexes" in q for q in queries) and any("sc_comments" in q for q in queries)


@pytest.mark.asyncio
async def test_selected_columns_validate_and_mark_omissions(tmp_path):
    path = await make_store(tmp_path)
    result = await metadata_service.describe_table(path, "db", "dbo", "Public", columns=["Title"])
    assert [c["column_name"] for c in result["columns"]] == ["Title"]
    assert result["column_count"] == 2
    assert result["columns_omitted"] is True
    with pytest.raises(ValueError, match="Unknown column"):
        await metadata_service.describe_table(path, "db", "dbo", "Public", columns=["Invented"])


def test_selected_columns_preserve_compound_join_context():
    from db_semantic_mcp.server.tools.metadata import _project_describe_table

    full = {
        "schema_name": "dbo",
        "table_name": "T",
        "columns": [{"column_name": "a", "data_type": "int"}],
        "join_constraints": [
            {"column_pairs": [{"from_column": "a", "to_column": "x"}, {"from_column": "b", "to_column": "y"}]}
        ],
        "required_join_columns": [{"column_name": "b", "sql_type": "decimal(19,4)"}],
    }
    result = _project_describe_table(full, {}, 500)
    assert len(result["join_constraints"][0]["column_pairs"]) == 2
    assert result["required_join_columns"][0]["sql_type"] == "decimal(19,4)"


@pytest.mark.asyncio
async def test_bundle_keeps_all_key_column_types_beyond_display_cap(monkeypatch):
    from db_semantic_mcp.workflows import bundle

    columns = [{"column_name": name, "data_type": "int"} for name in [*(f"p{i}" for i in range(6)), "fa", "fb"]]
    full = {
        "columns": columns,
        "classification": {"type": "dimension"},
        "primary_key": [f"p{i}" for i in range(6)],
        "foreign_keys": [
            {
                "schema_name": "dbo",
                "table_name": "T",
                "column_name": name,
                "ref_schema": "dbo",
                "ref_table": "Parent",
                "ref_column": name,
                "constraint_id": 1,
                "constraint_name": "FK_T",
                "constraint_column_id": i,
            }
            for i, name in enumerate(["fa", "fb"], 1)
        ],
    }

    async def describe(*args, **kwargs):
        return full

    monkeypatch.setattr(bundle.metadata_service, "describe_table", describe)
    result = await bundle._table_summary_for_joining("fake", "db", "dbo", "T")
    assert {col["name"] for col in result["columns"]} == {col["column_name"] for col in columns}
    assert len(result["fk_edges"][0]["column_pairs"]) == 2


@pytest.mark.asyncio
async def test_warmup_persists_only_authorized_catalog(tmp_path, monkeypatch):
    from db_semantic_mcp.config import Config
    from db_semantic_mcp.infrastructure.cache import structural
    from db_semantic_mcp.infrastructure.cache.probe import SchemaFingerprints
    from db_semantic_mcp.policy.models import PolicyFile, PolicyProfile

    path = str(tmp_path / "scoped.db")
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        PolicyFile(
            profiles={"readonly": PolicyProfile(profile_name="readonly", scope={"allowed_tables": ["dbo.Public"]})}
        ).model_dump_json()
    )
    cfg = Config.model_construct(
        server="fake", database="db", cache_path=path, policy_file=str(policy_file)
    )
    await init_store(path)
    snap = StructuralSnapshot(
        tables=[("dbo", "Public"), ("dbo", "Secret")],
        columns=[],
        primary_keys=[],
        indexes=[],
        foreign_keys=[("dbo", "Public", "Id", "dbo", "Secret", "Id")],
        objects=[("dbo", "SecretView", "VIEW")],
        comments=[("dbo", "Secret", "", "hidden")],
    )
    fps = SchemaFingerprints(
        tables={("dbo", "Public"): "1", ("dbo", "Secret"): "2"}, objects={("dbo", "SecretView", "V"): "3"}
    )
    monkeypatch.setattr(structural, "_fetch_warmup_data", lambda _cfg: (fps, snap))
    await structural.warmup_structural_cache(cfg)
    async with aiosqlite.connect(path) as db:
        assert await (await db.execute("SELECT table_name FROM sc_tables")).fetchall() == [("Public",)]
        assert await (await db.execute("SELECT name FROM sc_fingerprints")).fetchall() == [("Public",)]
        assert await (await db.execute("SELECT COUNT(*) FROM sc_foreign_keys")).fetchone() == (0,)
        assert await (await db.execute("SELECT COUNT(*) FROM sc_comments")).fetchone() == (0,)


@pytest.mark.asyncio
async def test_resource_calls_revalidation_and_preserves_payload_on_freshness_error(monkeypatch):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from pydantic import AnyUrl

    from db_semantic_mcp.config import Config
    from db_semantic_mcp.server.resources import schema

    cfg = Config.model_construct(server="fake", database="db", cache_enabled=True)
    monkeypatch.setattr(schema, "get_context", lambda: SimpleNamespace(cfg=cfg))
    monkeypatch.setattr(schema, "_read_resource_payload", AsyncMock(return_value='{"found":true}'))
    schedule = AsyncMock()
    monkeypatch.setattr(schema.revalidation, "maybe_revalidate", schedule)
    monkeypatch.setattr(schema.revalidation, "cache_freshness", AsyncMock(side_effect=RuntimeError("unavailable")))
    result = json.loads(await schema.read_resource(AnyUrl("semantic://summary/database")))
    assert result["found"] is True
    assert result["cache"]["freshness_unknown"] is True
    schedule.assert_awaited_once_with(cfg)


@pytest.mark.asyncio
async def test_old_nonlookup_classification_recomputed_for_compound_fk(tmp_path):
    from db_semantic_mcp.infrastructure.cache.semantic import upsert_table_analysis
    from db_semantic_mcp.services.semantic_service import detect_lookup_tables

    path = str(tmp_path / "old-classifier.db")
    await init_store(path)
    await write_structural_snapshot(
        path,
        "db",
        StructuralSnapshot(
            tables=[("dbo", "T")],
            columns=[
                ("dbo", "T", name, "int", None, 0, None, i) for i, name in enumerate(["Code", "Name", "FkA", "FkB"], 1)
            ],
            primary_keys=[],
            indexes=[],
            objects=[],
            comments=[],
            foreign_keys=[
                ("dbo", "T", name, "dbo", "P", name, 1, "FK_T", i, 0, 0) for i, name in enumerate(["FkA", "FkB"], 1)
            ],
        ),
    )
    async with aiosqlite.connect(path) as db:
        digest = (await (await db.execute("SELECT structural_hash FROM sc_tables")).fetchone())[0]
    await upsert_table_analysis(
        path,
        "db",
        "dbo",
        "T",
        structural_hash=digest,
        status="ready",
        classification={"type": "bridge", "confidence": 0.8},
        is_lookup=False,
    )
    result = await detect_lookup_tables(path, "db")
    assert [row["table_name"] for row in result] == ["T"]


@pytest.mark.asyncio
async def test_scoped_describe_and_lookup_do_not_read_shared_classification(tmp_path, monkeypatch):
    from db_semantic_mcp.services.semantic_service import detect_lookup_tables

    path = await make_store(tmp_path)
    connect = aiosqlite.connect
    queries = []

    def tracked_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        execute = conn.execute

        def tracked_execute(sql, *params, **kw):
            queries.append(sql)
            return execute(sql, *params, **kw)

        conn.execute = tracked_execute
        return conn

    monkeypatch.setattr(metadata_service.aiosqlite, "connect", tracked_connect)
    policy = LimitedPolicy()
    assert await metadata_service.describe_table(path, "db", "dbo", "Public", policy=policy)
    await detect_lookup_tables(path, "db", policy=policy)
    assert not any("sem_table_analysis" in query for query in queries)
