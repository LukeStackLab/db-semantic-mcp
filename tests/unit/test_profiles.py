import json

import pytest

from sqlserver_semantic_mcp.credentials import CredentialReference
from sqlserver_semantic_mcp.profiles import (
    ConnectionProfile, load_profile, policy_bytes, profile_bytes,
)
from sqlserver_semantic_mcp.services.policy_service import PolicyService
from sqlserver_semantic_mcp.setup.files import SetupError, atomic_write


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setenv("PROFILE_TEST_PASSWORD", "not-in-profile")

    def write(name="erp", **kwargs):
        profile = ConnectionProfile(
            name=name, server="sql.example", database="ERP", user="reader",
            credential=CredentialReference(storage="env", key="PROFILE_TEST_PASSWORD"),
            **kwargs,
        )
        path = tmp_path / name / "connection.json"
        atomic_write(path, profile_bytes(profile))
        atomic_write(path.parent / "policy.json", policy_bytes(profile))
        return path
    return write


def test_profiles_isolate_same_database_and_inherited_env(saved, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "SEMANTIC_MCP_MSSQL_DATABASE=Wrong\nSEMANTIC_MCP_POLICY_PROFILE=admin\n"
    )
    for key, value in {
        "MSSQL_SERVER": "wrong-host", "MSSQL_DATABASE": "Wrong",
        "MSSQL_PASSWORD": "wrong-password", "POLICY_PROFILE": "admin",
        "DEFAULT_TOKEN_BUDGET_HINT": "high", "CACHE_PATH": "/wrong.db",
        "STRICT_ROWS_AFFECTED_CAP": "false", "MSSQL_WINDOWS_AUTH": "true",
    }.items():
        monkeypatch.setenv("SEMANTIC_MCP_" + key, value)
    configs = [load_profile(saved(f"erp{n}")) for n in range(5)]
    assert len({cfg.cache_path for cfg in configs}) == 5
    assert len({cfg.policy_file for cfg in configs}) == 5
    for cfg in configs:
        assert cfg.mssql_server == "sql.example"
        assert cfg.mssql_database == "ERP"
        assert cfg.mssql_password == "not-in-profile"
        assert cfg.policy_profile == "readonly"
        assert cfg.default_token_budget_hint == "medium"
        assert cfg.strict_rows_affected_cap is True
        assert cfg.mssql_windows_auth is False


def test_changed_target_gets_new_cache(saved):
    path = saved()
    old = load_profile(path).cache_path
    profile = json.loads(path.read_text())
    profile["server"] = "another-host"
    path.write_text(json.dumps(profile))
    assert load_profile(path).cache_path != old


def test_tighter_policy_is_not_overridden(saved):
    path = saved()
    raw = json.loads((path.parent / "policy.json").read_text())
    raw["profiles"]["readonly"]["constraints"]["max_rows_returned"] = 20
    raw["profiles"]["readonly"]["constraints"]["query_timeout_seconds"] = 5
    (path.parent / "policy.json").write_text(json.dumps(raw))
    cfg = load_profile(path)
    policy = PolicyService(cfg)
    assert policy.current_policy().constraints.max_rows_returned == 20
    assert cfg.query_timeout == 5


@pytest.mark.parametrize("name", ["../other", "a/b", "a.b", "CON", "LPT1", "x" * 65])
def test_invalid_profile_name(name):
    with pytest.raises(ValueError):
        ConnectionProfile(name=name, server="s", database="d", user="u")


def test_missing_policy_and_credential_fail_closed(saved, monkeypatch):
    path = saved()
    monkeypatch.delenv("PROFILE_TEST_PASSWORD")
    with pytest.raises(SetupError):
        load_profile(path)
    (path.parent / "policy.json").unlink()
    with pytest.raises(SetupError, match="policy"):
        load_profile(path)


def test_readonly_profile_cannot_be_escalated(saved):
    path = saved()
    raw = json.loads((path.parent / "policy.json").read_text())
    raw["profiles"]["readonly"]["operations"]["update"] = True
    (path.parent / "policy.json").write_text(json.dumps(raw))
    with pytest.raises(SetupError, match="權限"):
        load_profile(path)


@pytest.mark.parametrize("sql", [
    "SELECT 1 DELETE dbo.Orders",
    "UPDATE dbo.Orders SET x=1 WHERE id=1 DELETE dbo.Orders",
    "UPDATE dbo.Orders SET x=1 WHERE id=1 UPDATE dbo.Orders SET x=2 WHERE id=2",
    "UPDATE dbo.Orders SET note='WHERE'",
    "UPDATE dbo.Orders SET x=1 OUTPUT inserted.x WHERE id=1",
    "INSERT INTO dbo.Orders SELECT * FROM dbo.Other",
])
def test_fixed_writer_rejects_hidden_or_unmeasurable_writes(saved, sql):
    cfg = load_profile(saved(readonly=False))
    assert PolicyService(cfg).validate(sql)["allowed"] is False


def test_fixed_writer_allows_simple_dml_and_reload_fails_closed(saved):
    cfg = load_profile(saved(readonly=False))
    service = PolicyService(cfg)
    assert service.validate("INSERT INTO dbo.Orders (id) VALUES (1)")["allowed"]
    assert service.validate("UPDATE dbo.Orders SET x=1 WHERE id=1")["allowed"]
    old = service.current_policy()
    from pathlib import Path
    Path(cfg.policy_file).write_text("invalid")
    with pytest.raises(ValueError, match="refusing"):
        service.reload()
    assert service.current_policy() is old


def test_profile_error_does_not_echo_unknown_secret(tmp_path):
    path = tmp_path / "connection.json"
    path.write_text('{"password":"DO_NOT_ECHO"}')
    with pytest.raises(SetupError) as error:
        load_profile(path)
    assert "DO_NOT_ECHO" not in str(error.value)
