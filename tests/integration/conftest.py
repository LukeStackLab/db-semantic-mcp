"""Only explicit disposable targets; never inherit generic runtime DB settings."""

import os
from pathlib import Path

import pytest


def _profile(provider):
    if os.getenv(f"SEMANTIC_MCP_DISPOSABLE_{provider.upper()}_TESTS") != "1":
        pytest.skip(f"Explicit disposable {provider} opt-in required")
    target = os.getenv(f"SEMANTIC_MCP_TEST_{provider.upper()}_PROFILE")
    if not target or not Path(target).is_absolute():
        pytest.fail("An absolute disposable connection.json profile path is required")
    from db_semantic_mcp.profiles import load_profile

    cfg = load_profile(Path(target))
    expected = "sqlserver" if provider == "sql" else "oracle"
    if cfg.database_type != expected:
        pytest.fail("Disposable profile provider mismatch")
    return cfg


@pytest.fixture
def disposable_config(tmp_path):
    cfg = _profile("sql")
    cfg.cache_path = str(tmp_path / "sql.db")
    return cfg


@pytest.fixture
def oracle_config(tmp_path):
    cfg = _profile("oracle")
    cfg.cache_path = str(tmp_path / "oracle.db")
    cfg.max_cell_chars = 64
    return cfg
