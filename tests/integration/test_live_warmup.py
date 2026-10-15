import os

import pytest

from sqlserver_semantic_mcp.config import get_config, reset_config
from sqlserver_semantic_mcp.infrastructure.cache.store import init_store
from sqlserver_semantic_mcp.infrastructure.cache.structural import (
    read_schema_version,
    warmup_structural_cache,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_live_warmup(tmp_path, monkeypatch):
    if not os.getenv("SEMANTIC_MCP_MSSQL_SERVER"):
        pytest.skip("SEMANTIC_MCP_MSSQL_SERVER not set")
    monkeypatch.setenv("SEMANTIC_MCP_CACHE_PATH", str(tmp_path / "live.db"))
    reset_config()
    cfg = get_config()
    await init_store(cfg.cache_path)
    result = await warmup_structural_cache(cfg)
    assert result["structural_hash"]
    ver = await read_schema_version(cfg.cache_path, cfg.mssql_database)
    assert ver is not None
