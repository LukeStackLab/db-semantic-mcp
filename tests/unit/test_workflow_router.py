"""Unit tests for the workflow router."""

import pytest

from sqlserver_semantic_mcp.config import reset_config
from sqlserver_semantic_mcp.services.policy_service import PolicyService
from sqlserver_semantic_mcp.workflows.router import route_query


@pytest.fixture
def policy(monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_DATABASE", "x")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_PASSWORD", "p")
    reset_config()
    svc = PolicyService()
    svc.load()
    return svc


def test_empty_query_routes_to_policy_only(policy):
    dec = route_query("", policy=policy)
    assert dec.route == "policy_only"


def test_natural_language_routes_to_discovery(policy):
    dec = route_query("list all customers who bought stuff", policy=policy)
    assert dec.route == "discovery"
    assert "discover_relevant_tables" in dec.recommended_tools


def test_allowed_select_routes_to_direct_execute(policy):
    dec = route_query("SELECT TOP 5 * FROM dbo.Users", policy=policy)
    assert dec.route == "direct_execute"
    assert "plan_or_execute_query" in dec.recommended_tools


def test_disallowed_drop_routes_to_direct_validate(policy):
    dec = route_query("DROP TABLE dbo.Users", policy=policy)
    assert dec.route == "direct_validate"
    assert "plan_or_execute_query" in dec.recommended_tools


@pytest.mark.parametrize(
    "sql, route",
    [
        ("SELECT TOP 5 * FROM dbo.Users", "direct_execute"),
        ("DROP TABLE dbo.Users", "direct_validate"),
        ("DECLARE @id int = 1; SELECT @id", "direct_execute"),
    ],
)
def test_route_query_parses_ast_exactly_once(policy, monkeypatch, sql, route):
    from sqlserver_semantic_mcp.policy.intents.ast_analyzer import _StrictTSQL

    call_count = 0
    original_parse = _StrictTSQL.parse

    def counted_parse(self, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        return original_parse(self, *args, **kwargs)

    monkeypatch.setattr(_StrictTSQL, "parse", counted_parse)
    assert route_query(sql, policy=policy).route == route
    assert call_count == 1


def test_regex_routing_still_authorizes_with_ast():
    from sqlserver_semantic_mcp.config import Config
    from sqlserver_semantic_mcp.policy.models import PolicyProfile

    cfg = Config(_env_file=None, mssql_server="x", mssql_database="x", intent_analyzer="regex")
    service = PolicyService(cfg)
    service._policy = PolicyProfile(profile_name="scoped", scope={"denied_tables": ["dbo.Secret"]})
    sql = "SELECT dbo.Secret(1) FROM dbo.Public"
    assert "dbo.Secret" not in service.analyze(sql).affected_tables
    decision = route_query(sql, policy=service)
    assert decision.route == "direct_validate"
    assert decision.reason == "User-defined function scope cannot be verified"
