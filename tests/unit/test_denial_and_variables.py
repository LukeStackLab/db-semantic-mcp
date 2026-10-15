"""Denials explain themselves; DECLARE/SET @variable + one SELECT is allowed."""

import pytest

from sqlserver_semantic_mcp.config import Config
from sqlserver_semantic_mcp.policy.intents.ast_analyzer import AstIntentAnalyzer
from sqlserver_semantic_mcp.services.policy_service import PolicyService


@pytest.fixture
def service():
    cfg = Config(_env_file=None, mssql_server="s", mssql_database="d", mssql_user="u", mssql_password="p")
    svc = PolicyService(cfg)
    svc.load()
    return svc


def test_denied_write_names_rule_flag_and_hint(service):
    result = service.validate("DELETE FROM dbo.T WHERE id = 1")
    assert result["allowed"] is False
    assert result["denial"]["rule"] in {"operation_not_allowed", "write_keyword_in_select_only"}
    assert result["denial"]["hint"]


def test_keyword_guard_denial_reports_line_and_column(service):
    result = service.validate("SELECT 1\n-- ok\nSELECT 2 DELETE dbo.T")
    assert result["allowed"] is False
    matches = result["denial"]["matches"]
    assert {"keyword": "DELETE", "line": 3, "column": 10} in matches


def test_unparseable_sql_reports_ast_rule(service):
    result = service.validate("SELECT INTO #t FROM dbo.T")
    assert result["allowed"] is False
    assert result["denial"]["layer"] in {"policy", "keyword_guard"}
    assert result["denial"]["message"]


def test_allowed_query_has_no_denial(service):
    assert "denial" not in service.validate("SELECT 1")


@pytest.mark.parametrize(
    "sql",
    [
        "DECLARE @a int = 5; SELECT * FROM dbo.T WHERE id = @a",
        "DECLARE @a int; SET @a = 5; SELECT @a",
        "DECLARE @n varchar(10) = 'x', @m int = 2; SELECT @n, @m",
    ],
)
def test_variable_preamble_with_one_select_is_allowed(service, sql):
    result = service.validate(sql)
    assert result["allowed"] is True, result
    assert result["intent"]["statement_count"] == 1
    assert result["intent"]["is_multi_statement"] is False
    assert result["intent"]["variable_statements"] >= 1


@pytest.mark.parametrize(
    "sql",
    [
        "DECLARE @t TABLE (id int); SELECT 1",
        "SET @a = (SELECT 1); SELECT 1",
        "DECLARE @a int = (SELECT COUNT(*) FROM dbo.T); SELECT @a",
        "SET NOCOUNT ON; SELECT 1",
        "DECLARE @a int = dbo.f(1); SELECT 1",
        "DECLARE @a int = 1; DELETE dbo.T WHERE id = @a",
        "DECLARE @a int = 1; SELECT 1; SELECT 2",
        "DECLARE @a int = 1",
    ],
)
def test_variable_preamble_abuse_is_refused(sql):
    assert AstIntentAnalyzer().analyze(sql).authorization_error


def test_variable_initializer_metadata_function_respects_scope():
    from sqlserver_semantic_mcp.policy.enforcer import enforce
    from sqlserver_semantic_mcp.policy.models import PolicyProfile

    intent = AstIntentAnalyzer().analyze("DECLARE @a int = OBJECT_ID('x'); SELECT 1")
    restricted = PolicyProfile(profile_name="r", scope={"denied_tables": ["dbo.Secret"]})
    result = enforce(intent, restricted, "d")
    assert result.allowed is False and result.rule == "metadata_function_scope"


def test_declare_batch_executes_as_one_statement(service, monkeypatch):
    from unittest.mock import MagicMock

    from sqlserver_semantic_mcp.services.query_service import QueryService

    opened = MagicMock()
    cursor = opened.return_value.__enter__.return_value.cursor.return_value
    cursor.description = [("id",)]
    cursor.fetchmany.return_value = [(1,)]
    monkeypatch.setattr("sqlserver_semantic_mcp.services.query_service.open_connection", opened)
    sql = "DECLARE @a int = 1; SELECT id FROM dbo.T WHERE id = @a"
    result = QueryService(service, service._cfg).execute_query(sql)
    assert result["executed"] is True and result["rows"] == [[1]]
    cursor.execute.assert_called_once_with(sql)
