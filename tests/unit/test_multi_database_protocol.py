"""Actual stdio MCP transport; no SQL Server is contacted by this fixture."""

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def test_stdio_database_discovery_schema_and_routing():
    script = """
import asyncio
from mcp.server.stdio import stdio_server
from mcp.types import Tool
from db_semantic_mcp.config import Config, configure, get_config
from db_semantic_mcp.policy.models import PolicyProfile
from db_semantic_mcp.server import app as server
from db_semantic_mcp.server import database_context
from db_semantic_mcp.server.tools import register_all
from db_semantic_mcp.server import resources
cfg=Config.from_fixed(server="fixture", database="ERP", databases=["ERP","CRM"], profile_version=3, cache_enabled=False, metrics_enabled=False)
cfg._policy_snapshot=PolicyProfile(profile_name="readonly", scope={"allowed_databases":["ERP","CRM"],"database_scopes":{"ERP":{},"CRM":{}}})
configure(cfg)
async def initialize(ctx): return ctx
database_context._initialize=initialize
register_all()
async def identity(args): return {"selected":get_config().database}
server.register_tool(Tool(name="identity",inputSchema={"type":"object"}), identity)
async def run():
    async with stdio_server() as (r,w):
        await server.app.run(r,w,server.app.create_initialization_options())
asyncio.run(run())
"""
    params = StdioServerParameters(command=sys.executable, args=["-I", "-c", script])
    async with asyncio.timeout(15), stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        by_name = {tool.name: tool for tool in tools}
        assert "list_databases" in by_name
        assert "database" in by_name["get_tables"].inputSchema["required"]
        assert "query" in by_name["plan_or_execute_query"].inputSchema["required"]
        choices = await session.call_tool("list_databases", {})
        assert choices.structuredContent == {"databases": ["ERP", "CRM"], "selection_required": True}
        assert not choices.isError
        bad = await session.call_tool("identity", {})
        assert bad.isError
        wrong = await session.call_tool("identity", {"database": "Forbidden"})
        assert wrong.isError
        first, second = await asyncio.gather(
            session.call_tool("identity", {"database": "ERP"}),
            session.call_tool("identity", {"database": "CRM"}),
        )
        assert first.structuredContent["selected"] == "ERP"
        assert second.structuredContent["selected"] == "CRM"
        for result in (first, second):
            assert result.structuredContent == json.loads(result.content[0].text)
            assert not result.isError
        resources = (await session.list_resources()).resources
        assert {str(resource.uri) for resource in resources} == {
            "semantic://summary/database?database=ERP",
            "semantic://summary/database?database=CRM",
        }
