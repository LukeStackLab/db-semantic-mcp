import json

import pytest

from sqlserver_semantic_mcp.config import reset_config
from sqlserver_semantic_mcp.services.policy_service import PolicyService


def test_service_initialises_with_builtin(monkeypatch):
    monkeypatch.delenv("SEMANTIC_MCP_POLICY_FILE", raising=False)
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_DATABASE", "x")
    reset_config()
    svc = PolicyService()
    svc.load()
    assert svc.current_policy().profile_name == "readonly"


def test_validate_select(monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_DATABASE", "x")
    reset_config()
    svc = PolicyService()
    svc.load()
    res = svc.validate("SELECT 1")
    assert res["allowed"] is True
    assert res["intent"]["primary_operation"] == "SELECT"


def test_validate_blocked(monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_DATABASE", "x")
    reset_config()
    svc = PolicyService()
    svc.load()
    res = svc.validate("UPDATE T SET x=1 WHERE Id=1")
    assert res["allowed"] is False


def test_reload(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # isolate from local .env
    pf = tmp_path / "p.json"
    pf.write_text(json.dumps({"active_profile": "ro", "profiles": {"ro": {"profile_name": "ro"}}}))
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_DATABASE", "x")
    monkeypatch.setenv("SEMANTIC_MCP_POLICY_FILE", str(pf))
    monkeypatch.delenv("SEMANTIC_MCP_POLICY_PROFILE", raising=False)
    reset_config()
    svc = PolicyService()
    svc.load()
    assert svc.current_policy().profile_name == "ro"
    svc.reload()
    assert svc.current_policy().profile_name == "ro"


@pytest.mark.parametrize(
    "connection, write, sql",
    [
        ("", False, "SELECT 1\nDELETE dbo.T"),
        ("fixed", False, "SELECT 1\nDELETE dbo.T"),
        ("fixed", True, "UPDATE dbo.T SET x = 1 WHERE id = 2"),
    ],
)
def test_validation_masks_and_scans_keywords_once(mocker, connection, write, sql):
    from sqlserver_semantic_mcp.config import Config
    from sqlserver_semantic_mcp.policy.models import PolicyProfile
    from sqlserver_semantic_mcp.services import policy_service

    cfg = Config(
        _env_file=None,
        mssql_server="x",
        mssql_database="x",
        connection_name=connection,
        policy_profile="read_write_safe" if write else "readonly",
    )
    service = PolicyService(cfg)
    service._policy = PolicyProfile(profile_name="test", operations={"update": write})
    mask = mocker.spy(policy_service, "mask_literals")
    scan = mocker.spy(policy_service, "keyword_hits_in_masked")

    result = service.validate(sql)

    assert result["allowed"] is write
    mask.assert_called_once_with(sql)
    scan.assert_called_once_with(mask.spy_return)
    if not write:
        assert result["denial"]["matches"] == [{"keyword": "DELETE", "line": 2, "column": 1}]
