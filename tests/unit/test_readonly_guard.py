import json

import pytest

from db_semantic_mcp.config import reset_config
from db_semantic_mcp.policy.readonly_guard import (
    find_write_keywords,
    mask_literals,
)
from db_semantic_mcp.services.policy_service import PolicyService


@pytest.fixture
def readonly_svc(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # isolate from local .env
    monkeypatch.delenv("SEMANTIC_MCP_POLICY_FILE", raising=False)
    monkeypatch.delenv("SEMANTIC_MCP_POLICY_PROFILE", raising=False)
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "x")
    reset_config()
    svc = PolicyService()
    svc.load()
    return svc


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 DELETE dbo.X",
        "WITH c AS (SELECT 1 x) UPDATE dbo.X SET a=1",
        "SELECT * INTO dbo.NEW_T FROM dbo.X",
        "SELECT 1 UPDATE dbo.X SET a=1 COMMIT",
        "SELECT '--' DELETE dbo.X",
        "SELECT '/*' DELETE dbo.X --*/",
        "SELECT 1\nEXEC sp_executesql N'DELETE dbo.X'",
        "SELECT NEXT VALUE FOR dbo.Seq",
        "SELECT * FROM OPENQUERY(LNK, 'SELECT 1')",
        "SELECT 1 DISABLE TRIGGER trg ON dbo.X",
    ],
)
def test_readonly_rejects_hidden_writes(readonly_svc, sql):
    res = readonly_svc.validate(sql)
    assert res["allowed"] is False
    assert "Select-only policy" in res["reason"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT [DELETE], UPDATE_DATE, CREATE_USER FROM dbo.T",
        "SELECT * FROM dbo.T WHERE NOTE = N'please update, drop it'",
        'SELECT "INSERT" FROM dbo.T',
        "SELECT 1 -- delete later",
        "SELECT 1 /* drop table */",
        "WITH c AS (SELECT 1 x) SELECT x FROM c",
        "SELECT 'it''s' AS s",
    ],
)
def test_readonly_allows_plain_reads(readonly_svc, sql):
    assert readonly_svc.validate(sql)["allowed"] is True


def test_nested_comment_errs_towards_reject(readonly_svc):
    # SQL Server nests block comments, so this is really just SELECT 1;
    # ending the comment at the first */ over-rejects, never under-rejects.
    res = readonly_svc.validate("/* /* */ DELETE dbo.X */ SELECT 1")
    assert res["allowed"] is False


def test_write_profile_is_not_guarded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pf = tmp_path / "p.json"
    pf.write_text(
        json.dumps(
            {
                "active_profile": "rw",
                "profiles": {
                    "rw": {
                        "profile_name": "rw",
                        "operations": {"select": True, "update": True},
                    }
                },
            }
        )
    )
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "x")
    monkeypatch.setenv("SEMANTIC_MCP_POLICY_FILE", str(pf))
    monkeypatch.delenv("SEMANTIC_MCP_POLICY_PROFILE", raising=False)
    reset_config()
    svc = PolicyService()
    svc.load()
    assert svc.validate("UPDATE dbo.T SET x=1 WHERE Id=1")["allowed"] is True
    assert svc.validate("SELECT * INTO dbo.NEW_T FROM dbo.X")["allowed"] is False


def test_mask_literals_keeps_code_outside_tokens():
    masked = mask_literals("SELECT 'a''b', [x]]y], \"q\" FROM t -- c\nWHERE 1=1")
    assert "a" not in masked.replace("WHERE", "").replace("SELECT", "")
    assert "FROM t" in masked
    assert "WHERE 1=1" in masked


def test_find_write_keywords_dedupes_and_normalises():
    hits = find_write_keywords("select 1 delete x delete y next  value for s")
    assert hits == ["DELETE", "NEXT VALUE FOR"]
