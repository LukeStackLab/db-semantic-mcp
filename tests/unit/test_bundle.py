"""Unit tests for bundle_context_for_next_step."""

import pytest

from sqlserver_semantic_mcp.config import get_config, reset_config
from sqlserver_semantic_mcp.infrastructure.cache.store import init_store
from sqlserver_semantic_mcp.workflows.bundle import bundle_context_for_next_step


@pytest.fixture
async def cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_DATABASE", "testdb")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_PASSWORD", "p")
    monkeypatch.setenv("SEMANTIC_MCP_CACHE_PATH", str(tmp_path / "bundle.db"))
    reset_config()
    c = get_config()
    await init_store(c.cache_path)
    return c


async def _seed_table(cfg, schema: str, table: str, columns: list[tuple]):
    import aiosqlite

    async with aiosqlite.connect(cfg.cache_path) as db:
        await db.execute(
            "INSERT INTO sc_tables(database_name, schema_name, table_name) VALUES (?,?,?)",
            (cfg.mssql_database, schema, table),
        )
        for ord_pos, (name, dtype, nullable) in enumerate(columns, start=1):
            await db.execute(
                "INSERT INTO sc_columns(database_name, schema_name, table_name, "
                "column_name, data_type, max_length, is_nullable, "
                "column_default, ordinal_position) VALUES (?,?,?,?,?,?,?,?,?)",
                (cfg.mssql_database, schema, table, name, dtype, None, 1 if nullable else 0, None, ord_pos),
            )
        await db.execute(
            "INSERT INTO sc_primary_keys(database_name, schema_name, table_name, column_name) VALUES (?,?,?,?)",
            (cfg.mssql_database, schema, table, columns[0][0]),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_bundle_joining_returns_compact_summary(cfg):
    await _seed_table(
        cfg,
        "sales",
        "orders",
        [
            ("order_id", "int", False),
            ("customer_id", "int", False),
            ("total", "decimal", True),
        ],
    )

    env = await bundle_context_for_next_step(
        [{"kind": "table", "schema": "sales", "table": "orders"}],
        goal="joining",
        cfg=cfg,
    )
    assert env["kind"] == "bundle_context_for_next_step"
    assert env["bundle_key"] == "joining"
    tables = env["data"]["tables"]
    assert len(tables) == 1
    assert tables[0]["table"] == "sales.orders"
    assert tables[0]["pk"] == ["order_id"]
    assert "order_id" in tables[0]["important_columns"]


@pytest.mark.asyncio
async def test_bundle_unknown_goal_returns_error_payload(cfg):
    env = await bundle_context_for_next_step(
        [],
        goal="not-a-thing",
        cfg=cfg,
    )
    assert "error" in env["data"]
    assert "supported_goals" in env["data"]
