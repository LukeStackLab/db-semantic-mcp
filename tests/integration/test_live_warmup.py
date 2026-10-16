import pytest

from db_semantic_mcp.infrastructure.cache.store import init_store
from db_semantic_mcp.infrastructure.cache.structural import (
    read_schema_version,
    warmup_structural_cache,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_live_warmup(disposable_config):
    cfg = disposable_config
    await init_store(cfg.cache_path)
    result = await warmup_structural_cache(cfg)
    assert result["structural_hash"]
    ver = await read_schema_version(cfg.cache_path, cfg.database)
    assert ver is not None
