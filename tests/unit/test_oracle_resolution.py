from unittest.mock import MagicMock

import pytest

from db_semantic_mcp.infrastructure.backends import oracle_catalog as catalog
from db_semantic_mcp.infrastructure.backends.oracle_resolution import OracleResolver, resolve_object
from db_semantic_mcp.policy.models import PolicyProfile
from db_semantic_mcp.policy.scope import ObjectReference
from tests.unit.test_oracle_policy import config


def ref(name, schema="", link="", source="local"):
    return ObjectReference("", schema, name, source=source, db_link=link, dialect="oracle")


class Catalog:
    def __init__(self, monkeypatch):
        self.objects = {
            ("local", "ERP", "ORDERS"): "TABLE",
            ("local", "ERP", "CUSTOMERS"): "TABLE",
            ("local", "SYS", "DUAL"): "TABLE",
            ("remote", "ERP", "ORDERS"): "TABLE",
        }
        self.synonyms = {("local", "PUBLIC", "DUAL"): ("SYS", "DUAL", None)}
        self.calls = []
        self.route = {
            "owner": "ERP",
            "db_link": "SALES.EXAMPLE",
            "username": "ERP",
            "host": "remote",
            "remote_user": "ERP",
        }
        monkeypatch.setattr(catalog, "_fetch", self.fetch)
        monkeypatch.setattr(catalog, "current_schema", lambda cfg, conn: "ERP")
        monkeypatch.setattr(catalog, "resolve_db_link", lambda cfg, conn: dict(self.route))

    def fetch(self, conn, sql, params=None):
        self.calls.append((sql, params))
        params = params or {}
        source = "remote" if "@SALES.EXAMPLE" in sql else "local"
        key = source, params.get("owner", "PUBLIC"), params.get("name")
        if "all_tab_columns" in sql:
            return [("ID",), ("AMOUNT",), ("DUMMY",)]
        if "all_objects" in sql:
            kind = self.objects.get(key)
            return [(kind,)] if kind else []
        if "all_synonyms" in sql:
            return [self.synonyms[key]] if key in self.synonyms else []
        raise AssertionError(sql)


def setup(monkeypatch, **scope):
    fake = Catalog(monkeypatch)
    cfg = config(sources={"sales": {"db_link": "SALES", "readonly": False}})
    policy = PolicyProfile(profile_name="rw", operations={"select": True, "insert": True, "update": True}, scope=scope)
    return fake, cfg, policy, OracleResolver(cfg, MagicMock(), policy)


def test_private_precedence_public_chain_and_quoted_name(monkeypatch):
    fake, _, _, resolver = setup(monkeypatch)
    fake.synonyms.update(
        {
            ("local", "PUBLIC", "SHADOW"): ("ERP", "ORDERS", None),
            ("local", "ERP", "SHADOW"): ("ERP", "CUSTOMERS", None),
            ("local", "ERP", "CHAIN"): ("ERP", "SHADOW", None),
        }
    )
    assert resolver.resolve(ref("CHAIN")).name == "CUSTOMERS"
    assert [r.name for r in resolver.result.references] == ["CHAIN", "SHADOW", "CUSTOMERS"]
    del fake.synonyms[("local", "ERP", "SHADOW")]
    assert resolver.resolve(ref("SHADOW")).name == "ORDERS"
    fake.objects["local", "ERP", "Odd.Table"] = "TABLE"
    assert resolver.resolve(ref("Odd.Table", "ERP")).name == "Odd.Table"


@pytest.mark.parametrize("denied", ["ERP.CHAIN", "ERP.ORDERS"])
def test_alias_and_target_denials(monkeypatch, denied):
    fake, _, _, resolver = setup(monkeypatch, denied_tables=[denied])
    fake.synonyms["local", "ERP", "CHAIN"] = ("ERP", "ORDERS", None)
    with pytest.raises(PermissionError):
        resolver.resolve(ref("CHAIN"))


def test_cycle_missing_target_and_retarget_checked_live(monkeypatch):
    fake, _, _, resolver = setup(monkeypatch, allowed_tables=["ERP.A", "ERP.B", "ERP.ORDERS"])
    fake.synonyms["local", "ERP", "A"] = ("ERP", "B", None)
    fake.synonyms["local", "ERP", "B"] = ("ERP", "A", None)
    with pytest.raises(PermissionError, match="cycle"):
        resolver.resolve(ref("A"))
    fake.synonyms["local", "ERP", "A"] = ("ERP", "MISSING", None)
    with pytest.raises(PermissionError):
        resolver.resolve(ref("A"))
    fake.synonyms["local", "ERP", "A"] = ("ERP", "ORDERS", None)
    assert resolver.resolve(ref("A")).name == "ORDERS"
    fake.synonyms["local", "ERP", "A"] = ("ERP", "CUSTOMERS", None)
    with pytest.raises(PermissionError):
        resolver.resolve(ref("A"))


def test_remote_needs_policy_and_each_source_operation(monkeypatch):
    _, _, _, resolver = setup(monkeypatch)
    with pytest.raises(PermissionError):
        resolver.resolve(ref("ORDERS", "ERP", "SALES.EXAMPLE"))
    fake, cfg, policy, resolver = setup(monkeypatch, sources={"sales": {"allowed_schemas": ["ERP"]}})
    assert resolver.resolve(ref("ORDERS", "ERP", "SALES.EXAMPLE")).source == "sales"
    assert resolver.result.source_configs["sales"].cache_path != cfg.for_source("sales").cache_path
    with pytest.raises(PermissionError):
        resolver.resolve(ref("ORDERS", "ERP", "SALES"), "UPDATE")
    policy.scope.sources["sales"].operations.update = True
    cfg.sources["sales"] = cfg.sources["sales"].model_copy(update={"readonly": True})
    # An existing proof retains its policy; changed grants apply to a new one.
    with pytest.raises(PermissionError, match="not enabled for the remote source"):
        resolver.resolve(ref("ORDERS", "ERP", "SALES"), "UPDATE")
    resolver = OracleResolver(cfg, MagicMock(), policy)
    with pytest.raises(PermissionError, match="readonly"):
        resolver.resolve(ref("ORDERS", "ERP", "SALES"), "UPDATE")
    cfg.sources["sales"] = cfg.sources["sales"].model_copy(update={"readonly": False})
    resolver = OracleResolver(cfg, MagicMock(), policy)
    assert resolver.resolve(ref("ORDERS", "ERP", "SALES"), "UPDATE").source == "sales"


def test_remote_synonym_path_and_canonical_api(monkeypatch):
    fake, cfg, policy, _ = setup(monkeypatch, sources={"sales": {"allowed_schemas": ["ERP"]}})
    fake.synonyms["local", "ERP", "REMOTE"] = ("ERP", "ORDERS", "SALES.EXAMPLE")
    result = resolve_object(cfg, MagicMock(), policy, "ERP", "REMOTE")
    assert result["target"] == {"source": "sales", "schema": "ERP", "name": "ORDERS", "object_type": "TABLE"}
    assert result["chain"][0]["object_type"] == "SYNONYM"
    assert result["canonical_config"].source_id == "sales"
    first = result["canonical_config"].cache_path
    fake.route["remote_user"] = "OTHER"
    assert resolve_object(cfg, MagicMock(), policy, "ERP", "REMOTE")["canonical_config"].cache_path != first


def test_live_dual_intrinsic_shadow_and_explicit_deny(monkeypatch):
    fake, _, _, resolver = setup(monkeypatch, allowed_schemas=["HR"])
    assert resolver.resolve(ref("DUAL")).schema == "SYS"
    fake.objects["local", "ERP", "DUAL"] = "TABLE"
    with pytest.raises(PermissionError):
        resolver.resolve(ref("DUAL"))
    del fake.objects["local", "ERP", "DUAL"]
    resolver.policy.scope.denied_tables = ["SYS.DUAL"]
    with pytest.raises(PermissionError):
        resolver.resolve(ref("DUAL"))
    resolver.policy.scope.denied_tables = ["PUBLIC.DUAL"]
    with pytest.raises(PermissionError):
        resolver.resolve(ref("DUAL"))


def test_view_expands_live_sources_and_rejects_custom_function(monkeypatch):
    fake, _, _, resolver = setup(monkeypatch, allowed_tables=["ERP.VIEW", "ERP.ORDERS"])
    fake.objects["local", "ERP", "VIEW"] = "VIEW"
    monkeypatch.setattr(catalog, "get_object_definition", lambda *a, **kw: "SELECT * FROM ERP.CUSTOMERS")
    with pytest.raises(PermissionError):
        resolver.resolve(ref("VIEW"))
    monkeypatch.setattr(catalog, "get_object_definition", lambda *a, **kw: "SELECT CUSTOM_FUNCTION(id) FROM ERP.ORDERS")
    with pytest.raises(ValueError):
        resolver.resolve(ref("VIEW"))
    monkeypatch.setattr(catalog, "get_object_definition", lambda *a, **kw: "SELECT * FROM ERP.ORDERS")
    assert resolver.resolve(ref("VIEW")).name == "VIEW"


def test_restricted_view_can_use_public_intrinsic_dual(monkeypatch):
    fake, cfg, policy, resolver = setup(monkeypatch, allowed_tables=["ERP.V"])
    fake.objects["local", "ERP", "V"] = "VIEW"
    monkeypatch.setattr(catalog, "get_object_definition", lambda *a, **kw: "SELECT 1 AS ID FROM DUAL")
    assert resolver.resolve(ref("V", "ERP")).name == "V"
    result = resolve_object(cfg, MagicMock(), policy, "ERP", "V")
    assert len(result["chain"]) == 1
    assert result["chain"][0]["name"] == "V"


def test_global_database_limit_also_applies_to_remote_metadata(monkeypatch):
    _, _, _, resolver = setup(monkeypatch, allowed_databases=["other"], sources={"sales": {}})
    with pytest.raises(PermissionError, match="global policy"):
        resolver.source_config("sales")


def test_resolver_authority_uses_sys_dictionary_even_with_shadowed_current_schema(monkeypatch):
    fake, _, _, resolver = setup(monkeypatch)
    resolver.resolve(ref("ORDERS", "ERP"))
    assert all("FROM sys.all_" in sql for sql, _ in fake.calls)
