import pytest


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_DATABASE", "testdb")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_PASSWORD", "p")
    monkeypatch.setenv("SEMANTIC_MCP_CACHE_PATH", str(tmp_path / "t.db"))
    from sqlserver_semantic_mcp.config import reset_config

    reset_config()


@pytest.mark.asyncio
async def test_list_resources_only_summary_after_dedup(env):
    from sqlserver_semantic_mcp.server.resources import schema as mod

    resources = await mod.list_resources()

    # P2: semantic://schema/tables dropped (duplicates get_tables tool).
    assert len(resources) == 1
    uris = [str(r.uri) for r in resources]
    assert uris == ["semantic://summary/database"]


@pytest.mark.asyncio
async def test_list_resource_templates_returns_expected_patterns(env):
    from sqlserver_semantic_mcp.server.resources import schema as mod

    templates = await mod.list_resource_templates()
    patterns = [t.uriTemplate for t in templates]

    # Schema and classification templates
    assert "semantic://schema/tables/{qualified}" in patterns
    assert "semantic://analysis/classification/{qualified}" in patterns
    # Summary and bundle templates
    assert "semantic://summary/table/{qualified}" in patterns
    assert "semantic://summary/object/{type}/{qualified}" in patterns
    assert "semantic://bundle/joining/{qualified}" in patterns
