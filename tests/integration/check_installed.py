"""Clean-wheel smoke: python -I /absolute/path/check_installed.py base|oracle.

Requires the same explicit disposable opt-in/profile variables as live pytest.
Run from outside the source checkout. Install only the wheel in this environment.
"""

import asyncio
import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from db_semantic_mcp.profiles import ConnectionProfile, load_profile, policy_bytes, profile_bytes
from db_semantic_mcp.setup.checks import check_connection
from db_semantic_mcp.setup.clients import server_entry


async def mcp_query(path, sql):
    entry = server_entry(path)
    entry["env"] = {k: v for k, v in os.environ.items() if not k.startswith("SEMANTIC_MCP_")}
    with tempfile.TemporaryFile(mode="w+") as errors:
        async with asyncio.timeout(180):
            async with stdio_client(StdioServerParameters(**entry), errlog=errors) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    assert len((await session.list_tools()).tools) == 12
                    result = await session.call_tool("plan_or_execute_query", {"query": sql})
                    assert not result.isError and result.structuredContent["data"]["executed"]


def main(mode):
    assert mode in {"base", "oracle"}
    assert importlib.metadata.version("lukestack-db-semantic-mcp") == "0.8.0"
    cli = Path(sys.executable).parent / ("db-semantic-mcp.exe" if os.name == "nt" else "db-semantic-mcp")
    version = subprocess.run([str(cli), "--version"], capture_output=True, text=True, check=True)
    assert version.stdout.strip() == "db-semantic-mcp 0.8.0"
    subprocess.run([str(cli), "--help"], capture_output=True, check=True)
    if mode == "base":
        assert importlib.util.find_spec("oracledb") is None
        with tempfile.TemporaryDirectory(prefix="missing-oracle-") as directory:
            p = Path(directory)
            profile = ConnectionProfile(
                name="missing",
                database_type="oracle",
                server="invalid.invalid",
                database="demo",
                user="fixture",
                encrypt=False,
                oracle={"service_name": "demo"},
                credential={"storage": "env", "key": "DBMCP_MISSING_DRIVER_PASSWORD"},
            )
            (p / "connection.json").write_bytes(profile_bytes(profile))
            (p / "policy.json").write_bytes(policy_bytes(profile))
            result = subprocess.run(
                [str(cli), "doctor", "--config", str(p / "connection.json")],
                env={**os.environ, "DBMCP_MISSING_DRIVER_PASSWORD": "unused-fixture-value"},
                capture_output=True,
                text=True,
            )
            assert result.returncode == 1
            assert "uv pip install '.[oracle]'" in result.stderr
            assert "unused-fixture-value" not in result.stderr
        provider = "SQL"
        sql = "SELECT CAST(123.4500 AS decimal(19,4)) AS amount"
    else:
        assert importlib.util.find_spec("oracledb") is not None
        provider = "ORACLE"
        sql = "SELECT AMOUNT FROM DBMCP_TEST.DBMCP_CHAIN"
    assert os.getenv(f"SEMANTIC_MCP_DISPOSABLE_{provider}_TESTS") == "1"
    path = Path(os.environ[f"SEMANTIC_MCP_TEST_{provider}_PROFILE"])
    assert path.is_absolute()
    cfg = load_profile(path)
    assert cfg.database_type == ("sqlserver" if provider == "SQL" else "oracle")
    diagnostic = check_connection(cfg)
    asyncio.run(mcp_query(path, sql))
    print(
        json.dumps(
            {
                "status": "passed",
                "install": mode,
                "version": "0.8.0",
                "provider": cfg.database_type,
                "driver_mode": diagnostic.get("driver_mode"),
                "transport": diagnostic.get("transport"),
                "mcp_tools": 12,
                "actual_query": True,
            }
        )
    )


if __name__ == "__main__":
    main(sys.argv[1])
