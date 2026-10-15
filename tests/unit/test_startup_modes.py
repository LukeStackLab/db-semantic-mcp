from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from db_semantic_mcp.config import get_config, reset_config


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "testdb")
    monkeypatch.setenv("SEMANTIC_MCP_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_PASSWORD", "p")
    monkeypatch.setenv("SEMANTIC_MCP_CACHE_PATH", str(tmp_path / "t.db"))
    reset_config()


@pytest.mark.asyncio
async def test_startup_cache_first_reuses_existing_cache(env, monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_STARTUP_MODE", "cache_first")
    reset_config()
    cfg = get_config()

    from db_semantic_mcp import main as mod

    with (
        patch.object(mod, "init_store", AsyncMock()),
        patch.object(
            mod,
            "read_schema_version",
            AsyncMock(
                return_value={
                    "database_name": "testdb",
                    "structural_hash": "cached-hash",
                    "captured_at": "2026-04-21T00:00:00+00:00",
                }
            ),
        ),
        patch.object(mod, "warmup_structural_cache", AsyncMock()) as warmup,
        patch.object(mod, "enqueue_all_tables", AsyncMock()) as enqueue,
        patch.object(
            mod,
            "background_fill_loop",
            MagicMock(return_value="bg-loop"),
        ) as bg_loop,
        patch.object(mod, "register_all"),
        patch.object(mod, "register_prompts"),
        patch.object(mod, "get_context"),
        patch("db_semantic_mcp.main.asyncio.create_task") as create_task,
    ):
        fake_task = MagicMock()
        create_task.return_value = fake_task

        task = await mod._startup()

    warmup.assert_not_awaited()
    enqueue.assert_awaited_once_with(cfg.cache_path, cfg.database)
    create_task.assert_called_once()
    bg_loop.assert_called_once_with(cfg)
    assert task is fake_task


@pytest.mark.asyncio
async def test_startup_full_mode_forces_warmup(env, monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_STARTUP_MODE", "full")
    reset_config()
    cfg = get_config()

    from db_semantic_mcp import main as mod

    with (
        patch.object(mod, "init_store", AsyncMock()),
        patch.object(
            mod,
            "read_schema_version",
            AsyncMock(
                return_value={
                    "database_name": "testdb",
                    "structural_hash": "old-hash",
                    "captured_at": "2026-04-21T00:00:00+00:00",
                }
            ),
        ),
        patch.object(
            mod,
            "warmup_structural_cache",
            AsyncMock(return_value={"structural_hash": "fresh-hash"}),
        ) as warmup,
        patch.object(mod, "enqueue_all_tables", AsyncMock()) as enqueue,
        patch.object(
            mod,
            "background_fill_loop",
            MagicMock(return_value="bg-loop"),
        ) as bg_loop,
        patch.object(mod, "register_all"),
        patch.object(mod, "register_prompts"),
        patch.object(mod, "get_context"),
        patch("db_semantic_mcp.main.asyncio.create_task") as create_task,
    ):
        fake_task = MagicMock()
        create_task.return_value = fake_task

        task = await mod._startup()

    warmup.assert_awaited_once_with(cfg)
    enqueue.assert_awaited_once_with(cfg.cache_path, cfg.database)
    create_task.assert_called_once()
    bg_loop.assert_called_once_with(cfg)
    assert task is fake_task
