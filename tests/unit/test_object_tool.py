from unittest.mock import AsyncMock, patch

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
async def test_describe_object_view_default_omits_definition_body(env):
    from sqlserver_semantic_mcp.server.tools import object_tool

    fake = {
        "schema": "dbo",
        "object_name": "vw_x",
        "object_type": "VIEW",
        "definition": "CREATE VIEW vw_x AS SELECT 1",
        "dependencies": ["dbo.Users"],
        "status": "ready",
    }
    with patch.object(object_tool.object_service, "describe_object", new=AsyncMock(return_value=fake)):
        result = await object_tool._describe_object_tool({"schema": "dbo", "name": "vw_x", "type": "VIEW"})

    assert "definition" not in result
    assert len(result["definition_hash"]) == 8
    assert result["definition_bytes"] == len(b"CREATE VIEW vw_x AS SELECT 1")
    assert result["object"] == "dbo.vw_x"
    assert result["type"] == "VIEW"


@pytest.mark.asyncio
async def test_describe_object_include_definition_returns_full(env):
    from sqlserver_semantic_mcp.server.tools import object_tool

    fake = {
        "schema": "dbo",
        "object_name": "vw_x",
        "object_type": "VIEW",
        "definition": "CREATE VIEW vw_x AS SELECT 1",
        "dependencies": [],
        "status": "ready",
    }
    with patch.object(object_tool.object_service, "describe_object", new=AsyncMock(return_value=fake)):
        result = await object_tool._describe_object_tool(
            {"schema": "dbo", "name": "vw_x", "type": "VIEW", "include_definition": True}
        )

    # include_definition adds the body; hash and bytes are always present.
    assert result["definition"] == "CREATE VIEW vw_x AS SELECT 1"
    assert len(result["definition_hash"]) == 8
    assert result["definition_bytes"] == len(b"CREATE VIEW vw_x AS SELECT 1")


@pytest.mark.asyncio
async def test_describe_object_returns_reads_writes_and_definition_on_request(env):
    from sqlserver_semantic_mcp.server.tools import object_tool

    fake = {
        "schema": "dbo",
        "object_name": "vw_x",
        "object_type": "VIEW",
        "definition": "CREATE VIEW vw_x AS SELECT 1",
        "dependencies": [],
        "status": "ready",
    }
    with patch.object(object_tool.object_service, "describe_object", new=AsyncMock(return_value=fake)):
        result = await object_tool._describe_object_tool(
            {"schema": "dbo", "name": "vw_x", "type": "VIEW", "include_definition": True}
        )

    assert result["definition"] == "CREATE VIEW vw_x AS SELECT 1"
    assert len(result["definition_hash"]) == 8
    assert result["affected_tables"] == []


@pytest.mark.asyncio
async def test_describe_object_procedure_default_omits_definition_body(env):
    from sqlserver_semantic_mcp.server.tools import object_tool

    fake = {
        "schema": "dbo",
        "object_name": "usp_x",
        "object_type": "PROCEDURE",
        "definition": "CREATE PROCEDURE usp_x AS SELECT 1",
        "dependencies": [],
        "status": "ready",
    }
    with patch.object(object_tool.object_service, "describe_object", new=AsyncMock(return_value=fake)):
        result = await object_tool._describe_object_tool({"schema": "dbo", "name": "usp_x", "type": "PROCEDURE"})

    assert "definition" not in result
    assert len(result["definition_hash"]) == 8
    assert result["definition_bytes"] == len(b"CREATE PROCEDURE usp_x AS SELECT 1")


@pytest.mark.asyncio
async def test_describe_object_handles_missing_definition(env):
    """When the object service returns an error/pending state with no definition."""
    from sqlserver_semantic_mcp.server.tools import object_tool

    fake = {"status": "error", "error_message": "not found"}
    with patch.object(object_tool.object_service, "describe_object", new=AsyncMock(return_value=fake)):
        result = await object_tool._describe_object_tool({"schema": "dbo", "name": "missing", "type": "VIEW"})

    assert result["status"] == "error"
    assert result["definition_hash"] is None


@pytest.mark.asyncio
async def test_describe_object_unified_types(monkeypatch, env):
    """VIEW / PROCEDURE / FUNCTION all route through the same handler."""
    from sqlserver_semantic_mcp.server.tools import object_tool

    async def fake_describe(schema, name, object_type, cfg, policy=None):
        assert policy is not None
        return {
            "schema": schema,
            "object_name": name,
            "object_type": object_type,
            "status": "ok",
            "dependencies": ["dbo.Base"],
            "read_tables": ["dbo.Base"],
            "write_tables": [],
            "definition": "SELECT 1",
            "definition_bytes": 8,
        }

    monkeypatch.setattr(object_tool.object_service, "describe_object", fake_describe)

    for t in ("VIEW", "PROCEDURE", "FUNCTION"):
        out = await object_tool._describe_object(
            {"schema": "dbo", "name": "X"},
            t,
        )
        assert out["type"] == t
        assert out["read_tables"] == ["dbo.Base"]

    brief = await object_tool._describe_object(
        {"schema": "dbo", "name": "X"},
        "VIEW",
    )
    assert brief["depends_on"] == ["dbo.Base"]


@pytest.mark.asyncio
async def test_describe_object_tool_dispatches_on_type_arg(monkeypatch, env):
    """The registered tool handler reads args['type'] to pick the object type."""
    from sqlserver_semantic_mcp.server.tools import object_tool

    seen_types = []

    async def fake_describe(schema, name, object_type, cfg, policy=None):
        assert policy is not None
        seen_types.append(object_type)
        return {
            "schema": schema,
            "object_name": name,
            "object_type": object_type,
            "status": "ready",
            "dependencies": [],
        }

    monkeypatch.setattr(object_tool.object_service, "describe_object", fake_describe)

    for t in ("VIEW", "PROCEDURE", "FUNCTION"):
        out = await object_tool._describe_object_tool({"schema": "dbo", "name": "X", "type": t})
        assert out["type"] == t

    assert seen_types == ["VIEW", "PROCEDURE", "FUNCTION"]


@pytest.mark.asyncio
async def test_describe_object_definition_is_byte_capped(env, monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_MAX_RESPONSE_BYTES", "1024")
    from sqlserver_semantic_mcp.config import reset_config

    reset_config()
    from sqlserver_semantic_mcp.server.tools import object_tool

    fake = {
        "schema": "dbo",
        "object_name": "usp_big",
        "object_type": "PROCEDURE",
        "definition": "x" * 5000,
        "dependencies": [],
        "status": "ready",
    }
    with patch.object(object_tool.object_service, "describe_object", new=AsyncMock(return_value=fake)):
        result = await object_tool._describe_object_tool(
            {"schema": "dbo", "name": "usp_big", "type": "PROCEDURE", "include_definition": True}
        )

    assert len(result["definition"]) == 1024
    assert result["definition_truncated"] is True
    assert result["definition_bytes"] == 5000
