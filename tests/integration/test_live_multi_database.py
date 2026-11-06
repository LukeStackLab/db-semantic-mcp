"""Additional explicit opt-in: creates/drops ONLY unique test-owned databases."""

import os
from contextlib import contextmanager
from uuid import uuid4

import pytest

from db_semantic_mcp.infrastructure.connection import open_connection
from db_semantic_mcp.policy.models import PolicyProfile
from db_semantic_mcp.services.policy_service import PolicyService
from db_semantic_mcp.services.query_service import QueryService

pytestmark = pytest.mark.integration


class RecordingConnection:
    """Record user SQL while forwarding every operation to the live session."""

    def __init__(self, connection, statements):
        self.connection, self.statements = connection, statements

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def cursor(self):
        cursor = self.connection.cursor()
        statements = self.statements

        class RecordingCursor:
            def __getattr__(self, name):
                return getattr(cursor, name)

            def execute(self, sql, *args, **kwargs):
                statements.append(sql)
                return cursor.execute(sql, *args, **kwargs)

        return RecordingCursor()


def test_native_cross_database_join_and_hidden_source_denial(disposable_config, monkeypatch):
    if os.getenv("SEMANTIC_MCP_DISPOSABLE_MULTI_DATABASE_TESTS") != "1":
        pytest.skip("Explicit permission to create disposable databases required")
    cfg = disposable_config
    names = [f"DBMCP_MULTI_{uuid4().hex[:10]}_{n}" for n in range(3)]
    created = []
    statements = []

    @contextmanager
    def recorded_connection(config):
        with open_connection(config) as conn:
            yield RecordingConnection(conn, statements)

    monkeypatch.setattr("db_semantic_mcp.services.query_service.open_connection", recorded_connection)
    try:
        with open_connection(cfg) as admin:
            admin.autocommit(True)
            cursor = admin.cursor()
            for name in names:
                cursor.execute(f"CREATE DATABASE [{name}]")
                created.append(name)
            cursor.close()
        for index, name in enumerate(names):
            with open_connection(cfg.model_copy(update={"database": name})) as conn:
                conn.autocommit(True)
                cursor = conn.cursor()
                cursor.execute("CREATE TABLE dbo.Shared (Id INT NOT NULL PRIMARY KEY, Value INT NOT NULL)")
                cursor.execute("INSERT dbo.Shared VALUES (1, %s)", (index + 10,))
                cursor.close()
        multi = cfg.model_copy(deep=True)
        multi.database, multi.databases, multi.profile_version = names[0], names[:2], 3
        multi._policy_snapshot = PolicyProfile(
            profile_name="readonly",
            scope={"allowed_databases": names[:2], "database_scopes": {name: {} for name in names[:2]}},
        )
        query = QueryService(PolicyService(multi), multi)
        sql = f"SELECT a.Value, b.Value FROM [{names[0]}].dbo.Shared a JOIN [{names[1]}].dbo.Shared b ON a.Id=b.Id"
        result = query.execute_query(sql)
        assert result["executed"] is True, result
        assert result["rows"] == [[10, 11]]
        assert result["referenced_databases"] == sorted(names[:2])
        assert statements.count(sql) == 1
        denied = query.execute_query(f"SELECT * FROM [{names[2]}].dbo.Shared")
        assert denied["executed"] is False
        assert "referenced_databases" not in denied
        with open_connection(multi) as conn:
            conn.autocommit(True)
            cursor = conn.cursor()
            cursor.execute(f"CREATE SYNONYM dbo.RemoteShared FOR [{names[1]}].dbo.Shared")
            cursor.execute(f"CREATE VIEW dbo.RemoteView AS SELECT Id, Value FROM [{names[1]}].dbo.Shared")
            cursor.execute("CREATE TABLE dbo.Triggered (Id INT NOT NULL PRIMARY KEY, Value INT NOT NULL)")
            cursor.execute("INSERT dbo.Triggered VALUES (1, 10)")
            cursor.execute(
                f"CREATE TRIGGER dbo.HiddenWrite ON dbo.Triggered AFTER UPDATE AS "
                f"UPDATE [{names[2]}].dbo.Shared SET Value=99 WHERE Id=1"
            )
            cursor.close()
        for source in ("RemoteShared", "RemoteView"):
            read = query.execute_query(f"SELECT Value FROM dbo.{source}")
            assert read["executed"] is True, read
            assert read["rows"] == [[11]]
        writable = multi.model_copy(deep=True)
        writable.policy_profile = "read_write_safe"
        writable._policy_snapshot = multi._policy_snapshot.model_copy(deep=True)
        writable._policy_snapshot.profile_name = "read_write_safe"
        writable._policy_snapshot.operations.update = True
        writer = QueryService(PolicyService(writable), writable)
        for target in ("RemoteShared", "RemoteView", "Triggered"):
            write_sql = f"UPDATE dbo.{target} SET Value=99 WHERE Id=1"
            rejected = writer.execute_query(write_sql)
            assert rejected["executed"] is False, rejected
            assert rejected["denial"]["rule"] == "database_source_scope"
            assert write_sql not in statements
        for index, name in enumerate(names):
            with open_connection(cfg.model_copy(update={"database": name})) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT Value FROM dbo.Shared WHERE Id=1")
                assert cursor.fetchone()[0] == index + 10
                cursor.close()
    finally:
        with open_connection(cfg) as admin:
            admin.autocommit(True)
            cursor = admin.cursor()
            for name in reversed(created):
                cursor.execute(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
                cursor.execute(f"DROP DATABASE [{name}]")
            cursor.close()


def test_object_only_metadata_permission_cannot_hide_rls(disposable_config, monkeypatch):
    if os.getenv("SEMANTIC_MCP_DISPOSABLE_MULTI_DATABASE_TESTS") != "1":
        pytest.skip("Explicit permission to create disposable databases required")
    if os.getenv("SEMANTIC_MCP_DISPOSABLE_LOWPRIV_TESTS") != "1":
        pytest.skip("Explicit permission to create and impersonate a disposable database user required")
    cfg = disposable_config
    name = f"DBMCP_LOWPRIV_{uuid4().hex[:10]}"
    username = f"reader_{uuid4().hex[:10]}"
    created = False
    statements = []
    try:
        with open_connection(cfg) as admin:
            admin.autocommit(True)
            cursor = admin.cursor()
            cursor.execute(f"CREATE DATABASE [{name}]")
            created = True
            cursor.close()
        target = cfg.model_copy(deep=True)
        target.database, target.databases, target.profile_version = name, [name], 3
        target._policy_snapshot = PolicyProfile(
            profile_name="readonly", scope={"allowed_databases": [name], "database_scopes": {name: {}}}
        )
        with open_connection(target) as conn:
            conn.autocommit(True)
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE dbo.Shared (Id INT NOT NULL PRIMARY KEY)")
            cursor.execute("INSERT dbo.Shared VALUES (1)")
            cursor.execute(
                "CREATE FUNCTION dbo.RowFilter(@Id INT) RETURNS TABLE WITH SCHEMABINDING "
                "AS RETURN SELECT 1 AS permitted WHERE @Id > 0"
            )
            cursor.execute(
                "CREATE SECURITY POLICY dbo.HiddenPolicy ADD FILTER PREDICATE dbo.RowFilter(Id) ON dbo.Shared"
            )
            cursor.execute(f"CREATE USER [{username}] WITHOUT LOGIN")
            cursor.execute(f"GRANT SELECT, VIEW DEFINITION ON OBJECT::dbo.Shared TO [{username}]")
            for phase in ("object_only", "denied_policy", "visible_policy"):
                if phase == "denied_policy":
                    cursor.execute(f"GRANT VIEW DEFINITION TO [{username}]")
                    cursor.execute(f"DENY VIEW DEFINITION ON OBJECT::dbo.HiddenPolicy TO [{username}]")
                elif phase == "visible_policy":
                    cursor.execute(f"GRANT ALTER ANY SECURITY POLICY TO [{username}]")
                cursor.execute(f"EXECUTE AS USER = '{username}'")
                try:
                    cursor.execute("SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DEFINITION')")
                    assert cursor.fetchone()[0] == int(phase != "object_only")
                    cursor.execute("SELECT HAS_PERMS_BY_NAME('dbo.Shared', 'OBJECT', 'VIEW DEFINITION')")
                    assert cursor.fetchone()[0] == 1
                    cursor.execute("SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'ALTER ANY SECURITY POLICY')")
                    assert cursor.fetchone()[0] == int(phase == "visible_policy")
                    cursor.execute("SELECT COUNT(*) FROM sys.security_predicates")
                    assert cursor.fetchone()[0] == int(phase == "visible_policy")

                    @contextmanager
                    def impersonated_connection(config):
                        yield RecordingConnection(conn, statements)

                    monkeypatch.setattr(
                        "db_semantic_mcp.services.query_service.open_connection", impersonated_connection
                    )
                    statements.clear()
                    sql = "SELECT Id FROM dbo.Shared"
                    result = QueryService(PolicyService(target), target).execute_query(sql)
                    assert result["executed"] is False, result
                    assert result["denial"]["rule"] == "database_source_scope"
                    assert sql not in statements
                    assert any(".sys.objects" in statement for statement in statements) == (phase == "visible_policy")
                finally:
                    cursor.execute("REVERT")
            cursor.close()
    finally:
        if created:
            with open_connection(cfg) as admin:
                admin.autocommit(True)
                cursor = admin.cursor()
                cursor.execute(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
                cursor.execute(f"DROP DATABASE [{name}]")
                cursor.close()
