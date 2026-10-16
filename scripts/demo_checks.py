"""Read-only real queries and MCP child checks for the owned demo."""

import asyncio
import importlib.util
import os
import tempfile
from decimal import Decimal

from demo_support import ROOT, RUNTIME, DemoError, environment


def smoke_module():
    spec = importlib.util.spec_from_file_location("demo_installed_smoke", ROOT / "tests/integration/check_installed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def mcp_check(path, queries, remote):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from db_semantic_mcp.setup.clients import server_entry

    entry = server_entry(path)
    entry["env"] = smoke_module().child_environment(path)
    with tempfile.TemporaryFile(mode="w+") as errors:
        async with asyncio.timeout(180):
            async with stdio_client(StdioServerParameters(**entry), errlog=errors) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    assert len((await session.list_tools()).tools) == 12
                    for query in queries:
                        result = await session.call_tool("plan_or_execute_query", {"query": query})
                        assert not result.isError and result.structuredContent["data"]["executed"]
                    for source in ("local", *(["remote"] if remote else [])):
                        result = await session.call_tool("get_tables", {"keyword": "Orders", "source": source})
                        assert not result.isError and result.structuredContent["data"]
                    if remote:
                        result = await session.call_tool(
                            "describe_table", {"schema": "PUBLIC", "table": "DBMCP_PUBLIC_REMOTE_ONLY"}
                        )
                        assert not result.isError and result.structuredContent["canonical_source"] == "remote"


def check(state):
    from db_semantic_mcp.infrastructure.cache.structural import fetch_snapshot_from_server
    from db_semantic_mcp.profiles import load_profile
    from db_semantic_mcp.services.policy_service import PolicyService
    from db_semantic_mcp.services.query_service import QueryService
    from db_semantic_mcp.setup.checks import check_connection

    os.environ.update(environment())
    for name in ("sql", "oracle", *(["oracle-remote"] if state["remote"] else [])):
        path = RUNTIME / name / "connection.json"
        if not path.is_file():
            raise DemoError("Initialize the demo before running check")
        cfg = load_profile(path)
        check_connection(cfg)
        oracle = name.startswith("oracle")
        queries = [
            "SELECT 1 AS N FROM DUAL" if oracle else "SELECT 1 AS N",
            "SELECT AMOUNT FROM DBMCP_TEST.DBMCP_CHAIN ORDER BY ORDER_ID"
            if oracle
            else "SELECT Amount FROM dbo.Orders WHERE TenantId=1 ORDER BY OrderId",
        ]
        if name == "oracle-remote":
            queries += [
                "SELECT AMOUNT FROM DBMCP_TEST.DBMCP_ORDERS@DBMCP.REMOTE.TEST",
                "SELECT AMOUNT FROM DBMCP_TEST.DBMCP_REMOTE_CHAIN",
            ]
        query_service = QueryService(PolicyService(cfg), cfg)
        result = query_service.execute_query(queries[1], validate_result_schema=True)
        assert result.get("executed") and result["rows"][0][0] == Decimal("123.4500")
        assert isinstance(result["rows"][0][0], Decimal)
        snapshot = fetch_snapshot_from_server(cfg)
        table = "DBMCP_ORDERS" if oracle else "Orders"
        fk = [row for row in snapshot.foreign_keys if row[1] == table]
        assert {row[8] for row in fk} >= {1, 2}
        if not oracle:
            large = query_service.execute_query("SELECT Amount FROM dbo.Orders WHERE TenantId=2")
            assert large["executed"] and large["rows"][0][0] == Decimal("987654321012345.6789")
        if oracle:
            lob = query_service.execute_query("SELECT NOTE FROM DBMCP_TEST.DBMCP_ORDERS WHERE ORDER_ID=1")
            assert lob["executed"] and lob["rows"][0][0].endswith("...[truncated]")
            if name == "oracle" and state["remote"]:
                denied = query_service.execute_query("SELECT AMOUNT FROM DBMCP_TEST.DBMCP_ORDERS@DBMCP.REMOTE.TEST")
                assert not denied.get("executed")
        asyncio.run(mcp_check(path, queries, name == "oracle-remote"))
        print(f"{name}: doctor, precise Decimal, compound FK, actual MCP 12 tools and queries passed")
