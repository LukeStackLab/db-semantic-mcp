"""Tests for SEMANTIC_MCP_TOOL_PROFILE gating (P2)."""

import pytest


@pytest.fixture
def base_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_DATABASE", "x")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_PASSWORD", "p")


def test_profile_defaults_to_all_registers_every_group(base_env):
    from sqlserver_semantic_mcp.config import reset_config
    from sqlserver_semantic_mcp.server.app import _TOOL_REGISTRY
    from sqlserver_semantic_mcp.server.tools import register_all

    reset_config()
    _TOOL_REGISTRY.clear()
    register_all()
    # All tool groups registered
    expected = {
        "get_tables",
        "describe_table",
        "get_table_relationships",
        "find_join_path",
        "get_dependency_chain",
        "describe_object",
        "detect_lookup_tables",
        "get_execution_policy",
        "plan_or_execute_query",
        "refresh_schema_cache",
    }
    registered = set(_TOOL_REGISTRY.keys())
    assert expected.issubset(registered)


def test_profile_metadata_only_registers_two_tools(base_env, monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_TOOL_PROFILE", "metadata")
    from sqlserver_semantic_mcp.config import reset_config
    from sqlserver_semantic_mcp.server.app import _TOOL_REGISTRY
    from sqlserver_semantic_mcp.server.tools import register_all

    reset_config()
    _TOOL_REGISTRY.clear()
    register_all()

    names = set(_TOOL_REGISTRY.keys())
    assert names == {"get_tables", "describe_table"}
    assert "classify_table" not in names
    assert "describe_object" not in names
    assert "plan_or_execute_query" not in names


def test_profile_multiple_groups(base_env, monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_TOOL_PROFILE", "metadata,semantic")
    from sqlserver_semantic_mcp.config import reset_config
    from sqlserver_semantic_mcp.server.app import _TOOL_REGISTRY
    from sqlserver_semantic_mcp.server.tools import register_all

    reset_config()
    _TOOL_REGISTRY.clear()
    register_all()

    names = set(_TOOL_REGISTRY.keys())
    assert "get_tables" in names
    assert "detect_lookup_tables" in names
    assert "describe_object" not in names
    assert "plan_or_execute_query" not in names


def test_profile_unknown_group_raises(base_env, monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_TOOL_PROFILE", "metadata,bogus")
    from sqlserver_semantic_mcp.config import reset_config
    from sqlserver_semantic_mcp.server.app import _TOOL_REGISTRY
    from sqlserver_semantic_mcp.server.tools import register_all

    reset_config()
    _TOOL_REGISTRY.clear()
    with pytest.raises(ValueError, match="bogus"):
        register_all()


def test_profile_empty_string_treated_as_all(base_env, monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_TOOL_PROFILE", "")
    from sqlserver_semantic_mcp.config import reset_config
    from sqlserver_semantic_mcp.server.app import _TOOL_REGISTRY
    from sqlserver_semantic_mcp.server.tools import register_all

    reset_config()
    _TOOL_REGISTRY.clear()
    register_all()

    names = set(_TOOL_REGISTRY.keys())
    assert "get_tables" in names
    assert "plan_or_execute_query" in names
