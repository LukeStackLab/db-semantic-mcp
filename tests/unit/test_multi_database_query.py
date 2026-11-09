"""Cross-database proof must use the executing session and every source scope."""

from types import SimpleNamespace

import pytest

from db_semantic_mcp.policy.models import PolicyProfile, PolicyScope
from db_semantic_mcp.services.sqlserver_database_proof import authorize_cross_database_query


def policy():
    return PolicyProfile(profile_name="readonly", scope=PolicyScope(allowed_databases=["ERP", "CRM"]))


def config():
    return SimpleNamespace(database="ERP", databases=["ERP", "CRM"], profile_version=3)


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def execute(self, sql, params=()):
        self.conn.queries.append((sql, params))
        if sql == "SELECT DB_NAME(), SCHEMA_NAME()":
            self.rows = [(self.conn.database, "dbo")]
        elif "FROM sys.databases" in sql:
            self.rows = [(params[0],)] if params[0] in {"ERP", "CRM"} else []
            if "HAS_PERMS_BY_NAME(name, 'DATABASE', 'VIEW DEFINITION')" in sql and self.rows:
                self.rows = [(params[0], self.conn.database_visibility.get(params[0]))]
                if "HAS_PERMS_BY_NAME(name, 'DATABASE', 'ALTER ANY SECURITY POLICY')" in sql:
                    self.rows = [(*self.rows[0], self.conn.policy_visibility.get(params[0]))]
        else:
            db = "CRM" if "[CRM]" in sql else "ERP"
            key = (db, *params[1:])
            obj = self.conn.objects.get(key)
            if obj and "RTRIM(o.type)" not in sql:
                # sys.objects.type is CHAR(2): SQL Server pads one-letter codes ("U ", "V ").
                obj = (obj[0], obj[1].ljust(2), *obj[2:])
            if obj and "AS unsafe_write" in sql:
                flags = self.conn.write_dependencies.get(key, {})
                checks = {
                    "trigger": "sys.triggers",
                    "default": "default_object_id",
                    "check": "sys.check_constraints",
                    "foreign_key_parent": "f.parent_object_id=o.object_id",
                    "foreign_key_referenced": "f.referenced_object_id=o.object_id",
                    "bound_rule": "rule_object_id",
                    "temporal": "temporal_type",
                }
                unsafe_write = any(value and checks.get(kind, kind) in sql for kind, value in flags.items())
                obj = (*obj, int(unsafe_write))
            self.rows = [obj] if obj else []
        return self

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class Connection:
    def __init__(self):
        self.database = "ERP"
        self.database_visibility = {"ERP": 1, "CRM": 1}
        self.policy_visibility = {"ERP": 1, "CRM": 1}
        self.write_dependencies = {}
        self.queries = []
        self.objects = {
            ("ERP", "dbo", "Orders"): (1, "U", "dbo", "Orders", None, None, 0),
            ("CRM", "dbo", "Customers"): (2, "U", "dbo", "Customers", None, None, 0),
        }

    def cursor(self):
        return Cursor(self)


def test_authorized_join_verifies_both_database_catalogs():
    conn = Connection()
    assert authorize_cross_database_query(
        config(), policy(), conn, "SELECT o.Id FROM ERP.dbo.Orders o JOIN CRM.dbo.Customers c ON o.Id=c.Id"
    ) == ["CRM", "ERP"]
    assert any("[ERP].sys.objects" in sql for sql, _ in conn.queries)
    assert any("[CRM].sys.objects" in sql for sql, _ in conn.queries)


def test_unconfigured_database_rejected_before_catalog_access():
    conn = Connection()
    with pytest.raises(PermissionError):
        authorize_cross_database_query(config(), policy(), conn, "SELECT * FROM Forbidden.dbo.Secrets")
    assert conn.queries == []


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * INTO CRM.dbo.New FROM ERP.dbo.Orders",
        "SELECT * FROM remote.ERP.dbo.Orders",
        "USE CRM; SELECT * FROM dbo.Customers",
        "EXEC('SELECT * FROM CRM.dbo.Customers')",
        "UPDATE CRM.dbo.Customers SET Id=1 WHERE Id=2",
    ],
)
def test_unsupported_or_cross_database_write_is_rejected(query):
    with pytest.raises(PermissionError):
        authorize_cross_database_query(config(), policy(), Connection(), query)


def test_synonym_cannot_hide_forbidden_database():
    conn = Connection()
    conn.objects[("ERP", "dbo", "Orders")] = (3, "SN", "dbo", "Orders", "Forbidden.dbo.Secrets", None, 0)
    with pytest.raises(PermissionError):
        authorize_cross_database_query(config(), policy(), conn, "SELECT * FROM dbo.Orders")


def test_view_checks_its_dependency_database_and_same_named_table():
    conn = Connection()
    conn.objects[("ERP", "dbo", "Orders")] = (
        3,
        "V",
        "dbo",
        "Orders",
        None,
        "CREATE VIEW dbo.Orders AS SELECT * FROM CRM.dbo.Customers",
        0,
    )
    assert authorize_cross_database_query(config(), policy(), conn, "SELECT * FROM dbo.Orders") == ["CRM", "ERP"]
    conn.objects[("CRM", "dbo", "Customers")] = None
    with pytest.raises(PermissionError):
        authorize_cross_database_query(config(), policy(), conn, "SELECT * FROM dbo.Orders")


def test_unproven_view_and_cycles_fail_closed():
    conn = Connection()
    for definition in (None, "CREATE VIEW dbo.Orders AS SELECT * FROM dbo.Orders"):
        conn.objects[("ERP", "dbo", "Orders")] = (3, "V", "dbo", "Orders", None, definition, 0)
        with pytest.raises(PermissionError):
            authorize_cross_database_query(config(), policy(), conn, "SELECT * FROM dbo.Orders")


def test_session_database_and_unsafe_table_evidence():
    conn = Connection()
    conn.database = "CRM"
    with pytest.raises(PermissionError):
        authorize_cross_database_query(config(), policy(), conn, "SELECT * FROM dbo.Orders")
    conn.database = "ERP"
    conn.objects[("ERP", "dbo", "Orders")] = (1, "U", "dbo", "Orders", None, None, 1)
    with pytest.raises(PermissionError):
        authorize_cross_database_query(config(), policy(), conn, "SELECT * FROM dbo.Orders")


def test_identifier_values_are_parameters_and_brackets_are_escaped():
    conn = Connection()
    conn.objects[("ERP", "sales.x", "Order]s")] = (4, "U", "sales.x", "Order]s", None, None, 0)
    assert authorize_cross_database_query(config(), policy(), conn, "SELECT * FROM ERP.[sales.x].[Order]]s]") == ["ERP"]
    assert any(params == ("ERP", "sales.x", "Order]s") for _, params in conn.queries)


@pytest.mark.parametrize("mode", ["validate", "preview", "execute"])
@pytest.mark.parametrize("missing_evidence", ["object", "database_visibility", "policy_visibility"])
def test_query_service_modes_use_live_proof_and_do_not_execute_denied_sql(monkeypatch, mode, missing_evidence):
    from contextlib import contextmanager

    from db_semantic_mcp.config import Config
    from db_semantic_mcp.services.policy_service import PolicyService
    from db_semantic_mcp.services.query_service import QueryService

    cfg = Config.from_fixed(
        server="host", database="ERP", user="u", profile_version=3, databases=["ERP", "CRM"], cache_enabled=False
    )
    cfg._policy_snapshot = PolicyProfile(
        profile_name="readonly", scope={"allowed_databases": ["ERP", "CRM"], "database_scopes": {"ERP": {}, "CRM": {}}}
    )
    conn = Connection()
    if missing_evidence == "object":
        conn.objects[("CRM", "dbo", "Customers")] = None
    elif missing_evidence == "database_visibility":
        conn.database_visibility["CRM"] = 0
    else:
        conn.policy_visibility["CRM"] = 0

    @contextmanager
    def opened(cfg):
        yield conn

    monkeypatch.setattr("db_semantic_mcp.services.query_service.open_connection", opened)
    query = "SELECT * FROM ERP.dbo.Orders JOIN CRM.dbo.Customers ON 1=1"
    service = QueryService(PolicyService(cfg), cfg)
    method = {"validate": service.validate_query, "preview": service.preview_query, "execute": service.execute_query}[
        mode
    ]
    result = method(query)
    assert result.get("allowed") is False or result.get("executed") is False
    assert "referenced_databases" not in result
    assert result["denial"]["rule"] == "database_source_scope"
    assert not any(sql == query for sql, _ in conn.queries)


def test_query_service_executes_original_sql_on_the_proven_connection(monkeypatch):
    from contextlib import contextmanager

    from db_semantic_mcp.config import Config
    from db_semantic_mcp.services.policy_service import PolicyService
    from db_semantic_mcp.services.query_service import QueryService

    cfg = Config.from_fixed(
        server="host", database="ERP", user="u", profile_version=3, databases=["ERP", "CRM"], cache_enabled=False
    )
    cfg._policy_snapshot = PolicyProfile(
        profile_name="readonly", scope={"allowed_databases": ["ERP", "CRM"], "database_scopes": {"ERP": {}, "CRM": {}}}
    )
    query = "SELECT o.Id FROM ERP.dbo.Orders o JOIN CRM.dbo.Customers c ON o.Id=c.Id"
    conn = Connection()

    class ResultCursor(Cursor):
        description = [("Id",)]

        def execute(self, sql, params=()):
            if sql == query:
                self.conn.queries.append((sql, params))
                self.rows = [(42,)]
                return self
            return super().execute(sql, params)

        def fetchmany(self, size):
            rows, self.rows = self.rows[:size], self.rows[size:]
            return rows

    conn.cursor = lambda: ResultCursor(conn)
    opened_count = []

    @contextmanager
    def opened(cfg):
        opened_count.append(cfg.database)
        yield conn

    monkeypatch.setattr("db_semantic_mcp.services.query_service.open_connection", opened)
    result = QueryService(PolicyService(cfg), cfg).execute_query(query)
    assert result["executed"] is True
    assert result["rows"] == [[42]]
    assert result["referenced_databases"] == ["CRM", "ERP"]
    assert opened_count == ["ERP"]
    assert conn.queries[-1] == (query, ())
    assert sum(sql == query for sql, _ in conn.queries) == 1


def write_policy():
    return PolicyProfile(
        profile_name="readwrite",
        operations={"select": True, "insert": True, "update": True},
        scope=PolicyScope(allowed_databases=["ERP", "CRM"]),
    )


@pytest.mark.parametrize("kind", ["SN", "V"])
@pytest.mark.parametrize("target", ["ERP.dbo.Physical", "CRM.dbo.Customers"])
@pytest.mark.parametrize("sql", ["UPDATE dbo.Orders SET Id=1 WHERE Id=2", "INSERT dbo.Orders (Id) VALUES (1)"])
def test_write_through_synonym_or_view_is_rejected(kind, target, sql):
    conn = Connection()
    conn.objects[("ERP", "dbo", "Physical")] = (4, "U", "dbo", "Physical", None, None, 0)
    conn.objects[("ERP", "dbo", "Orders")] = (
        3,
        kind,
        "dbo",
        "Orders",
        target if kind == "SN" else None,
        f"CREATE VIEW dbo.Orders AS SELECT * FROM {target}" if kind == "V" else None,
        0,
    )
    with pytest.raises(PermissionError):
        authorize_cross_database_query(config(), write_policy(), conn, sql)
    assert all(statement != sql for statement, _ in conn.queries)


@pytest.mark.parametrize(
    "dependency",
    ["trigger", "default", "check", "foreign_key_parent", "foreign_key_referenced", "bound_rule", "temporal"],
)
@pytest.mark.parametrize("sql", ["UPDATE dbo.Orders SET Id=1 WHERE Id=2", "INSERT dbo.Orders (Id) VALUES (1)"])
def test_write_with_implicit_dependencies_fails_closed(dependency, sql):
    conn = Connection()
    conn.write_dependencies[("ERP", "dbo", "Orders")] = {dependency: True}
    with pytest.raises(PermissionError):
        authorize_cross_database_query(config(), write_policy(), conn, sql)
    assert all(statement != sql for statement, _ in conn.queries)
    # Write-only dependencies do not remove read access to an ordinary table.
    assert authorize_cross_database_query(config(), policy(), conn, "SELECT * FROM dbo.Orders") == ["ERP"]


def test_plain_physical_table_write_is_still_proven():
    assert authorize_cross_database_query(
        config(), write_policy(), Connection(), "UPDATE dbo.Orders SET Id=1 WHERE Id=2"
    ) == ["ERP"]


def test_fixed_write_profile_reaches_live_source_proof(monkeypatch):
    from contextlib import contextmanager

    from db_semantic_mcp.config import Config
    from db_semantic_mcp.services.policy_service import PolicyService
    from db_semantic_mcp.services.query_service import QueryService

    cfg = Config.from_fixed(
        server="host", database="ERP", user="u", profile_version=3, databases=["ERP", "CRM"], cache_enabled=False
    )
    cfg.connection_name = "disposable-test"
    cfg.policy_profile = "read_write_safe"
    cfg._policy_snapshot = PolicyProfile(
        profile_name="read_write_safe",
        operations={"select": True, "update": True},
        scope={"allowed_databases": ["ERP", "CRM"], "database_scopes": {"ERP": {}, "CRM": {}}},
    )
    conn = Connection()
    conn.write_dependencies[("ERP", "dbo", "Orders")] = {"trigger": True}

    @contextmanager
    def opened(config):
        yield conn

    monkeypatch.setattr("db_semantic_mcp.services.query_service.open_connection", opened)
    sql = "UPDATE dbo.Orders SET Id=1 WHERE Id=2"
    policy_service = PolicyService(cfg)
    assert policy_service.validate(sql)["allowed"] is True
    result = QueryService(policy_service, cfg).execute_query(sql)
    assert result["executed"] is False
    assert result["denial"]["rule"] == "database_source_scope"
    assert any("AS unsafe_write" in statement for statement, _ in conn.queries)
    assert not any(statement == sql for statement, _ in conn.queries)


@pytest.mark.parametrize("flag_index", [6, 7])
@pytest.mark.parametrize("unknown", [None, 2, "0"])
def test_unknown_metadata_flags_fail_closed(flag_index, unknown):
    conn = Connection()

    class UnknownCursor(Cursor):
        def execute(self, sql, params=()):
            super().execute(sql, params)
            if "AS unsafe_write" in sql and self.rows:
                row = list(self.rows[0])
                row[flag_index] = unknown
                self.rows = [tuple(row)]
            return self

    conn.cursor = lambda: UnknownCursor(conn)
    with pytest.raises(PermissionError):
        authorize_cross_database_query(config(), write_policy(), conn, "UPDATE dbo.Orders SET Id=1 WHERE Id=2")


@pytest.mark.parametrize("database", ["ERP", "CRM"])
@pytest.mark.parametrize("visibility", [0, None])
def test_object_visibility_cannot_substitute_for_database_metadata_permission(database, visibility):
    conn = Connection()
    conn.database_visibility[database] = visibility
    with pytest.raises(PermissionError):
        authorize_cross_database_query(
            config(), policy(), conn, "SELECT * FROM ERP.dbo.Orders JOIN CRM.dbo.Customers ON 1=1"
        )
    assert not any(f"[{database}].sys.objects" in sql for sql, _ in conn.queries)


@pytest.mark.parametrize("database", ["ERP", "CRM"])
@pytest.mark.parametrize("visibility", [0, None, 2, "1"])
def test_database_view_definition_cannot_prove_security_policy_visibility(database, visibility):
    conn = Connection()
    conn.policy_visibility[database] = visibility
    assert conn.database_visibility[database] == 1
    with pytest.raises(PermissionError):
        authorize_cross_database_query(
            config(), policy(), conn, "SELECT * FROM ERP.dbo.Orders JOIN CRM.dbo.Customers ON 1=1"
        )
    assert not any(f"[{database}].sys.objects" in sql for sql, _ in conn.queries)
