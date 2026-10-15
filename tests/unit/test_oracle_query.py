from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from db_semantic_mcp.infrastructure.backends.oracle import OracleBackend
from db_semantic_mcp.policy.models import PolicyProfile
from db_semantic_mcp.services.policy_service import PolicyService
from db_semantic_mcp.services.query_service import QueryService
from tests.unit.test_oracle_policy import config
from tests.unit.test_oracle_resolution import Catalog


def service(monkeypatch, *, scope=None, cap=100):
    fake = Catalog(monkeypatch)
    cfg = config(sources={"sales": {"db_link": "SALES", "readonly": False}}, max_rows_affected=cap)
    policy = PolicyService(cfg)
    policy._policy = PolicyProfile(
        profile_name="rw",
        operations={"select": True, "update": True, "insert": True},
        constraints={"max_rows_affected": cap},
        scope=scope or {},
    )
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.description = [("AMOUNT", "NUMBER", None, None, 19, 4, True)]
    cursor.fetchmany.side_effect = [[(Decimal("123.4500"),)]]
    cursor.rowcount = 1

    @contextmanager
    def connect(*args):
        yield conn

    monkeypatch.setattr("db_semantic_mcp.services.query_service.open_connection", connect)
    monkeypatch.setattr("db_semantic_mcp.infrastructure.connection.open_connection", connect)
    return fake, conn, cursor, policy, QueryService(policy, cfg)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT evil FROM dual",
        "SELECT ERP.EVIL FROM dual",
        "SELECT ORDERS.EVIL FROM orders",
        "SELECT id FROM orders WHERE evil=1",
        "SELECT id FROM orders ORDER BY evil",
        "WITH c AS (SELECT evil FROM orders) SELECT * FROM c",
        "SELECT * FROM (SELECT evil FROM dual)",
        "UPDATE orders SET amount=evil WHERE id=1",
        "INSERT INTO orders(id) VALUES(evil)",
        "INSERT INTO orders(unknown) VALUES(1)",
    ],
)
def test_bare_function_and_unknown_column_denied_before_driver(monkeypatch, sql):
    _, _, cursor, _, query = service(monkeypatch)
    result = query.execute_query(sql, validate_result_schema=True)
    assert not result["executed"]
    cursor.execute.assert_not_called()
    cursor.parse.assert_not_called()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM orders",
        "SELECT o.id FROM orders o",
        "SELECT id AS a FROM orders ORDER BY a",
        "WITH c AS(SELECT id FROM orders) SELECT id FROM c",
        "SELECT x.id FROM (SELECT id FROM orders) x",
        "SELECT COUNT(*) FROM orders",
        "SELECT * FROM orders",
        "SELECT q'[DELETE it's @LINK]' FROM dual",
        "SELECT 123.45 FROM dual",
        "SELECT orders.id FROM ERP.orders@SALES.EXAMPLE",
    ],
)
def test_live_column_evidence_preserves_common_queries_and_original_sql(monkeypatch, sql):
    _, conn, cursor, _, query = service(monkeypatch, scope={"sources": {"sales": {}}})
    result = query.execute_query(sql, validate_result_schema=True)
    assert result["executed"], result
    assert result["rows"] == [[Decimal("123.4500")]]
    cursor.execute.assert_called_once_with(sql)
    cursor.parse.assert_called_once_with(sql)
    assert conn.autocommit is False


def test_remote_unqualified_object_is_not_proven_by_remote_principal(monkeypatch):
    _, _, cursor, _, query = service(monkeypatch, scope={"sources": {"sales": {}}})
    assert not query.execute_query("SELECT * FROM orders@SALES")["executed"]
    cursor.execute.assert_not_called()


def test_validate_then_retarget_reauthorized_on_execution_session(monkeypatch):
    fake, _, cursor, policy, query = service(monkeypatch, scope={"allowed_tables": ["ERP.A", "ERP.ORDERS"]})
    fake.synonyms["local", "ERP", "A"] = ("ERP", "ORDERS", None)
    assert policy.validate("SELECT * FROM A")["allowed"]
    fake.synonyms["local", "ERP", "A"] = ("ERP", "CUSTOMERS", None)
    assert not query.execute_query("SELECT * FROM A")["executed"]
    cursor.execute.assert_not_called()


def test_each_source_of_join_requires_permission(monkeypatch):
    _, _, cursor, _, query = service(monkeypatch)
    assert not query.execute_query("SELECT * FROM ERP.ORDERS a JOIN ERP.ORDERS@SALES b ON a.id=b.id")["executed"]
    cursor.execute.assert_not_called()


def test_remote_cap_is_always_strict(monkeypatch):
    _, conn, cursor, _, query = service(
        monkeypatch, scope={"sources": {"sales": {"operations": {"update": True}}}}, cap=1
    )
    cursor.rowcount = 2
    result = query.execute_query("UPDATE ERP.ORDERS@SALES SET amount=2 WHERE id>0", affected_rows_policy="report")
    assert result["transaction_state"] == "rolled_back"
    assert result["failure_category"] == "affected_rows_cap"
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


@pytest.mark.parametrize("failure", ["execute", "commit", "rollback"])
def test_remote_ambiguous_write_never_auto_retries(monkeypatch, failure):
    _, conn, cursor, _, query = service(
        monkeypatch, scope={"sources": {"sales": {"operations": {"update": True}}}}, cap=1
    )
    if failure == "execute":
        cursor.execute.side_effect = RuntimeError("secret wallet details")
    elif failure == "commit":
        conn.commit.side_effect = RuntimeError("secret wallet details")
    else:
        cursor.rowcount = 2
        conn.rollback.side_effect = RuntimeError("secret wallet details")
    result = query.execute_query("UPDATE ERP.ORDERS@SALES SET amount=2 WHERE id>0")
    assert result["transaction_state"] == "unknown"
    assert result["automatic_retry_allowed"] is False
    assert "secret" not in str(result)
    assert "wallet details" not in str(result)


def test_preflight_never_parses_ddl_or_write():
    cursor = MagicMock()
    for sql in [
        "CREATE TABLE evil(id NUMBER)",
        "UPDATE orders SET amount=1 WHERE id=1",
        "SELECT * FROM orders FOR UPDATE",
    ]:
        with pytest.raises(ValueError):
            OracleBackend().describe_result_schema(cursor, sql)
    cursor.parse.assert_not_called()


def test_oversized_column_metadata_is_bounded_even_with_zero_rows(monkeypatch):
    _, _, cursor, _, query = service(monkeypatch)
    query._cfg.max_response_bytes = 1024
    cursor.description = [("c" * 80 + str(i),) for i in range(30)]
    cursor.fetchmany.side_effect = [[]]
    result = query.execute_query("SELECT * FROM orders")
    assert result["executed"]
    assert result["columns"] == []
    assert result["rows"] == []
    assert result["columns_omitted"] is True
    assert result["truncated"] and not result["result_complete"]
    assert result["truncation_reason"] == "column_metadata_limit"
    cursor.fetchmany.assert_not_called()


@pytest.mark.parametrize("value", ["a" * 6000, b"a" * 4000])
def test_lob_prefix_does_not_claim_exact_total_length(monkeypatch, value):
    _, _, cursor, _, query = service(monkeypatch)

    class LOB:
        def size(self):
            return len(value)

        def read(self, *, offset, amount):
            assert amount <= 2 * (query._cfg.max_cell_chars + 1)
            return value[offset - 1 : offset - 1 + amount]

    cursor.fetchmany.side_effect = [[(LOB(),)]]
    result = query.execute_query("SELECT * FROM orders")
    shown = result["rows"][0][0]
    assert "truncated" in shown
    assert "total" not in shown


def test_preflight_metadata_cannot_bypass_response_cap(monkeypatch):
    _, _, cursor, _, query = service(monkeypatch)
    query._cfg.max_response_bytes = 1024
    cursor.description = [("c" * 80 + str(i), "NUMBER", None, None, 19, 4, True) for i in range(30)]
    result = query.execute_query("SELECT * FROM orders", validate_result_schema=True)
    assert result["result_schema_omitted"] is True
    assert "result_schema" not in result
    assert result["columns"] == []
    assert not result["result_complete"]


def test_thin_timestamp_failure_provides_actionable_hint_without_rows(monkeypatch):
    from db_semantic_mcp.infrastructure.backends.oracle import OracleResultTypeError

    _, _, cursor, _, query = service(monkeypatch)
    cursor.execute.side_effect = OracleResultTypeError()
    result = query.execute_query("SELECT * FROM orders")
    assert not result["executed"]
    assert "TO_CHAR" in result["error"] and "FF9 TZR TZD" in result["error"]
    assert "rows" not in result
    assert not result.get("result_complete")


@pytest.mark.parametrize(
    "clause", ["FETCH FIRST evil ROWS ONLY", "FETCH FIRST evil PERCENT ROWS ONLY", "OFFSET evil ROWS"]
)
def test_unproven_row_limiting_count_is_denied_before_driver(monkeypatch, clause):
    _, _, cursor, _, query = service(monkeypatch)
    result = query.execute_query(f"SELECT * FROM orders {clause}", validate_result_schema=True)
    assert not result["executed"]
    cursor.execute.assert_not_called()
    cursor.parse.assert_not_called()
