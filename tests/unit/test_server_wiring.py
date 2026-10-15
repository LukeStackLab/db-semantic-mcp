def test_registrations_load(monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "x")
    monkeypatch.setenv("SEMANTIC_MCP_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_PASSWORD", "p")
    from db_semantic_mcp.config import reset_config

    reset_config()
    from db_semantic_mcp.server.app import _TOOL_REGISTRY
    from db_semantic_mcp.server.tools import register_all

    _TOOL_REGISTRY.clear()
    register_all()
    expected = [
        # metadata
        "get_tables",
        "describe_table",
        # relationship
        "get_table_relationships",
        "find_join_path",
        "get_dependency_chain",
        # object
        "describe_object",
        # semantic
        "detect_lookup_tables",
        # policy
        "get_execution_policy",
        # query
        "plan_or_execute_query",
        # cache
        "refresh_schema_cache",
    ]
    for name in expected:
        assert name in _TOOL_REGISTRY, f"tool not registered: {name}"
    assert len(_TOOL_REGISTRY) >= len(expected)


def test_query_group_consolidated(monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "x")
    monkeypatch.setenv("SEMANTIC_MCP_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_PASSWORD", "p")
    from db_semantic_mcp.config import reset_config

    reset_config()
    from db_semantic_mcp.server.app import _TOOL_REGISTRY
    from db_semantic_mcp.server.tools import register_all

    _TOOL_REGISTRY.clear()
    register_all()
    names = set(_TOOL_REGISTRY.keys())
    assert "plan_or_execute_query" in names
    for gone in ("validate_query", "run_safe_query", "preview_safe_query", "estimate_execution_risk"):
        assert gone not in names


def test_get_columns_tool_removed(monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "x")
    monkeypatch.setenv("SEMANTIC_MCP_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_PASSWORD", "p")
    from db_semantic_mcp.config import reset_config

    reset_config()
    from db_semantic_mcp.server.app import _TOOL_REGISTRY
    from db_semantic_mcp.server.tools import register_all

    _TOOL_REGISTRY.clear()
    register_all()
    names = set(_TOOL_REGISTRY.keys())
    assert "get_columns" not in names
    assert "describe_table" in names


def test_classify_and_analyze_tools_removed(monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "x")
    monkeypatch.setenv("SEMANTIC_MCP_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_PASSWORD", "p")
    from db_semantic_mcp.config import reset_config

    reset_config()
    from db_semantic_mcp.server.app import _TOOL_REGISTRY
    from db_semantic_mcp.server.tools import register_all

    _TOOL_REGISTRY.clear()
    register_all()
    names = set(_TOOL_REGISTRY.keys())
    assert "classify_table" not in names
    assert "analyze_columns" not in names
    assert "detect_lookup_tables" in names


def test_summarize_table_tool_removed(monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "x")
    monkeypatch.setenv("SEMANTIC_MCP_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_PASSWORD", "p")
    from db_semantic_mcp.config import reset_config

    reset_config()
    from db_semantic_mcp.server.app import _TOOL_REGISTRY
    from db_semantic_mcp.server.tools import register_all

    _TOOL_REGISTRY.clear()
    register_all()
    names = set(_TOOL_REGISTRY.keys())
    assert "summarize_table_for_joining" not in names
    assert "describe_table" in names


def test_score_join_candidate_tool_removed(monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "x")
    monkeypatch.setenv("SEMANTIC_MCP_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_PASSWORD", "p")
    from db_semantic_mcp.config import reset_config

    reset_config()
    from db_semantic_mcp.server.app import _TOOL_REGISTRY
    from db_semantic_mcp.server.tools import register_all

    _TOOL_REGISTRY.clear()
    register_all()
    names = set(_TOOL_REGISTRY.keys())
    assert "score_join_candidate" not in names
    assert "find_join_path" in names


def test_object_tools_consolidated(monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "x")
    monkeypatch.setenv("SEMANTIC_MCP_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_PASSWORD", "p")
    from db_semantic_mcp.config import reset_config

    reset_config()
    from db_semantic_mcp.server.app import _TOOL_REGISTRY
    from db_semantic_mcp.server.tools import register_all

    _TOOL_REGISTRY.clear()
    register_all()
    names = set(_TOOL_REGISTRY.keys())
    assert "describe_object" in names
    for gone in ("describe_view", "describe_procedure", "trace_object_dependencies", "summarize_object_for_impact"):
        assert gone not in names


def test_policy_tools_consolidated(monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "x")
    monkeypatch.setenv("SEMANTIC_MCP_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_PASSWORD", "p")
    from db_semantic_mcp.config import reset_config

    reset_config()
    from db_semantic_mcp.server.app import _TOOL_REGISTRY
    from db_semantic_mcp.server.tools import register_all

    _TOOL_REGISTRY.clear()
    register_all()
    names = set(_TOOL_REGISTRY.keys())
    assert "get_execution_policy" in names
    for gone in ("validate_sql_against_policy", "refresh_policy"):
        assert gone not in names


def test_metrics_tools_consolidated(monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "x")
    monkeypatch.setenv("SEMANTIC_MCP_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_PASSWORD", "p")
    from db_semantic_mcp.config import reset_config

    reset_config()
    from db_semantic_mcp.server.app import _TOOL_REGISTRY
    from db_semantic_mcp.server.tools import register_all

    _TOOL_REGISTRY.clear()
    register_all()
    names = set(_TOOL_REGISTRY.keys())
    assert "tool_metrics" in names
    for gone in ("get_tool_metrics", "reset_tool_metrics"):
        assert gone not in names


async def test_meta_tools_removed_bundle_resource_kept(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "x")
    monkeypatch.setenv("SEMANTIC_MCP_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_PASSWORD", "p")
    monkeypatch.setenv("SEMANTIC_MCP_CACHE_PATH", str(tmp_path / "wiring.db"))
    from db_semantic_mcp.config import reset_config

    reset_config()
    from db_semantic_mcp.server.app import _TOOL_REGISTRY, reset_context

    reset_context()
    from db_semantic_mcp.server.tools import register_all

    _TOOL_REGISTRY.clear()
    register_all()
    names = set(_TOOL_REGISTRY.keys())
    assert "suggest_next_tool" not in names
    assert "bundle_context_for_next_step" not in names

    from db_semantic_mcp.config import get_config
    from db_semantic_mcp.infrastructure.cache.store import init_store

    await init_store(get_config().cache_path)

    from pydantic import AnyUrl

    from db_semantic_mcp.server.resources import schema as res

    body = await res.read_resource(AnyUrl("semantic://bundle/joining/dbo.Orders"))
    assert body is not None  # resource still works


EXPECTED_TOOLS = {
    "get_tables",
    "describe_table",
    "describe_object",
    "discover_relevant_tables",
    "get_table_relationships",
    "find_join_path",
    "get_dependency_chain",
    "plan_or_execute_query",
    "get_execution_policy",
    "detect_lookup_tables",
    "refresh_schema_cache",
    "tool_metrics",
}


def test_exactly_twelve_tools(monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "x")
    monkeypatch.setenv("SEMANTIC_MCP_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_PASSWORD", "p")
    from db_semantic_mcp.config import reset_config

    reset_config()
    from db_semantic_mcp.server.app import _TOOL_REGISTRY
    from db_semantic_mcp.server.tools import register_all

    _TOOL_REGISTRY.clear()
    register_all()
    assert set(_TOOL_REGISTRY.keys()) == EXPECTED_TOOLS
    assert len(_TOOL_REGISTRY) == 12


def test_duplicate_tool_registration_raises(monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "x")
    monkeypatch.setenv("SEMANTIC_MCP_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_PASSWORD", "p")
    from db_semantic_mcp.config import reset_config

    reset_config()
    from mcp.types import Tool

    from db_semantic_mcp.server.app import _TOOL_REGISTRY, register_tool

    async def handler(args):
        return {"ok": True}

    _TOOL_REGISTRY.clear()
    register_tool(
        Tool(name="dup", description="x", inputSchema={"type": "object"}),
        handler,
    )
    import pytest

    with pytest.raises(ValueError, match="Duplicate tool registration: dup"):
        register_tool(
            Tool(name="dup", description="x", inputSchema={"type": "object"}),
            handler,
        )
