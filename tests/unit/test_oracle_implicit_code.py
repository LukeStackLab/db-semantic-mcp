"""Implicit execution must be rejected before driver parse or execute."""

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from db_semantic_mcp.infrastructure.backends import oracle_catalog as catalog
from db_semantic_mcp.services import query_service
from db_semantic_mcp.services.policy_service import PolicyService
from tests.unit.test_oracle_resolution import setup


@pytest.mark.parametrize(
    "column",
    [
        ("OBJ", "PROBE_OBJ", "ERP", "NO", "NO"),
        ("ID", "NUMBER", None, "YES", "NO"),
        ("SYS_NC0002$", "NUMBER", None, "YES", "YES"),
        ("ID", "UNKNOWN", None, "NO", "NO"),
        ("ID", "NUMBER", None, None, "NO"),
        ("ID",),
    ],
)
@pytest.mark.parametrize("path", ["table", "synonym", "view", "remote"])
def test_unsafe_target_denied_before_driver_work(monkeypatch, column, path):
    fake, cfg, policy, _ = setup(monkeypatch, sources={"sales": {"allowed_schemas": ["ERP"]}})
    target = "ERP.ORDERS"
    key = ("local", "ERP", "ORDERS")
    if path == "synonym":
        fake.synonyms["local", "ERP", "ALIAS"] = ("ERP", "ORDERS", None)
        target = "ERP.ALIAS"
    elif path == "view":
        fake.objects["local", "ERP", "V"] = "VIEW"
        monkeypatch.setattr(catalog, "get_object_definition", lambda *a, **kw: "SELECT ID FROM ERP.ORDERS")
        target = "ERP.V"
    elif path == "remote":
        target += "@SALES"
        key = ("remote", "ERP", "ORDERS")
    fake.columns[key] = [("ID", "NUMBER", None, "NO", "NO"), column]
    connection = MagicMock()

    @contextmanager
    def connect(config):
        yield connection

    monkeypatch.setattr(query_service, "open_connection", connect)
    service = PolicyService(cfg)
    service._policy = policy
    for sql in (f"SELECT * FROM {target}", f"SELECT ID FROM {target} ORDER BY ID", f"SELECT COUNT(*) FROM {target}"):
        result = query_service.QueryService(service, cfg).execute_query(sql, validate_result_schema=True)
        assert result["executed"] is False
        assert result["validation"]["source_resolution"] == "denied"
    # Catalog fake has no driver cursor. Caller work must never reach one.
    connection.cursor.assert_not_called()


@pytest.mark.parametrize(
    ("view", "evidence"),
    [
        ("all_policies", [("VPD",)]),
        ("all_external_tables", [("ORDERS",)]),
        ("all_object_tables", [("ORDERS",)]),
        ("all_indexes", [("DOMAIN",)]),
        ("all_indexes", [("FUNCTION-BASED NORMAL",)]),
    ],
)
def test_implicit_access_paths_denied(monkeypatch, view, evidence):
    fake, cfg, policy, _ = setup(monkeypatch)
    fake.access_paths[(("local", "ERP", "ORDERS"), view)] = evidence
    service = PolicyService(cfg)
    service._policy = policy
    assert not service.validate("SELECT ID FROM ERP.ORDERS", conn=MagicMock())["allowed"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT ID FROM ERP.ORDERS ORDER BY AMOUNT",
        "SELECT * FROM ERP.ORDERS",
        "SELECT SUM(AMOUNT) FROM ERP.ORDERS GROUP BY ID",
        "SELECT o.ID FROM ERP.ORDERS o JOIN ERP.CUSTOMERS c ON o.ID=c.ID",
        "SELECT ID FROM ERP.ORDERS WHERE AMOUNT > 0",
    ],
)
def test_physical_scalar_shapes_remain_supported(monkeypatch, sql):
    fake, cfg, policy, _ = setup(monkeypatch)
    service = PolicyService(cfg)
    service._policy = policy
    assert service.validate(sql, conn=MagicMock())["allowed"]
    assert any("sys.all_tab_cols" in sql for sql, _ in fake.calls)


def test_catalog_failure_cannot_authorize_query(monkeypatch):
    _, cfg, policy, _ = setup(monkeypatch)
    original = catalog._fetch

    def failing(conn, sql, params=None):
        if "all_policies" in sql:
            raise RuntimeError("catalog inaccessible")
        return original(conn, sql, params)

    monkeypatch.setattr(catalog, "_fetch", failing)
    service = PolicyService(cfg)
    service._policy = policy
    assert not service.validate("SELECT ID FROM ERP.ORDERS", conn=MagicMock())["allowed"]


def test_synonym_vpd_policy_denied_even_when_target_is_scalar(monkeypatch):
    fake, cfg, policy, _ = setup(monkeypatch)
    fake.synonyms["local", "ERP", "ALIAS"] = ("ERP", "ORDERS", None)
    fake.access_paths[(("local", "ERP", "ALIAS"), "all_policies")] = [("VPD",)]
    service = PolicyService(cfg)
    service._policy = policy
    assert not service.validate("SELECT ID FROM ERP.ALIAS", conn=MagicMock())["allowed"]


@pytest.mark.parametrize("kind", ["NORMAL", "NORMAL/REV", "BITMAP", "IOT - TOP", "LOB", "CLUSTER"])
def test_builtin_index_storage_remains_supported(monkeypatch, kind):
    fake, cfg, policy, _ = setup(monkeypatch)
    fake.access_paths[(("local", "ERP", "ORDERS"), "all_indexes")] = [(kind,)]
    service = PolicyService(cfg)
    service._policy = policy
    assert service.validate("SELECT ID FROM ERP.ORDERS", conn=MagicMock())["allowed"]


@pytest.mark.parametrize(
    "evidence, allowed", [([("N",)], True), ([("Y",)], False), ([("UNKNOWN",)], False), ([], False)]
)
def test_stored_materialized_view_requires_no_query_computation(monkeypatch, evidence, allowed):
    fake, cfg, policy, _ = setup(monkeypatch)
    fake.objects["local", "ERP", "ORDERS"] = "MATERIALIZED VIEW"
    fake.access_paths[(("local", "ERP", "ORDERS"), "all_mviews")] = evidence
    service = PolicyService(cfg)
    service._policy = policy
    assert service.validate("SELECT ID FROM ERP.ORDERS", conn=MagicMock())["allowed"] is allowed
