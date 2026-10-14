"""Regression tests covering fixes from the three-agent audit."""
import json
import pytest
from sqlserver_semantic_mcp.policy.analyzer import analyze_sql
from sqlserver_semantic_mcp.policy.enforcer import enforce
from sqlserver_semantic_mcp.policy.loader import load_policy_from_file
from sqlserver_semantic_mcp.policy.models import (
    PolicyProfile, PolicyOperations, PolicyScope,
)
from sqlserver_semantic_mcp.domain.enums import SqlOperation


# ---- Fix 1: policy loader fallback on bad files ----

def test_fix1_malformed_json_fails_closed(tmp_path):
    pf = tmp_path / "bad.json"
    pf.write_text("{not valid json")
    with pytest.raises(ValueError, match="policy"):
        load_policy_from_file(str(pf), profile_override=None)


def test_fix1_bad_schema_fails_closed(tmp_path):
    pf = tmp_path / "bad.json"
    # Valid JSON but wrong structure
    pf.write_text(json.dumps({"foo": "bar"}))
    with pytest.raises(ValueError, match="policy"):
        load_policy_from_file(str(pf), profile_override=None)


def test_fix1_profile_not_found_still_raises(tmp_path):
    """Profile misconfiguration should still surface."""
    pf = tmp_path / "ok.json"
    pf.write_text(json.dumps({
        "active_profile": "ghost",
        "profiles": {"a": {"profile_name": "a"}}
    }))
    with pytest.raises(ValueError, match="not found"):
        load_policy_from_file(str(pf), profile_override=None)


# ---- Fix 2: bare DELETE syntax ----

def test_fix2_bare_delete_detects_table():
    intent = analyze_sql("DELETE Users WHERE Id = 1")
    assert intent.primary_operation == SqlOperation.DELETE
    targets = [t.strip("[]") for t in intent.affected_tables]
    assert "Users" in targets


def test_fix2_delete_from_still_works():
    intent = analyze_sql("DELETE FROM Users WHERE Id = 1")
    targets = [t.strip("[]") for t in intent.affected_tables]
    assert "Users" in targets


# ---- Fix 3: allowed_schemas rejects unqualified tables ----

def test_fix3_unqualified_table_rejected_when_schemas_restricted():
    p = PolicyProfile(
        profile_name="t",
        scope=PolicyScope(allowed_schemas=["dbo"]),
    )
    r = enforce(analyze_sql("SELECT * FROM Users"), p)
    assert r.allowed is False
    assert "unqualified" in r.reason.lower() or "schema" in r.reason.lower()


def test_fix3_qualified_table_respects_schemas():
    p = PolicyProfile(
        profile_name="t",
        scope=PolicyScope(allowed_schemas=["dbo"]),
    )
    r = enforce(analyze_sql("SELECT * FROM dbo.Users"), p)
    assert r.allowed is True


def test_fix3_disallowed_schema_rejected():
    p = PolicyProfile(
        profile_name="t",
        scope=PolicyScope(allowed_schemas=["dbo"]),
    )
    r = enforce(analyze_sql("SELECT * FROM audit.Users"), p)
    assert r.allowed is False
    assert "audit" in r.reason


# ---- Fix 4: URI length validation ----

@pytest.mark.asyncio
async def test_fix4_malformed_uri_raises_clear_error(monkeypatch, tmp_path):
    from sqlserver_semantic_mcp.config import reset_config
    from sqlserver_semantic_mcp.infrastructure.cache.store import init_store
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_DATABASE", "x")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_PASSWORD", "p")
    monkeypatch.setenv("SEMANTIC_MCP_CACHE_PATH", str(tmp_path / "t.db"))
    reset_config()

    from sqlserver_semantic_mcp.server.app import reset_context
    reset_context()
    from sqlserver_semantic_mcp.server.resources.schema import read_resource
    from pydantic import AnyUrl

    cfg_path = tmp_path / "t.db"
    await init_store(str(cfg_path))

    with pytest.raises(ValueError, match="schema.table format"):
        await read_resource(AnyUrl("semantic://schema/tables/NoDot"))


# ---- Fix 5: detect_lookup_tables reuses cache ----

@pytest.mark.asyncio
async def test_fix5_detect_lookup_uses_cache_on_second_call(tmp_path):
    from sqlserver_semantic_mcp.infrastructure.cache.store import init_store
    from sqlserver_semantic_mcp.infrastructure.cache.structural import (
        write_structural_snapshot, StructuralSnapshot,
    )
    from sqlserver_semantic_mcp.services.semantic_service import (
        classify_table, detect_lookup_tables,
    )

    db_path = str(tmp_path / "t.db")
    await init_store(db_path)
    snap = StructuralSnapshot(
        tables=[("dbo", "StatusCode"), ("dbo", "Users")],
        columns=[
            ("dbo", "StatusCode", "Code", "nvarchar", 10, 0, None, 1),
            ("dbo", "StatusCode", "Name", "nvarchar", 50, 0, None, 2),
            ("dbo", "Users", "Id", "int", None, 0, None, 1),
            ("dbo", "Users", "Name", "nvarchar", 100, 0, None, 2),
            ("dbo", "Users", "Email", "nvarchar", 200, 0, None, 3),
        ],
        primary_keys=[("dbo", "StatusCode", "Code"), ("dbo", "Users", "Id")],
        foreign_keys=[], indexes=[], objects=[], comments=[],
    )
    await write_structural_snapshot(db_path, "db", snap)

    # Pre-classify to populate cache
    await classify_table(db_path, "db", "dbo", "StatusCode")
    await classify_table(db_path, "db", "dbo", "Users")

    # detect_lookup_tables should now read from cache
    import sqlserver_semantic_mcp.services.semantic_service as semsvc
    call_count = {"n": 0}
    original = semsvc._load_table_structure

    async def counting_wrapper(*args, **kwargs):
        call_count["n"] += 1
        return await original(*args, **kwargs)

    semsvc._load_table_structure = counting_wrapper
    try:
        results = await detect_lookup_tables(db_path, "db")
    finally:
        semsvc._load_table_structure = original

    # After cache is populated, detect_lookup_tables should not re-analyze
    assert call_count["n"] == 0
    assert any(r["table_name"] == "StatusCode" for r in results)


# ---- Fix 6: background backoff (import only — full behavior tested manually) ----

def test_fix6_background_has_exponential_backoff():
    import inspect
    from sqlserver_semantic_mcp.infrastructure import background
    src = inspect.getsource(background.background_fill_loop)
    assert "consecutive_errors" in src
    assert "max_backoff" in src or "2.0 **" in src


# ---- Fix 7: query_service cursor + truncation ----

def test_fix7_truncation_is_precise():
    from unittest.mock import patch, MagicMock
    from sqlserver_semantic_mcp.config import reset_config
    from sqlserver_semantic_mcp.services.policy_service import PolicyService
    from sqlserver_semantic_mcp.services.query_service import QueryService
    import os

    os.environ["SEMANTIC_MCP_MSSQL_SERVER"] = "x"
    os.environ["SEMANTIC_MCP_MSSQL_DATABASE"] = "x"
    os.environ["SEMANTIC_MCP_MSSQL_USER"] = "u"
    os.environ["SEMANTIC_MCP_MSSQL_PASSWORD"] = "p"
    reset_config()

    svc = PolicyService()
    svc.load()
    qs = QueryService(svc)

    with patch(
        "sqlserver_semantic_mcp.services.query_service.open_connection"
    ) as mock_open:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.description = [("Id",)]
        # Exactly limit rows, no more
        cursor.fetchmany.return_value = [(1,), (2,), (3,)]
        conn.cursor.return_value = cursor
        mock_open.return_value.__enter__.return_value = conn

        result = qs.execute_query(
            "SELECT * FROM T", max_rows=3,
            response_mode="rows", affected_rows_policy="report",
        )
        # fetchmany(limit+1=4) returned only 3 rows → not truncated
        assert result["row_count"] == 3
        assert result["truncated"] is False
        cursor.fetchmany.assert_called_once_with(4)
