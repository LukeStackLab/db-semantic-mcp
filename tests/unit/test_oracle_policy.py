import pytest
from pydantic import ValidationError

from db_semantic_mcp.config import Config
from db_semantic_mcp.policy.intents.oracle_analyzer import OracleIntentAnalyzer, mask_oracle_literals, parse_oracle
from db_semantic_mcp.policy.intents.router import get_analyzer
from db_semantic_mcp.policy.models import PolicyProfile, PolicyScope
from db_semantic_mcp.policy.scope import oracle_object_allowed
from db_semantic_mcp.services.policy_service import PolicyService


def config(**kw):
    return Config.from_fixed(database_type="oracle", server="host", oracle={"service_name": "db"}, **kw)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders",
        "SELECT COUNT(*), SUM(amount), NVL(MAX(amount),0) FROM orders",
        "SELECT q'[it's; DELETE -- @LINK]' FROM dual",
        "SELECT nq'{日本語}' FROM dual",
        'SELECT * FROM "erp"."Odd.Table"@SALES.EXAMPLE.COM o',
        "SELECT * FROM erp.orders@SALES o JOIN crm.customer@CRM.EXAMPLE c ON o.id=c.id",
        "WITH a AS (SELECT * FROM erp.orders), b AS (SELECT * FROM a) SELECT * FROM b",
        "SELECT * FROM orders WHERE id IN (SELECT id FROM customers)",
        "SELECT ROW_NUMBER() OVER (ORDER BY id) FROM orders",
        "SELECT * FROM orders FETCH FIRST 5 ROWS ONLY",
        "UPDATE orders SET amount=123.45 WHERE id=100",
        "INSERT INTO orders(id, name) VALUES (123, q'[a\"b]')",
        "UPDATE orders@SALES.EXAMPLE SET amount=2 WHERE id=1",
    ],
)
def test_native_oracle_supported(sql):
    intent = OracleIntentAnalyzer().analyze(sql)
    assert not intent.authorization_error


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT seq.NEXTVAL FROM dual",
        "SELECT seq.CURRVAL FROM dual",
        'SELECT "S"."NEXTVAL" FROM dual',
        "SELECT * FROM orders FOR UPDATE",
        "SELECT UTL_HTTP.REQUEST('url') FROM dual",
        "SELECT custom_function(1) FROM dual",
        "SELECT custom_function@LINK(1) FROM dual",
        "SELECT SYS_CONTEXT('a','b') FROM dual",
        "SELECT DBMS_RANDOM.VALUE FROM dual",
        "BEGIN UPDATE orders SET amount=1; END;",
        "UPDATE orders SET amount=1 WHERE id=1 RETURNING amount INTO :v",
        "INSERT INTO orders SELECT * FROM customers",
        "UPDATE orders SET amount=(SELECT MAX(amount) FROM other) WHERE id=1",
        "UPDATE orders SET amount=1",
        "DELETE FROM orders WHERE id=1",
        "SELECT * FROM TABLE(custom_function())",
        "SELECT * FROM XMLTABLE('/a')",
        "SELECT * FROM orders AS OF TIMESTAMP SYSDATE",
        "SELECT * FROM orders; SELECT * FROM forbidden",
        "SELECT q'[unclosed' FROM dual",
        "SELECT /* unclosed",
        "SELECT /*+ INDEX(orders) */ * FROM orders",
        "SELECT * INTO target FROM orders",
        "CREATE TABLE target(id NUMBER)",
        "WITH FUNCTION f RETURN NUMBER IS BEGIN RETURN 1; END; SELECT f FROM dual",
        "SELECT * FROM db.erp.orders",
        "SELECT [secret] FROM orders",
        "SELECT * FROM orders@LINK..EVIL",
        'SELECT * FROM orders@"LINK"',
        "SELECT 1 FROM dual CONNECT BY LEVEL<=10",
    ],
)
def test_oracle_rejects_unproven_sql(sql):
    assert OracleIntentAnalyzer().analyze(sql).authorization_error


def test_remote_and_nested_lexical_bindings():
    _, refs = parse_oracle('WITH c AS (SELECT * FROM ERP.ORDERS@FULL.LINK) SELECT * FROM c JOIN "c" ON 1=1')
    assert [(r.schema, r.name, r.db_link) for r in refs] == [("ERP", "ORDERS", "FULL.LINK"), ("", "c", "")]
    _, refs = parse_oracle("SELECT * FROM (WITH x AS (SELECT * FROM a) SELECT * FROM x) y JOIN x ON 1=1")
    assert {r.name for r in refs} == {"A", "X"}


def test_quoted_names_and_q_keyword_guard():
    scope = PolicyScope(allowed_schemas=["erp"], allowed_tables=['erp."Odd.Table"'])
    assert oracle_object_allowed(scope, "db", "ERP", "Odd.Table")
    assert not oracle_object_allowed(scope, "db", "erp", "Odd.Table")
    assert not oracle_object_allowed(scope, "db", "ERP", "ODD.TABLE")
    scope.denied_tables = ['erp."Odd.Table"']
    assert not oracle_object_allowed(scope, "db", "ERP", "Odd.Table")
    assert "DELETE" not in mask_oracle_literals("SELECT q'[it's DELETE -- ]' FROM dual")


def test_provider_selected_and_no_extra_scope_fields():
    assert isinstance(get_analyzer(config(intent_analyzer="regex")), OracleIntentAnalyzer)
    with pytest.raises(ValidationError):
        PolicyScope(sources={"remote": {"allowed_table": ["orders"]}})


def test_invalid_sql_never_connects(monkeypatch):
    def no_connect(*args, **kwargs):
        raise AssertionError("must not connect")

    monkeypatch.setattr("db_semantic_mcp.infrastructure.connection.open_connection", no_connect)
    service = PolicyService(config())
    service._policy = PolicyProfile(profile_name="readonly")
    result = service.validate("CREATE TABLE o(id NUMBER)")
    assert not result["allowed"]
    assert "T-SQL" not in result["denial"]["hint"]


def test_global_write_permission_cannot_be_expanded_by_remote():
    service = PolicyService(config(sources={"remote": {"db_link": "R", "readonly": False}}))
    service._policy = PolicyProfile(
        profile_name="readonly", scope={"sources": {"remote": {"operations": {"update": True}}}}
    )
    result = service.validate("UPDATE orders@R SET a=1 WHERE id=1", resolve_sources=False)
    assert not result["allowed"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT DATE_ADD(id,1) FROM orders",
        "SELECT CONCAT_WS(id,1) FROM orders",
        "SELECT SUBSTRING(id,1) FROM orders",
        "SELECT IF(id,1,2) FROM orders",
        'SELECT "TO_CHAR"(id) FROM orders',
        "SELECT CAST(id AS custom_type) FROM orders",
        "WITH date_add(x) AS (SELECT id FROM orders) SELECT DATE_ADD(id,1) FROM orders",
    ],
)
def test_other_dialects_builtin_names_do_not_authorize_oracle_user_functions(sql):
    assert OracleIntentAnalyzer().analyze(sql).authorization_error


@pytest.mark.parametrize(
    "clause",
    [
        "FETCH FIRST evil ROWS ONLY",
        'FETCH FIRST "evil" ROWS ONLY',
        "FETCH FIRST evil PERCENT ROWS ONLY",
        'FETCH FIRST "evil" PERCENT ROWS ONLY',
        "OFFSET evil ROWS",
        'OFFSET "evil" ROWS FETCH NEXT 5 ROWS ONLY',
        "FETCH FIRST '5' ROWS ONLY",
        "FETCH FIRST (1+1) ROWS ONLY",
        "OFFSET 1+1 ROWS",
        "OFFSET :count ROWS",
    ],
)
def test_row_limiting_unknown_or_expression_counts_are_rejected(clause):
    assert OracleIntentAnalyzer().analyze(f"SELECT * FROM orders {clause}").authorization_error


@pytest.mark.parametrize(
    "clause",
    [
        "FETCH FIRST 5 ROWS ONLY",
        "FETCH FIRST ROW ONLY",
        "FETCH NEXT 2.5 PERCENT ROWS ONLY",
        "OFFSET 0 ROWS",
        "OFFSET 3 ROWS FETCH NEXT 2 ROWS ONLY",
        "OFFSET 1.5 ROWS FETCH FIRST 10 PERCENT ROWS WITH TIES",
    ],
)
def test_row_limiting_numeric_literals_and_default_fetch_remain_supported(clause):
    assert not OracleIntentAnalyzer().analyze(f"SELECT * FROM orders ORDER BY id {clause}").authorization_error
