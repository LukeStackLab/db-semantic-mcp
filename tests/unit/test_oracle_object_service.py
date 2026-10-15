from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from db_semantic_mcp.infrastructure.backends import oracle_catalog as catalog
from db_semantic_mcp.policy.models import PolicyProfile
from db_semantic_mcp.services.object_service import _describe_oracle
from db_semantic_mcp.services.policy_service import PolicyService
from tests.unit.test_oracle_policy import config
from tests.unit.test_oracle_resolution import Catalog


def setup(monkeypatch, *, scope=None):
    fake = Catalog(monkeypatch)
    cfg = config(sources={"sales": {"db_link": "SALES"}})
    policy = PolicyService(cfg)
    policy._policy = PolicyProfile(profile_name="readonly", scope=scope or {})

    @contextmanager
    def connect(*args):
        yield MagicMock()

    monkeypatch.setattr("db_semantic_mcp.infrastructure.connection.open_connection", connect)
    return fake, cfg, policy


@pytest.mark.parametrize("kind", ["PACKAGE", "PACKAGE BODY", "PROCEDURE", "FUNCTION"])
def test_restricted_plsql_is_withheld_and_never_claims_complete_io(monkeypatch, kind):
    fake, cfg, policy = setup(monkeypatch, scope={"allowed_schemas": ["ERP"]})
    fake.objects["local", "ERP", "MODULE"] = kind
    seen_types = []

    def definition(cfg, owner, name, object_type, **kw):
        seen_types.append(object_type)
        return "BEGIN EXECUTE IMMEDIATE secret_sql; END;"

    monkeypatch.setattr(catalog, "get_object_definition", definition)
    monkeypatch.setattr(catalog, "get_object_dependencies", lambda *a, **kw: [])
    r = _describe_oracle(cfg, "ERP", "MODULE", kind, policy)
    assert r["definition"] is None
    assert r["status"] == "restricted"
    assert r["io_completeness"] == "unknown"
    assert r["dependency_completeness"] == "static_catalog_only"
    assert seen_types == []


def test_unrestricted_local_scope_still_withholds_opaque_definition_and_keeps_static_metadata(monkeypatch):
    fake, cfg, policy = setup(monkeypatch)
    fake.objects["local", "ERP", "MODULE"] = "PACKAGE BODY"
    monkeypatch.setattr(catalog, "get_object_definition", lambda *a, **kw: "PACKAGE BODY MODULE AS END;")
    seen = []

    def dependencies(*a, **kw):
        seen.append(kw["object_type"])
        return [("ERP", "ORDERS", "TABLE", None)]

    monkeypatch.setattr(catalog, "get_object_dependencies", dependencies)
    r = _describe_oracle(cfg, "ERP", "MODULE", "PACKAGE BODY", policy)
    assert r["definition"] is None
    assert r["definition_withheld"]
    assert r["dependencies"] == [{"source": "local", "schema": "ERP", "name": "ORDERS", "object_type": "TABLE"}]
    assert r["read_tables"] == r["write_tables"] == []
    assert r["io_completeness"] == "unknown"
    assert seen == ["PACKAGE BODY"]


def test_view_definition_hidden_when_physical_dependency_denied(monkeypatch):
    fake, cfg, policy = setup(monkeypatch, scope={"allowed_tables": ["ERP.V"]})
    fake.objects["local", "ERP", "V"] = "VIEW"
    monkeypatch.setattr(catalog, "get_object_definition", lambda *a, **kw: "SELECT * FROM ERP.ORDERS")
    monkeypatch.setattr(catalog, "get_object_dependencies", lambda *a, **kw: [("ERP", "ORDERS", "TABLE", None)])
    r = _describe_oracle(cfg, "ERP", "V", "VIEW", policy)
    assert r["definition_withheld"]
    assert not r["dependencies"]
    assert r["dependencies_omitted"]


def test_oracle_synonym_describe_exposes_authorized_chain(monkeypatch):
    fake, cfg, policy = setup(monkeypatch, scope={"sources": {"sales": {"allowed_schemas": ["ERP"]}}})
    fake.objects["local", "ERP", "A"] = "SYNONYM"
    fake.synonyms["local", "ERP", "A"] = ("ERP", "ORDERS", "SALES.EXAMPLE")
    r = _describe_oracle(cfg, "ERP", "A", "SYNONYM", policy)
    assert r["status"] == "ready"
    assert r["resolved_target"]["source"] == "sales"
    assert r["definition"] is None
    assert r["io_completeness"] == "unknown"


def test_remote_object_cannot_inherit_local_scope_from_passed_policy(monkeypatch):
    fake, cfg, policy = setup(
        monkeypatch, scope={"allowed_schemas": ["ERP"], "sources": {"sales": {"allowed_schemas": ["OTHER"]}}}
    )
    fake.objects["remote", "ERP", "MODULE"] = "PACKAGE"
    r = _describe_oracle(cfg.for_source("sales"), "ERP", "MODULE", "PACKAGE", policy)
    assert r["status"] == "error"


@pytest.mark.parametrize("kind", ["PACKAGE", "PACKAGE BODY", "PROCEDURE", "FUNCTION", "MATERIALIZED VIEW"])
def test_remote_only_restriction_withholds_unknown_dynamic_definition(monkeypatch, kind):
    fake, cfg, policy = setup(monkeypatch, scope={"sources": {"sales": {"allowed_tables": ["ERP.ORDERS"]}}})
    fake.objects["local", "ERP", "MODULE"] = kind
    definition = "BEGIN EXECUTE IMMEDIATE 'SELECT SECRET FROM FORBIDDEN.OTHER@SALES'; END;"
    get_definition = MagicMock(return_value=definition)
    monkeypatch.setattr(catalog, "get_object_definition", get_definition)
    monkeypatch.setattr(catalog, "get_object_dependencies", lambda *a, **kw: [])
    result = _describe_oracle(cfg, "ERP", "MODULE", kind, policy)
    get_definition.assert_not_called()
    assert result["status"] == "restricted"
    assert result["definition"] is None
    assert result["definition_withheld"]
    assert "closed-world source authorization" in result["definition_omission_reason"]
    assert result["io_completeness"] == "unknown"
    assert result["dependency_completeness"] == "static_catalog_only"
    assert result["read_tables"] == result["write_tables"] == []
    assert "SECRET" not in str(result)


@pytest.mark.parametrize(
    "remote_scope",
    [
        {"allowed_schemas": ["ERP"]},
        {"denied_tables": ["FORBIDDEN.OTHER"]},
        {"allowed_databases": ["db"]},
        {"operations": {"select": False}},
        {},
        None,
    ],
)
def test_opaque_definition_never_assumes_unknown_remote_is_authorized(monkeypatch, remote_scope):
    scope = {"sources": {"sales": remote_scope}} if remote_scope is not None else {}
    fake, cfg, policy = setup(monkeypatch, scope=scope)
    if remote_scope is None:
        cfg.sources = {}  # No configured routes still does not authorize unknown links.
    fake.objects["local", "ERP", "MODULE"] = "PACKAGE BODY"
    monkeypatch.setattr(catalog, "get_object_definition", lambda *a, **kw: "BEGIN EXECUTE IMMEDIATE sql_text; END;")
    monkeypatch.setattr(catalog, "get_object_dependencies", lambda *a, **kw: [])
    result = _describe_oracle(cfg, "ERP", "MODULE", "PACKAGE BODY", policy)
    assert result["definition"] is None
    assert result["definition_withheld"]
    assert result["io_completeness"] == "unknown"


def test_withheld_module_preserves_only_authorized_static_remote_dependencies(monkeypatch):
    fake, cfg, policy = setup(monkeypatch, scope={"sources": {"sales": {"allowed_tables": ["ERP.ORDERS"]}}})
    fake.objects["local", "ERP", "MODULE"] = "PACKAGE BODY"
    monkeypatch.setattr(catalog, "get_object_definition", lambda *a, **kw: "PACKAGE BODY MODULE AS END;")
    monkeypatch.setattr(
        catalog,
        "get_object_dependencies",
        lambda *a, **kw: [
            ("ERP", "ORDERS", "TABLE", "SALES.EXAMPLE"),
            ("FORBIDDEN", "OTHER", "TABLE", "SALES.EXAMPLE"),
        ],
    )
    result = _describe_oracle(cfg, "ERP", "MODULE", "PACKAGE BODY", policy)
    assert result["definition"] is None
    assert result["dependencies"] == [{"source": "sales", "schema": "ERP", "name": "ORDERS", "object_type": "TABLE"}]
    assert result["dependencies_omitted"]
    assert result["io_completeness"] == "unknown"


def test_remote_only_limits_do_not_withhold_a_proven_local_view(monkeypatch):
    fake, cfg, policy = setup(monkeypatch, scope={"sources": {"sales": {"allowed_tables": ["ERP.ORDERS"]}}})
    fake.objects["local", "ERP", "V"] = "VIEW"
    definition = "SELECT ID FROM ERP.ORDERS"
    monkeypatch.setattr(catalog, "get_object_definition", lambda *a, **kw: definition)
    monkeypatch.setattr(catalog, "get_object_dependencies", lambda *a, **kw: [("ERP", "ORDERS", "TABLE", None)])
    result = _describe_oracle(cfg, "ERP", "V", "VIEW", policy)
    assert result["status"] == "ready"
    assert result["definition"] == definition
    assert not result["definition_withheld"]
    assert result["io_completeness"] == "complete_select_sources"
