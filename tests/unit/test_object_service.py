from unittest.mock import patch

import pytest

from sqlserver_semantic_mcp.config import get_config, reset_config
from sqlserver_semantic_mcp.infrastructure.cache.store import init_store
from sqlserver_semantic_mcp.infrastructure.cache.structural import (
    StructuralSnapshot,
    write_structural_snapshot,
)
from sqlserver_semantic_mcp.services.object_service import describe_object


@pytest.mark.asyncio
async def test_describe_object_uses_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_DATABASE", "testdb")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_PASSWORD", "p")
    monkeypatch.setenv("SEMANTIC_MCP_CACHE_PATH", str(tmp_path / "t.db"))
    reset_config()
    cfg = get_config()

    await init_store(cfg.cache_path)
    snap = StructuralSnapshot(
        tables=[],
        columns=[],
        primary_keys=[],
        foreign_keys=[],
        indexes=[],
        objects=[("dbo", "v1", "VIEW")],
        comments=[],
    )
    await write_structural_snapshot(cfg.cache_path, "testdb", snap)

    with (
        patch("sqlserver_semantic_mcp.services.object_service.fetch_one") as fo,
        patch("sqlserver_semantic_mcp.services.object_service.fetch_all") as fa,
    ):
        fo.return_value = ("CREATE VIEW v1 AS SELECT 1",)
        fa.return_value = [("dbo", "Users", "USER_TABLE")]
        result = await describe_object("dbo", "v1", "VIEW", cfg=cfg)

    assert result["definition"].startswith("CREATE VIEW")
    assert "dbo.Users" in result["dependencies"]
    # P3: affected_tables now means write-targets. A SELECT-only view has none;
    # dbo.Users appears in read_tables instead.
    assert result["affected_tables"] == []
    # Note: v1's body is "SELECT 1" (no FROM), so read_tables is also empty.
    # For a richer read_tables test see test_object_write_intent.py.


@pytest.mark.asyncio
async def test_object_scope_is_checked_before_cache_or_db(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from sqlserver_semantic_mcp.config import Config

    policy = MagicMock()
    policy.authorize_object.side_effect = PermissionError("denied")
    cfg = Config(_env_file=None, mssql_server="fake", mssql_database="db", mssql_user="u", mssql_password="p")
    with (
        patch("sqlserver_semantic_mcp.services.object_service.read_schema_version", AsyncMock()) as read,
        patch("sqlserver_semantic_mcp.services.object_service.fetch_one") as fetch,
    ):
        with pytest.raises(PermissionError):
            await describe_object("private", "secret", "VIEW", cfg=cfg, policy=policy)
    read.assert_not_awaited()
    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_cached_definition_with_denied_dependency_is_withheld(tmp_path):
    from unittest.mock import MagicMock

    from sqlserver_semantic_mcp.config import Config
    from sqlserver_semantic_mcp.infrastructure.cache.semantic import upsert_object_definition

    cfg = Config(
        _env_file=None,
        mssql_server="fake",
        mssql_database="db",
        mssql_user="u",
        mssql_password="p",
        cache_path=str(tmp_path / "scope.db"),
    )
    await init_store(cfg.cache_path)
    version = await write_structural_snapshot(
        cfg.cache_path,
        "db",
        StructuralSnapshot(
            tables=[],
            columns=[],
            primary_keys=[],
            foreign_keys=[],
            indexes=[],
            objects=[("dbo", "v", "VIEW")],
            comments=[],
        ),
    )
    await upsert_object_definition(
        cfg.cache_path,
        "db",
        "dbo",
        "v",
        "VIEW",
        object_hash=version["object_hash"],
        status="ready",
        definition="CREATE VIEW dbo.v AS SELECT * FROM private.Secret JOIN dbo.Public ON 1=1",
        dependencies=["private.Secret", "dbo.Public"],
    )
    policy = MagicMock()
    policy.object_allowed.side_effect = lambda schema, name, database="": schema == "dbo"
    result = await describe_object("dbo", "v", "VIEW", cfg=cfg, policy=policy)
    assert result["definition"] is None
    assert result["definition_withheld"] is True
    assert result["dependencies_omitted"] is True
    assert result["dependencies"] == ["dbo.Public"]
    assert result["read_tables"] == ["dbo.Public"]
    assert "Secret" not in str(result)


@pytest.mark.asyncio
async def test_definition_detects_cross_database_even_when_catalog_dependencies_miss_it(tmp_path):
    from unittest.mock import MagicMock

    from sqlserver_semantic_mcp.config import Config
    from sqlserver_semantic_mcp.infrastructure.cache.semantic import upsert_object_definition

    cfg = Config(
        _env_file=None,
        mssql_server="fake",
        mssql_database="db",
        mssql_user="u",
        mssql_password="p",
        cache_path=str(tmp_path / "cross.db"),
    )
    await init_store(cfg.cache_path)
    version = await write_structural_snapshot(
        cfg.cache_path,
        "db",
        StructuralSnapshot(
            tables=[],
            columns=[],
            primary_keys=[],
            foreign_keys=[],
            indexes=[],
            objects=[("dbo", "v", "VIEW")],
            comments=[],
        ),
    )
    await upsert_object_definition(
        cfg.cache_path,
        "db",
        "dbo",
        "v",
        "VIEW",
        object_hash=version["object_hash"],
        status="ready",
        definition="CREATE VIEW dbo.v AS SELECT * FROM otherdb.dbo.Secret",
        dependencies=[],
    )
    policy = MagicMock()
    policy.object_allowed.side_effect = lambda schema, name, database="": database == "db"
    result = await describe_object("dbo", "v", "VIEW", cfg=cfg, policy=policy)
    assert result["definition"] is None
    assert result["definition_withheld"] is True
    assert "otherdb" not in str(result)


@pytest.mark.asyncio
async def test_object_full_connection_lifecycles_stay_in_worker(tmp_path, monkeypatch):
    from threading import get_ident
    from unittest.mock import MagicMock

    from sqlserver_semantic_mcp.config import Config

    cfg = Config(
        _env_file=None,
        mssql_server="fake",
        mssql_database="db",
        mssql_user="u",
        mssql_password="p",
        cache_path=str(tmp_path / "threads.db"),
    )
    await init_store(cfg.cache_path)
    events = []
    loop_thread = get_ident()

    def connect(**kwargs):
        events.append(("connect", get_ident()))
        conn = MagicMock()
        cursor = conn.cursor.return_value
        cursor.execute.side_effect = lambda *args: events.append(("execute", get_ident()))
        cursor.fetchone.return_value = ("CREATE VIEW dbo.v AS SELECT 1",)
        cursor.fetchall.return_value = []
        cursor.close.side_effect = lambda: events.append(("cursor_close", get_ident()))
        conn.close.side_effect = lambda: events.append(("connection_close", get_ident()))
        return conn

    monkeypatch.setattr("sqlserver_semantic_mcp.infrastructure.connection.pymssql.connect", connect)
    result = await describe_object("dbo", "v", "VIEW", cfg=cfg)
    assert result["status"] == "ready"
    assert [name for name, _ in events] == ["connect", "execute", "cursor_close", "connection_close"] * 2
    assert len({thread for _, thread in events}) == 1
    assert events[0][1] != loop_thread


@pytest.mark.parametrize(
    "definition",
    [
        "CREATE VIEW dbo.v AS SELECT * FROM private.Secret",
        "CREATE VIEW dbo.v AS SELECT * FROM otherdb.dbo.Secret",
        "CREATE VIEW dbo.v AS SELECT otherdb.dbo.SecretFn(1)",
        "CREATE VIEW dbo.v AS SELECT private.SecretFn(1)",
        "CREATE PROCEDURE dbo.v AS EXEC('SELECT * FROM private.Secret')",
        "CREATE VIEW dbo.v AS SELECT * FROM Secret WHERE EXISTS (WITH Secret AS (SELECT 1 AS x) SELECT * FROM Secret)",
    ],
)
def test_definition_scope_rejects_hidden_and_unresolved_references(definition):
    from sqlserver_semantic_mcp.services.object_service import _definition_allowed

    class Policy:
        def object_allowed(self, schema, name, database=""):
            return schema == "dbo" and database == "db"

    assert _definition_allowed(definition, Policy(), "db", "dbo", "v") is False


def test_definition_scope_resolves_cte_without_rejecting_allowed_physical_source():
    from sqlserver_semantic_mcp.services.object_service import _definition_allowed

    class Policy:
        def object_allowed(self, schema, name, database=""):
            return schema == "dbo" and database == "db"

    assert (
        _definition_allowed(
            "CREATE VIEW dbo.v AS WITH visible AS (SELECT * FROM dbo.Public) SELECT * FROM visible",
            Policy(),
            "db",
            "dbo",
            "v",
        )
        is True
    )


@pytest.mark.asyncio
async def test_restricted_object_is_redacted_before_persisting_and_on_cache_hit(tmp_path):
    from unittest.mock import MagicMock

    from sqlserver_semantic_mcp.config import Config
    from sqlserver_semantic_mcp.infrastructure.cache.semantic import get_object_definition

    cfg = Config(
        _env_file=None,
        mssql_server="fake",
        mssql_database="db",
        mssql_user="u",
        mssql_password="p",
        cache_path=str(tmp_path / "redacted.db"),
    )
    await init_store(cfg.cache_path)
    policy = MagicMock()
    policy.object_allowed.side_effect = lambda schema, name, database="": schema == "dbo"
    with (
        patch(
            "sqlserver_semantic_mcp.services.object_service.fetch_one",
            return_value=("CREATE VIEW dbo.v AS SELECT * FROM private.Secret JOIN dbo.Public ON 1=1",),
        ),
        patch(
            "sqlserver_semantic_mcp.services.object_service.fetch_all",
            return_value=[("private", "Secret", "USER_TABLE"), ("dbo", "Public", "USER_TABLE")],
        ),
    ):
        result = await describe_object("dbo", "v", "VIEW", cfg=cfg, policy=policy)
    stored = await get_object_definition(cfg.cache_path, "db", "dbo", "v", "VIEW")
    assert stored["status"] == "restricted"
    assert stored["definition"] is None
    assert stored["dependencies"] == ["dbo.Public"]
    assert "Secret" not in str(stored)
    with patch("sqlserver_semantic_mcp.services.object_service.fetch_one") as fetch:
        again = await describe_object("dbo", "v", "VIEW", cfg=cfg, policy=policy)
    fetch.assert_not_called()
    assert again["definition_withheld"] is True
    assert again["dependencies_omitted"] is True
    assert again["definition"] is None
    assert result["status"] == again["status"] == "restricted"


def test_unsupported_definition_never_logs_raw_body(caplog):
    import logging
    from unittest.mock import MagicMock

    from sqlserver_semantic_mcp.services.object_service import _definition_allowed

    body = "CREATE PROCEDURE dbo.v AS BEGIN PRINT 'SECRET_PAYLOAD_8765'; END"
    with caplog.at_level(logging.WARNING):
        assert _definition_allowed(body, MagicMock(), "db", "dbo", "v") is False
    assert "SECRET_PAYLOAD_8765" not in caplog.text


@pytest.mark.parametrize(
    "definition",
    [
        "CREATE PROCEDURE dbo.Public AS CREATE TABLE other.dbo.Public(Id int)",
        "CREATE PROCEDURE dbo.Public AS CREATE TABLE other.dbo.Secret(Id int)",
        "CREATE VIEW other.dbo.Public AS SELECT 1",
        "CREATE PROCEDURE dbo.Public AS SELECT OBJECT_DEFINITION(OBJECT_ID('other.dbo.Secret'))",
        "CREATE PROCEDURE dbo.Public AS SELECT OBJECT_NAME(123)",
    ],
)
def test_definition_root_target_and_metadata_functions_cannot_bypass_restricted_scope(definition):
    from sqlserver_semantic_mcp.config import Config
    from sqlserver_semantic_mcp.policy.models import PolicyProfile, PolicyScope
    from sqlserver_semantic_mcp.services.object_service import _definition_allowed
    from sqlserver_semantic_mcp.services.policy_service import PolicyService

    cfg = Config(_env_file=None, mssql_server="fake", mssql_database="app", mssql_user="u", mssql_password="p")
    policy = PolicyService(cfg)
    policy._policy = PolicyProfile(
        profile_name="test",
        scope=PolicyScope(allowed_databases=["app"], allowed_schemas=["dbo"], allowed_tables=["dbo.Public"]),
    )
    assert _definition_allowed(definition, policy, "app", "dbo", "Public") is False


def test_definition_authorized_root_module_target_is_allowed():
    from sqlserver_semantic_mcp.config import Config
    from sqlserver_semantic_mcp.policy.models import PolicyProfile, PolicyScope
    from sqlserver_semantic_mcp.services.object_service import _definition_allowed
    from sqlserver_semantic_mcp.services.policy_service import PolicyService

    cfg = Config(_env_file=None, mssql_server="fake", mssql_database="app", mssql_user="u", mssql_password="p")
    policy = PolicyService(cfg)
    policy._policy = PolicyProfile(
        profile_name="test",
        scope=PolicyScope(allowed_databases=["app"], allowed_schemas=["dbo"], allowed_tables=["dbo.Public"]),
    )
    assert _definition_allowed("CREATE PROCEDURE dbo.Public AS SELECT 1", policy, "app", "dbo", "Public") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "definition",
    [
        "CREATE VIEW dbo.Public AS SELECT * FROM dbo.Users FOR SYSTEM_TIME ALL",
        "CREATE VIEW dbo.Public AS SELECT * FROM dbo.Users FOR SYSTEM_TIME AS OF '2026-01-01'",
        "CREATE PROCEDURE dbo.Public AS CREATE EXTERNAL TABLE dbo.Users(Id INT) WITH "
        "(LOCATION = '/users', DATA_SOURCE = remote, FILE_FORMAT = csv)",
        "CREATE EXTERNAL VIEW dbo.Public AS SELECT 1",
        "CREATE VIEW dbo.Public AS SELECT FileTableRootPath(N'dbo.UsersHistory')",
        "CREATE PROCEDURE dbo.Public AS SELECT STATS_DATE(1, 2)",
    ],
)
async def test_cached_opaque_sources_and_metadata_definitions_are_withheld(tmp_path, definition):
    from sqlserver_semantic_mcp.config import Config
    from sqlserver_semantic_mcp.infrastructure.cache.semantic import upsert_object_definition
    from sqlserver_semantic_mcp.policy.models import PolicyProfile, PolicyScope
    from sqlserver_semantic_mcp.services.policy_service import PolicyService

    cfg = Config(_env_file=None, mssql_server="fake", mssql_database="app", cache_path=str(tmp_path / "opaque.db"))
    policy = PolicyService(cfg)
    policy._policy = PolicyProfile(
        profile_name="test",
        scope=PolicyScope(
            allowed_tables=["dbo.Public", "dbo.Users"],
            denied_tables=["dbo.UsersHistory"],
        ),
    )
    object_type = "PROCEDURE" if definition.startswith("CREATE PROCEDURE") else "VIEW"
    await init_store(cfg.cache_path)
    version = await write_structural_snapshot(
        cfg.cache_path,
        "app",
        StructuralSnapshot(
            tables=[],
            columns=[],
            primary_keys=[],
            foreign_keys=[],
            indexes=[],
            objects=[("dbo", "Public", object_type)],
            comments=[],
        ),
    )
    await upsert_object_definition(
        cfg.cache_path,
        "app",
        "dbo",
        "Public",
        object_type,
        object_hash=version["object_hash"],
        status="ready",
        definition=definition,
        dependencies=["dbo.Users"],
    )
    with patch("sqlserver_semantic_mcp.services.object_service.fetch_one") as fetch:
        result = await describe_object("dbo", "Public", object_type, cfg=cfg, policy=policy)
    fetch.assert_not_called()
    assert result["definition"] is None
    assert result["definition_withheld"] is True
    assert result["dependencies"] == ["dbo.Users"]


def test_restricted_definition_preserves_safe_scalar_queries():
    from sqlserver_semantic_mcp.config import Config
    from sqlserver_semantic_mcp.policy.models import PolicyProfile, PolicyScope
    from sqlserver_semantic_mcp.services.object_service import _definition_allowed
    from sqlserver_semantic_mcp.services.policy_service import PolicyService

    policy = PolicyService(Config(_env_file=None, mssql_server="fake", mssql_database="app"))
    policy._policy = PolicyProfile(
        profile_name="test",
        scope=PolicyScope(
            allowed_tables=["dbo.Public", "dbo.Users"],
        ),
    )
    assert (
        _definition_allowed(
            "CREATE VIEW dbo.Public AS SELECT ABS(-1), LOWER('ABC'), LEN('abc') FROM dbo.Users",
            policy,
            "app",
            "dbo",
            "Public",
        )
        is True
    )
