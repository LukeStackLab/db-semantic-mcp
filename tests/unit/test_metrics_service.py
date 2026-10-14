"""Tests for metrics recording and aggregation (P4)."""
import pytest

from sqlserver_semantic_mcp.infrastructure.cache.store import init_store
from sqlserver_semantic_mcp.services import metrics_service


@pytest.fixture
def cache_path(tmp_path):
    return str(tmp_path / "metrics.db")


@pytest.mark.asyncio
async def test_record_single_metric(cache_path):
    await init_store(cache_path)
    await metrics_service.record_metric(
        cache_path, "describe_table",
        response_bytes=256, array_length=None, fields_returned=6,
    )

    top = await metrics_service.query_top_tools(cache_path, limit=10)
    assert len(top) == 1
    assert top[0]["tool_name"] == "describe_table"
    assert top[0]["call_count"] == 1
    assert top[0]["total_bytes"] == 256
    assert top[0]["max_bytes"] == 256


@pytest.mark.asyncio
async def test_aggregation_sorts_by_total_bytes(cache_path):
    await init_store(cache_path)
    # Tool A: small but called many times
    for _ in range(5):
        await metrics_service.record_metric(
            cache_path, "tool_a", response_bytes=100,
        )
    # Tool B: big, called twice
    for _ in range(2):
        await metrics_service.record_metric(
            cache_path, "tool_b", response_bytes=400,
        )

    top = await metrics_service.query_top_tools(cache_path)
    assert [r["tool_name"] for r in top] == ["tool_b", "tool_a"]
    assert top[0]["total_bytes"] == 800
    assert top[0]["call_count"] == 2
    assert top[1]["total_bytes"] == 500


@pytest.mark.asyncio
async def test_p95_computed_correctly(cache_path):
    await init_store(cache_path)
    for n in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
        await metrics_service.record_metric(
            cache_path, "tool_x", response_bytes=n,
        )
    top = await metrics_service.query_top_tools(cache_path)
    # Nearest rank: ceil(0.95 * 10) = 10, so the tenth sorted value.
    assert top[0]["p95_bytes"] == 100


@pytest.mark.asyncio
async def test_p95_empty_is_zero(cache_path):
    await init_store(cache_path)
    top = await metrics_service.query_top_tools(cache_path)
    assert top == []


@pytest.mark.asyncio
async def test_limit_truncates(cache_path):
    await init_store(cache_path)
    for i in range(5):
        await metrics_service.record_metric(
            cache_path, f"t{i}", response_bytes=100 + i,
        )
    top = await metrics_service.query_top_tools(cache_path, limit=2)
    assert len(top) == 2


@pytest.mark.asyncio
async def test_clear_metrics(cache_path):
    await init_store(cache_path)
    await metrics_service.record_metric(cache_path, "t", response_bytes=1)
    await metrics_service.record_metric(cache_path, "t", response_bytes=2)
    n = await metrics_service.clear_metrics(cache_path)
    assert n == 2
    top = await metrics_service.query_top_tools(cache_path)
    assert top == []
