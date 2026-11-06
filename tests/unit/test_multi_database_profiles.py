import json

import pytest
from pydantic import ValidationError

from db_semantic_mcp.config import Config
from db_semantic_mcp.policy.models import PolicyProfile, PolicyScope
from db_semantic_mcp.policy.scope import object_allowed
from db_semantic_mcp.profiles import ConnectionProfile, config_for_profile, policy_bytes, profile_bytes
from db_semantic_mcp.services.policy_service import PolicyService


def profile(**kwargs):
    return ConnectionProfile(name="business", server="sql", database="ERP", user="reader", **kwargs)


def test_v3_roundtrip_and_v2_compatibility(tmp_path):
    p = profile(format_version=3, databases=["ERP", "CRM"])
    assert ConnectionProfile.model_validate_json(profile_bytes(p)) == p
    cfg = config_for_profile(p, tmp_path, "secret")
    assert (cfg.profile_version, cfg.databases) == (3, ["ERP", "CRM"])
    assert profile().databases == ["ERP"]
    assert "databases" not in json.loads(profile_bytes(profile()))
    assert Config.from_fixed(server="sql", database="ERP").databases == ["ERP"]


@pytest.mark.parametrize(
    "changes",
    [
        {"databases": []},
        {"databases": ["ERP", "ERP"]},
        {"databases": ["CRM"]},
        {"databases": ["ERP", "*"]},
        {"databases": ["ERP", " "]},
        {"databases": ["ERP"], "database_type": "oracle"},
        {"databases": ["ERP"], "typo": True},
    ],
)
def test_v3_rejects_invalid(changes):
    with pytest.raises(ValidationError):
        profile(format_version=3, **changes)


def test_v2_rejects_new_database_configuration():
    with pytest.raises(ValidationError):
        profile(databases=["ERP", "CRM"])


def test_scope_is_global_and_database_intersection():
    scope = PolicyScope.model_validate(
        {
            "allowed_databases": ["ERP", "CRM"],
            "allowed_schemas": ["dbo", "sales"],
            "denied_tables": ["Secret"],
            "database_scopes": {
                "ERP": {"allowed_schemas": ["dbo"], "denied_tables": ["Orders"]},
                "CRM": {"allowed_schemas": ["sales"]},
            },
        }
    )
    assert object_allowed(scope, "ERP", "dbo", "Customers")
    assert not object_allowed(scope, "ERP", "sales", "Customers")
    assert not object_allowed(scope, "ERP", "dbo", "Orders")
    assert not object_allowed(scope, "CRM", "sales", "Secret")
    assert not object_allowed(scope, "crm", "sales", "Customers")
    with pytest.raises(ValidationError):
        PolicyScope(database_scopes={"ERP": {"allowed_schema": ["dbo"]}})


@pytest.mark.parametrize("scope", [{}, {"allowed_databases": ["ERP", "CRM"], "database_scopes": {"ERP": {}}}])
def test_v3_policy_requires_explicit_database_scopes(scope):
    cfg = Config.from_fixed(server="sql", database="ERP", profile_version=3, databases=["ERP", "CRM"])
    cfg._policy_snapshot = PolicyProfile(profile_name="readonly", scope=scope)
    with pytest.raises(ValueError):
        PolicyService(cfg).current_policy()


def test_v3_policy_intersects_configuration_and_snapshot():
    cfg = Config.from_fixed(server="sql", database="ERP", profile_version=3, databases=["ERP", "CRM"])
    cfg._policy_snapshot = PolicyProfile(
        profile_name="readonly",
        scope={"allowed_databases": ["ERP", "Other"], "database_scopes": {"ERP": {}, "Other": {}}},
    )
    service = PolicyService(cfg)
    assert service.current_policy().scope.allowed_databases == ["ERP"]
    assert not service.object_allowed("dbo", "Orders", "Other")
    assert cfg._policy_snapshot.scope.allowed_databases == ["ERP", "Other"]


def test_upgrade_preserves_restrictions_and_expands_database_list():
    old = profile().policy()
    old.scope.allowed_schemas = ["dbo"]
    old.scope.denied_tables = ["Secret"]
    raw = json.dumps({"profiles": {"readonly": old.model_dump()}}).encode()
    p = profile(format_version=3, databases=["ERP", "CRM"])
    saved = json.loads(policy_bytes(p, previous_policy=raw, previous_database="ERP"))
    scope = saved["profiles"]["readonly"]["scope"]
    assert scope["allowed_databases"] == ["ERP", "CRM"]
    assert scope["allowed_schemas"] == ["dbo"]
    assert scope["denied_tables"] == ["Secret"]
    assert set(scope["database_scopes"]) == {"ERP", "CRM"}


def test_policy_can_remove_startup_database_without_blocking_other_databases():
    cfg = Config.from_fixed(server="sql", database="ERP", profile_version=3, databases=["ERP", "CRM"])
    cfg._policy_snapshot = PolicyProfile(
        profile_name="readonly", scope={"allowed_databases": ["CRM"], "database_scopes": {"CRM": {}}}
    )
    service = PolicyService(cfg)
    assert service.current_policy().scope.allowed_databases == ["CRM"]
    assert not service.object_allowed("dbo", "Orders", "ERP")
    assert service.object_allowed("dbo", "Orders", "CRM")


def test_table_allowlists_are_intersected_and_denies_always_win():
    scope = PolicyScope(
        allowed_databases=["ERP"],
        allowed_tables=["dbo.Orders", "dbo.Customers"],
        database_scopes={"ERP": {"allowed_tables": ["Orders", "dbo.Other"], "denied_tables": ["dbo.Customers"]}},
    )
    assert object_allowed(scope, "ERP", "dbo", "Orders")
    assert not object_allowed(scope, "ERP", "dbo", "Customers")
    assert not object_allowed(scope, "ERP", "dbo", "Other")


@pytest.mark.parametrize("version", [1, 4, "3"])
def test_unknown_or_noninteger_profile_version_rejected(version):
    with pytest.raises(ValidationError):
        profile(format_version=version)


@pytest.mark.parametrize("databases", [[], ["ERP", "ERP"], ["CRM"], ["ERP", "*"]])
def test_config_rejects_invalid_v3_database_list(databases):
    with pytest.raises(ValidationError):
        Config.from_fixed(server="sql", database="ERP", profile_version=3, databases=databases)


def test_upgrade_preserves_existing_per_database_restrictions():
    previous = profile(format_version=3, databases=["ERP"]).policy()
    previous.scope.database_scopes["ERP"].allowed_tables = ["dbo.Orders"]
    raw = json.dumps({"profiles": {"readonly": previous.model_dump()}}).encode()
    p = profile(format_version=3, databases=["ERP", "CRM"])
    result = json.loads(policy_bytes(p, previous_policy=raw))["profiles"]["readonly"]["scope"]
    assert result["database_scopes"]["ERP"]["allowed_tables"] == ["dbo.Orders"]
    assert result["database_scopes"]["CRM"]["allowed_tables"] == []


@pytest.mark.parametrize("operation", ["object_allowed", "validate"])
def test_empty_database_intersection_fails_closed_on_first_call(operation):
    cfg = Config.from_fixed(server="sql", database="ERP", profile_version=3, databases=["ERP", "CRM"])
    cfg._policy_snapshot = PolicyProfile(
        profile_name="readonly", scope={"allowed_databases": ["Other"], "database_scopes": {"Other": {}}}
    )
    service = PolicyService(cfg)
    with pytest.raises(ValueError, match="No configured database"):
        if operation == "object_allowed":
            service.object_allowed("dbo", "Orders")
        else:
            service.validate("SELECT * FROM dbo.Orders")


def test_reload_with_empty_database_intersection_fails_closed():
    cfg = Config.from_fixed(server="sql", database="ERP", profile_version=3, databases=["ERP", "CRM"])
    cfg._policy_snapshot = PolicyProfile(
        profile_name="readonly", scope={"allowed_databases": ["CRM"], "database_scopes": {"CRM": {}}}
    )
    service = PolicyService(cfg)
    assert service.object_allowed("dbo", "Orders", "CRM")
    cfg._policy_snapshot = PolicyProfile(
        profile_name="readonly", scope={"allowed_databases": ["Other"], "database_scopes": {"Other": {}}}
    )
    with pytest.raises(ValueError, match="No configured database"):
        service.reload()
    with pytest.raises(ValueError, match="No configured database"):
        service.object_allowed("dbo", "Orders", "CRM")
    with pytest.raises(ValueError, match="No configured database"):
        service.validate("SELECT * FROM CRM.dbo.Orders")
