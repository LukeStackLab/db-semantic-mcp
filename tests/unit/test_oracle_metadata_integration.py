from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from db_semantic_mcp.infrastructure.cache.store import init_store
from db_semantic_mcp.infrastructure.cache.structural import StructuralSnapshot, write_structural_snapshot
from db_semantic_mcp.infrastructure.workers import shutdown_db_workers
from db_semantic_mcp.policy.models import PolicyProfile
from db_semantic_mcp.services import metadata_service, oracle_metadata
from db_semantic_mcp.services.policy_service import PolicyService
from tests.unit.test_oracle_policy import config
from tests.unit.test_oracle_resolution import Catalog


@pytest.fixture
def oracle_metadata_fixture(tmp_path, monkeypatch):
    fake = Catalog(monkeypatch)
    cfg = config(cache_path=str(tmp_path / "schema.db"), sources={"sales": {"db_link": "SALES"}})
    policy = PolicyService(cfg)
    policy._policy = PolicyProfile(
        profile_name="readonly",
        scope={"allowed_schemas": ["ERP", "PUBLIC"], "sources": {"sales": {"allowed_schemas": ["ERP"]}}},
    )
    fake.synonyms.update(
        {
            ("local", "ERP", "REMOTE"): ("ERP", "ORDERS", "SALES.EXAMPLE"),
            ("local", "PUBLIC", "PUBLIC_ORDERS"): ("ERP", "ORDERS", None),
        }
    )
    fake.objects["local", "ERP", "Odd.Table"] = "TABLE"

    @contextmanager
    def connection(cfg):
        yield MagicMock()

    monkeypatch.setattr(oracle_metadata, "open_connection", connection)
    monkeypatch.setattr(
        oracle_metadata, "catalog_suffix", lambda cfg, conn: "@SALES.EXAMPLE" if cfg.source_id != "local" else ""
    )
    calls = []

    def fetch(conn, sql, params):
        calls.append((sql, params))
        assert "all_source" not in sql
        assert (
            params == {"owner": "ERP", "name": "ORDERS"}
            or params == {"owner": "ERP", "name": "Odd.Table"}
            or params == {"owner": "ERP", "name": "V"}
        )
        if "all_tab_columns" in sql:
            return [("ID", "NUMBER", 22, "N", None, 1, 19, 0, None, None)]
        if "all_col_comments" in sql:
            return [("ID", "key")]
        if "all_tab_comments" in sql:
            return [("orders",)]
        if "c.constraint_type='P'" in sql:
            return [("ID",)]
        return []

    monkeypatch.setattr(oracle_metadata, "_fetch", fetch)
    return fake, cfg, policy, calls


async def snapshot(cfg):
    await init_store(cfg.cache_path)
    await write_structural_snapshot(
        cfg.cache_path,
        cfg.database,
        StructuralSnapshot(
            tables=[("ERP", "ORDERS")],
            columns=[],
            primary_keys=[],
            foreign_keys=[],
            indexes=[],
            objects=[
                ("ERP", "ORDERS", "TABLE"),
                ("ERP", "REMOTE", "SYNONYM"),
                ("PUBLIC", "PUBLIC_ORDERS", "SYNONYM"),
                ("ERP", "Odd.Table", "TABLE"),
            ],
            comments=[],
        ),
    )


@pytest.mark.asyncio
async def test_list_and_discovery_return_live_alias_target_and_safe_query_reference(oracle_metadata_fixture):
    fake, cfg, policy, _ = oracle_metadata_fixture
    await snapshot(cfg)
    rows = await metadata_service.list_tables(cfg.cache_path, cfg.database, policy=policy)
    remote = next(r for r in rows if r["table_name"] == "REMOTE")
    assert remote["canonical_source"] == "sales" and len(remote["chain"]) == 2
    assert remote["query_reference"] == '"ERP"."REMOTE"'
    assert next(r for r in rows if r["schema_name"] == "PUBLIC")["query_reference"] == '"PUBLIC_ORDERS"'
    from db_semantic_mcp.workflows.discovery_flow import discover_relevant_tables

    result = await discover_relevant_tables("REMOTE", cfg=cfg, policy=policy)
    candidate = result["data"]["candidates"][0]
    assert candidate["canonical_source"] == "sales" and candidate["target"]["name"] == "ORDERS"
    # A cached alias is never an authorization grant after retargeting.
    fake.synonyms["local", "ERP", "REMOTE"] = ("SECRET", "DENIED", None)
    assert not any(
        r["table_name"] == "REMOTE"
        for r in await metadata_service.list_tables(cfg.cache_path, cfg.database, policy=policy)
    )
    await shutdown_db_workers()


@pytest.mark.asyncio
async def test_alias_describe_targets_remote_metadata_without_warming_whole_source(oracle_metadata_fixture):
    _, cfg, policy, calls = oracle_metadata_fixture
    result = await metadata_service.describe_table(cfg.cache_path, cfg.database, "ERP", "REMOTE", policy=policy)
    assert result["column_count"] == 1 and result["columns"][0]["data_type"] == "NUMBER(19,0)"
    assert result["canonical_source"] == "sales" and result["primary_key"] == ["ID"]
    assert all("@SALES.EXAMPLE" in sql and params["name"] == "ORDERS" for sql, params in calls)
    assert result["cache"]["live_verified"]
    await shutdown_db_workers()


@pytest.mark.asyncio
async def test_view_target_and_quoted_dot_keep_decoded_names(oracle_metadata_fixture, monkeypatch):
    fake, cfg, policy, _ = oracle_metadata_fixture
    from db_semantic_mcp.infrastructure.backends import oracle_catalog

    fake.objects["local", "ERP", "V"] = "VIEW"
    fake.synonyms["local", "ERP", "ALIAS_V"] = ("ERP", "V", None)
    monkeypatch.setattr(oracle_catalog, "get_object_definition", lambda *a, **kw: "SELECT ID FROM ERP.ORDERS")
    result = await metadata_service.describe_table(cfg.cache_path, cfg.database, "ERP", "ALIAS_V", policy=policy)
    assert result["target"]["object_type"] == "VIEW" and result["column_count"] == 1
    result = await metadata_service.describe_table(cfg.cache_path, cfg.database, "ERP", "Odd.Table", policy=policy)
    assert result["query_reference"] == '"ERP"."Odd.Table"'
    await shutdown_db_workers()


def test_object_tool_preserves_actual_service_synonym_contract():
    from db_semantic_mcp.server.tools.object_tool import _project_describe_object

    obj = {
        "schema": "ERP",
        "object_name": "A",
        "object_type": "SYNONYM",
        "resolution_chain": [{"source": "local", "name": "A"}, {"source": "sales", "name": "T"}],
        "resolved_target": {"source": "sales", "name": "T"},
        "dependency_completeness": "resolved_path",
        "io_completeness": "unknown",
    }
    result = _project_describe_object(obj, False, 4096)
    assert result["resolution_chain"] == obj["resolution_chain"] and result["resolved_target"] == obj["resolved_target"]
    assert result["io_completeness"] == "unknown"


@pytest.mark.asyncio
async def test_summary_and_classification_resources_resolve_synonym_live(oracle_metadata_fixture, monkeypatch):
    import json
    from types import SimpleNamespace

    from pydantic import AnyUrl

    from db_semantic_mcp.server.resources import schema

    fake, cfg, policy, _ = oracle_metadata_fixture
    cfg.cache_enabled = False
    ctx = SimpleNamespace(cfg=cfg, policy=policy)
    monkeypatch.setattr(schema, "get_context", lambda: ctx)
    summary = json.loads(await schema.read_resource(AnyUrl("semantic://summary/table/ERP.REMOTE")))
    assert summary["data"]["canonical_source"] == "sales" and summary["data"]["pk"] == ["ID"]
    classification = json.loads(await schema.read_resource(AnyUrl("semantic://analysis/classification/ERP.REMOTE")))
    assert classification["data"]["confidence_kind"] == "heuristic"
    fake.synonyms["local", "ERP", "REMOTE"] = ("SECRET", "DENIED", None)
    with pytest.raises(PermissionError):
        await schema.read_resource(AnyUrl("semantic://summary/table/ERP.REMOTE"))
    await shutdown_db_workers()


@pytest.mark.asyncio
async def test_object_resource_preserves_unknown_io_and_decodes_package_body(oracle_metadata_fixture, monkeypatch):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from pydantic import AnyUrl

    from db_semantic_mcp.server.resources import schema

    _, cfg, policy, _ = oracle_metadata_fixture
    cfg.cache_enabled = False
    monkeypatch.setattr(schema, "get_context", lambda: SimpleNamespace(cfg=cfg, policy=policy))
    describe = AsyncMock(
        return_value={
            "schema": "ERP",
            "object_name": "PKG",
            "io_completeness": "unknown",
            "dependency_completeness": "static_catalog_only",
        }
    )
    monkeypatch.setattr(schema.object_service, "describe_object", describe)
    result = json.loads(await schema.read_resource(AnyUrl("semantic://summary/object/PACKAGE%20BODY/ERP.PKG")))
    assert describe.call_args.args[2] == "PACKAGE BODY"
    assert result["data"]["io_completeness"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_secret", [False, True])
async def test_alias_fk_filter_uses_proof_policy_when_disk_changes(
    oracle_metadata_fixture, monkeypatch, initial_secret
):
    from db_semantic_mcp.services import policy_service

    _, cfg, policy, _ = oracle_metadata_fixture
    if initial_secret:
        policy.current_policy().scope.sources["sales"].allowed_schemas.append("SECRET")
    later = policy.current_policy().model_copy(deep=True)
    later.scope.sources["sales"].allowed_schemas = ["ERP"] if initial_secret else ["ERP", "SECRET"]
    loads = []

    def load(config):
        loads.append(config.source_id)
        return later

    monkeypatch.setattr(policy_service, "load_active_policy", load)
    original_fetch = oracle_metadata._fetch

    def fetch(conn, sql, params):
        if "c.constraint_type='R'" in sql:
            return [
                (
                    "ID",
                    "SECRET",
                    "HIDDEN",
                    "ID",
                    "FK_SECRET",
                    1,
                    "ENABLED",
                    "VALIDATED",
                    "NOT DEFERRABLE",
                    "ENABLED",
                    "VALIDATED",
                    "NOT DEFERRABLE",
                )
            ]
        return original_fetch(conn, sql, params)

    monkeypatch.setattr(oracle_metadata, "_fetch", fetch)
    result = await metadata_service.describe_table(cfg.cache_path, cfg.database, "ERP", "REMOTE", policy=policy)
    assert bool(result["foreign_keys"]) is initial_secret
    assert loads == []  # canonical FK filtering must not reload after proof
    await shutdown_db_workers()
