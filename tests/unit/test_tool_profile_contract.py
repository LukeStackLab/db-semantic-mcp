import pytest

from db_semantic_mcp.config import Config
from db_semantic_mcp.profiles import ConnectionProfile, config_for_profile
from db_semantic_mcp.server import app as server
from db_semantic_mcp.server.prompts import registry
from db_semantic_mcp.server.tools import register_all


@pytest.mark.parametrize(
    "profile,expected",
    [
        ("query", {"get_execution_policy", "plan_or_execute_query"}),
        ("maintenance", {"get_execution_policy", "refresh_schema_cache", "tool_metrics"}),
    ],
)
def test_named_profiles_register_only_selected_tools(profile, expected, monkeypatch):
    cfg = Config.model_construct(server="fake", database="db", tool_profile=profile)
    monkeypatch.setattr("db_semantic_mcp.config._config", cfg)
    monkeypatch.setattr(server, "_TOOL_REGISTRY", {})
    register_all()
    assert set(server._TOOL_REGISTRY) == expected


def test_fixed_profile_exposes_tool_and_discovery_options(tmp_path):
    profile = ConnectionProfile(
        name="test",
        server="fake",
        database="db",
        user="u",
        tool_profile="explore",
        discovery_aliases={"訂單": ["SalesOrder"]},
        db_worker_limit=2,
    )
    cfg = config_for_profile(profile, tmp_path, "fake")
    assert cfg.tool_profile == "explore"
    assert cfg.db_worker_limit == 2
    assert cfg.discovery_aliases == {"訂單": ["SalesOrder"]}


@pytest.mark.asyncio
async def test_unavailable_prompts_not_advertised(monkeypatch):
    monkeypatch.setattr(registry, "_TOOL_REGISTRY", {"get_execution_policy": object()})
    registry.register_prompts()
    assert await registry._list_prompts() == []
    with pytest.raises(ValueError, match="Unknown prompt"):
        await registry._get_prompt("safe_sql_execution", {"query": "SELECT 1"})
