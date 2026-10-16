"""Portable product checks for fixtures/oracle; run each driver mode in a fresh process."""

import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from db_semantic_mcp.infrastructure.backends.oracle import OracleBackend
from db_semantic_mcp.infrastructure.cache.structural import fetch_snapshot_from_server
from db_semantic_mcp.services.policy_service import PolicyService
from db_semantic_mcp.services.query_service import QueryService

pytestmark = pytest.mark.integration


def query(cfg, sql):
    result = QueryService(PolicyService(cfg), cfg).execute_query(sql, validate_result_schema=True)
    assert result.get("executed"), result
    return result


def test_native_precision_lob_and_metadata(oracle_config):
    result = query(
        oracle_config, "SELECT ORDER_ID, AMOUNT, NOTE, PAYLOAD FROM DBMCP_TEST.DBMCP_ORDERS ORDER BY ORDER_ID"
    )
    assert isinstance(result["rows"][0][1], Decimal)
    assert str(result["rows"][0][2]).endswith("...[truncated]")
    assert len(str(result["rows"][0][2])) == 64 + len("...[truncated]")
    assert result["result_schema"][1]["precision"] == 19
    assert result["result_schema"][1]["scale"] == 4


def test_catalog_constraints_and_comments(oracle_config):
    snapshot = fetch_snapshot_from_server(oracle_config)
    assert any(row[0] == "DBMCP_TEST" and row[1] == "DBMCP_ORDERS" for row in snapshot.columns)
    fk = [row for row in snapshot.foreign_keys if row[1] == "DBMCP_ORDERS"]
    assert len(fk) >= 2
    assert {row[8] for row in fk} >= {1, 2}
    assert all(len(row) == 12 for row in snapshot.columns)
    assert ("DBMCP_TEST", "DBMCP_ORDERS", "", "測試訂單") in snapshot.comments
    assert ("DBMCP_TEST", "DBMCP_ORDERS", "AMOUNT", "精確金額") in snapshot.comments


def test_synonym_and_native_remote_multisource(oracle_config):
    query(oracle_config, "SELECT AMOUNT FROM DBMCP_TEST.DBMCP_CHAIN")
    if "remote" not in oracle_config.sources:
        pytest.skip("Configured disposable remote source required")
    link = oracle_config.sources["remote"].db_link
    query(
        oracle_config,
        f"SELECT l.ORDER_ID,r.AMOUNT FROM DBMCP_TEST.DBMCP_ORDERS l JOIN DBMCP_TEST.DBMCP_ORDERS@{link} r ON l.ORDER_ID=r.ORDER_ID",
    )
    query(oracle_config, "SELECT AMOUNT FROM DBMCP_TEST.DBMCP_REMOTE_CHAIN")


def test_temporal_contract(oracle_config):
    result = QueryService(PolicyService(oracle_config), oracle_config).execute_query(
        "SELECT CREATED_AT FROM DBMCP_TEST.DBMCP_ORDERS WHERE ORDER_ID=1"
    )
    if oracle_config.oracle.driver_mode == "thin":
        assert not result.get("executed") and "TO_CHAR" in str(result)
    else:
        assert result.get("executed") and isinstance(result["rows"][0][0], str)
    assert OracleBackend().capabilities(oracle_config)["native_timestamp_timezone"] is (
        oracle_config.oracle.driver_mode == "thick"
    )


@pytest.mark.asyncio
async def test_actual_mcp_tools_and_sources(oracle_config):
    # Use the original fixed profile for the child; do not serialize credentials.
    from db_semantic_mcp.setup.clients import server_entry

    entry = server_entry(Path(os.environ["SEMANTIC_MCP_TEST_ORACLE_PROFILE"]))
    entry["env"] = {k: v for k, v in os.environ.items() if not k.startswith("SEMANTIC_MCP_")}
    async with asyncio.timeout(180):
        async with stdio_client(StdioServerParameters(**entry)) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools) == 12
                for source in ["local", *oracle_config.sources]:
                    response = await session.call_tool("get_tables", {"source": source, "keyword": "ORDERS"})
                    assert not response.isError
                    assert response.structuredContent["source"]["id"] == source
                    assert response.structuredContent["data"]
                response = await session.call_tool(
                    "plan_or_execute_query", {"query": "SELECT AMOUNT FROM DBMCP_TEST.DBMCP_CHAIN"}
                )
                assert not response.isError and response.structuredContent["data"]["executed"]
                response = await session.call_tool(
                    "describe_object", {"schema": "DBMCP_TEST", "name": "DBMCP_PKG", "type": "PACKAGE"}
                )
                assert not response.isError
                assert "unknown" in json.dumps(response.structuredContent).lower()
                if "remote" in oracle_config.sources:
                    link = oracle_config.sources["remote"].db_link
                    remote_query = await session.call_tool(
                        "plan_or_execute_query", {"query": f"SELECT AMOUNT FROM DBMCP_TEST.DBMCP_ORDERS@{link}"}
                    )
                    assert not remote_query.isError and remote_query.structuredContent["data"]["executed"]
                if os.getenv("SEMANTIC_MCP_ORACLE_PUBLIC_FIXTURE") == "1":
                    public = await session.call_tool(
                        "describe_table", {"schema": "PUBLIC", "table": "DBMCP_PUBLIC_REMOTE_ONLY"}
                    )
                    assert not public.isError
                    assert public.structuredContent["canonical_source"] == "remote"
                    assert public.structuredContent["target"]["schema"] == "DBMCP_REMOTE_ONLY"
                response = await session.call_tool("get_execution_policy", {})
                assert "query_timeout_semantics" in response.structuredContent["capabilities"]


@pytest.mark.parametrize("remote", [False, True], ids=["local", "db-link"])
def test_write_cap_rolls_back(oracle_config, remote):
    if os.getenv("SEMANTIC_MCP_DISPOSABLE_ORACLE_WRITES") != "1":
        pytest.skip("Separate disposable write opt-in required")
    if remote and "remote" not in oracle_config.sources:
        pytest.skip("Configured disposable remote source required")
    if oracle_config.policy_profile != "read_write_safe":
        pytest.fail("Write tests require an explicitly writable disposable profile/policy")
    table = "DBMCP_TEST.DBMCP_ORDERS"
    if remote:
        table += "@" + oracle_config.sources["remote"].db_link
    oracle_config.max_rows_affected = 1
    before = query(oracle_config, f"SELECT ORDER_ID, AMOUNT FROM {table} ORDER BY ORDER_ID")["rows"]
    result = QueryService(PolicyService(oracle_config), oracle_config).execute_query(
        f"UPDATE {table} SET AMOUNT=AMOUNT+1 WHERE ORDER_ID>0"
    )
    assert result["transaction_state"] == "rolled_back", result
    assert not result.get("automatic_retry_allowed", False)
    assert query(oracle_config, f"SELECT ORDER_ID, AMOUNT FROM {table} ORDER BY ORDER_ID")["rows"] == before


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("TO_TIMESTAMP('2026-10-15 12:34:56.123456789','YYYY-MM-DD HH24:MI:SS.FF9')", "2026-10-15T12:34:56.123456789"),
        (
            "TO_TIMESTAMP_TZ('2026-10-15 12:34:56.123456789 +08:00','YYYY-MM-DD HH24:MI:SS.FF9 TZH:TZM')",
            "2026-10-15T12:34:56.123456789 +08:00",
        ),
        (
            "TO_TIMESTAMP_TZ('2026-10-15 12:34:56.123456789 Asia/Taipei','YYYY-MM-DD HH24:MI:SS.FF9 TZR')",
            "2026-10-15T12:34:56.123456789 ASIA/TAIPEI CST",
        ),
        (
            "CAST(TO_TIMESTAMP_TZ('2026-10-15 12:34:56.123456789 +08:00','YYYY-MM-DD HH24:MI:SS.FF9 TZH:TZM') AS TIMESTAMP(9) WITH LOCAL TIME ZONE)",
            "2026-10-15T04:34:56.123456789Z",
        ),
    ],
    ids=["ff9", "numeric-tz", "region-tz", "ltz-utc"],
)
def test_native_temporal_precision_matrix(oracle_config, expression, expected):
    result = QueryService(PolicyService(oracle_config), oracle_config).execute_query(
        f"SELECT {expression} AS T FROM DUAL", validate_result_schema=True
    )
    if oracle_config.oracle.driver_mode == "thin":
        assert not result.get("executed") and "TO_CHAR" in str(result)
    else:
        assert result.get("executed"), result
        assert result["rows"][0][0].upper() == expected
