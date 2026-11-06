"""V3 bootstrap connection checks precede all cache work."""

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from db_semantic_mcp import main as mod
from db_semantic_mcp.config import Config, configure, reset_config
from db_semantic_mcp.infrastructure import workers
from db_semantic_mcp.policy.models import PolicyProfile
from db_semantic_mcp.setup import checks


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_enabled", [True, False], ids=["existing-cache", "cache-disabled"])
@pytest.mark.parametrize("unavailable", [False, True], ids=["available", "unavailable"])
async def test_v3_bootstrap_probes_before_cache_and_registration(tmp_path, cache_enabled, unavailable):
    cfg = Config.from_fixed(
        server="host",
        database="ERP",
        databases=["ERP", "CRM"],
        profile_version=3,
        cache_path=str(tmp_path / "cache.db"),
        cache_enabled=cache_enabled,
        startup_mode="cache_first",
    )
    cfg._policy_snapshot = PolicyProfile(profile_name="readonly", scope={"allowed_databases": ["ERP", "CRM"]})
    configure(cfg)
    events = []
    loop_thread = threading.get_ident()

    def probe(actual):
        assert actual is cfg
        assert threading.get_ident() != loop_thread
        events.append("probe")
        if unavailable:
            raise RuntimeError("Database unavailable")
        return {"ok": True}

    async def initialize(path):
        assert path == cfg.cache_path
        events.append("cache")

    try:
        with (
            patch.object(mod, "get_context", side_effect=lambda: events.append("policy")),
            patch.object(checks, "check_connection", side_effect=probe) as connection,
            patch.object(workers, "run_db_work", AsyncMock(wraps=workers.run_db_work)) as worker,
            patch.object(mod, "init_store", AsyncMock(side_effect=initialize)) as init,
            patch.object(mod, "read_schema_version", AsyncMock(return_value={"captured_at": "cached"})) as read,
            patch.object(mod, "warmup_structural_cache", AsyncMock()) as warmup,
            patch.object(mod, "enqueue_all_tables", AsyncMock()) as enqueue,
            patch("db_semantic_mcp.server.database_context.start_database_background", AsyncMock()) as background,
            patch.object(mod, "register_all", MagicMock()) as tools,
            patch.object(mod, "register_prompts", MagicMock()) as prompts,
        ):
            if unavailable:
                with pytest.raises(RuntimeError, match="Database unavailable"):
                    await mod._startup()
                init.assert_not_awaited()
                read.assert_not_awaited()
                enqueue.assert_not_awaited()
                background.assert_not_awaited()
                tools.assert_not_called()
                prompts.assert_not_called()
                assert events == ["policy", "probe"]
            else:
                await mod._startup()
                assert events[:3] == ["policy", "probe", "cache"]
                tools.assert_called_once()
                prompts.assert_called_once()
                assert read.await_count == int(cache_enabled)
                assert background.await_count == int(cache_enabled)
            warmup.assert_not_awaited()
            connection.assert_called_once_with(cfg)
            worker.assert_awaited_once_with(cfg, connection, cfg)
    finally:
        await workers.shutdown_db_workers()
        reset_config()
