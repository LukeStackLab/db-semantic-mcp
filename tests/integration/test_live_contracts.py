"""Opt-in, read-only checks against an explicitly selected disposable SQL Server."""

import os
from decimal import Decimal

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def disposable_config():
    if os.getenv("SEMANTIC_MCP_DISPOSABLE_SQL_TESTS") != "1":
        pytest.skip("Explicit disposable SQL Server opt-in required")
    from sqlserver_semantic_mcp.config import Config

    return Config()


def test_live_scalar_and_result_metadata(disposable_config):
    from sqlserver_semantic_mcp.services.policy_service import PolicyService
    from sqlserver_semantic_mcp.services.query_service import QueryService

    policy = PolicyService(disposable_config)
    result = QueryService(policy, disposable_config).execute_query(
        "SELECT CAST(123.4500 AS decimal(19,4)) AS amount", validate_result_schema=True
    )
    assert result["executed"] is True, result
    assert result["rows"] == [[Decimal("123.4500")]]
    assert result["total_row_count"] == 1
    assert result["result_schema"][0]["type"] == "decimal(19,4)"


def test_live_catalog_tuple_formats(disposable_config):
    import json

    from sqlserver_semantic_mcp.infrastructure.cache.structural import fetch_snapshot_from_server

    snapshot = fetch_snapshot_from_server(disposable_config)
    assert all(len(row) == 12 for row in snapshot.columns)
    assert all(len(row) == 11 and isinstance(row[6], int) and row[8] >= 1 for row in snapshot.foreign_keys)
    assert all(len(row) == 8 and isinstance(json.loads(row[5]), list) for row in snapshot.indexes)
