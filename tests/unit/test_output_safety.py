"""Tiered detail / response-mode / budget arguments are gone; hard caps remain."""

import json

import pytest

from sqlserver_semantic_mcp import profiles
from sqlserver_semantic_mcp.config import Config
from sqlserver_semantic_mcp.server import app as server
from sqlserver_semantic_mcp.server.tools import register_all

RETIRED = {"detail", "response_mode", "return_mode", "token_budget_hint"}


def test_no_tool_schema_exposes_retired_arguments(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_DATABASE", "d")
    monkeypatch.setenv("SEMANTIC_MCP_CACHE_PATH", str(tmp_path / "c.db"))
    from sqlserver_semantic_mcp.config import reset_config

    reset_config()
    server._TOOL_REGISTRY.clear()
    register_all()
    assert server._TOOL_REGISTRY
    for name, (tool, _) in server._TOOL_REGISTRY.items():
        props = set((tool.inputSchema or {}).get("properties", {}))
        assert not props & RETIRED, name
    reset_config()


def test_config_has_no_retired_fields():
    assert not {"default_detail", "default_response_mode", "default_token_budget_hint"} & set(Config.model_fields)
    assert Config.model_fields["max_response_bytes"].default == 65536


def test_legacy_profile_with_budget_still_loads():
    legacy = {"name": "erp", "server": "s", "database": "d", "user": "u", "budget": "tiny"}
    profile = profiles.ConnectionProfile.model_validate_json(json.dumps(legacy))
    assert profile.max_response_bytes == 65536
    assert "budget" not in profile.model_dump()


def test_profile_rejects_tiny_byte_cap():
    with pytest.raises(ValueError):
        profiles.ConnectionProfile(name="erp", server="s", database="d", user="u", max_response_bytes=10)


def test_describe_table_projection_is_single_full_shape():
    from sqlserver_semantic_mcp.server.tools.metadata import _project_describe_table

    cols = [{"column_name": f"c{i}", "sql_type": "int", "description": "d"} for i in range(5)]
    out = _project_describe_table(
        {
            "schema_name": "dbo",
            "table_name": "T",
            "columns": cols,
            "column_count": 5,
            "primary_key": ["c0"],
            "indexes": [{"index_name": "IX"}],
            "description": "tbl",
        },
        {},
        max_columns=3,
    )
    assert out["table"] == "dbo.T"
    assert [c["name"] for c in out["columns"]] == ["c0", "c1", "c2"]
    assert out["columns_omitted"] is True and "columns_hint" in out
    assert out["indexes"] == [{"index_name": "IX"}] and out["description"] == "tbl"
