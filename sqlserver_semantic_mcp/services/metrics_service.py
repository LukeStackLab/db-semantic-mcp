"""Aggregate payload and execution metrics without storing SQL or row data."""
from datetime import datetime, timezone
from math import ceil
from typing import Optional

import aiosqlite

_METRIC_COLUMNS = {
    "route_type": "TEXT", "detail": "TEXT", "response_mode": "TEXT",
    "token_budget_hint": "TEXT", "was_direct_execute": "INTEGER",
    "bundle_used": "INTEGER", "next_action": "TEXT", "elapsed_ms": "REAL",
    "worker_wait_ms": "REAL", "db_duration_ms": "REAL", "fetched_rows": "INTEGER",
    "cache_hit": "INTEGER", "error_category": "TEXT",
}


async def migrate_metrics(db: aiosqlite.Connection) -> None:
    """Called once at store initialization, including for older cache stores."""
    cur = await db.execute("PRAGMA table_info(tool_metrics)")
    existing = {row[1] for row in await cur.fetchall()}
    for name, dtype in _METRIC_COLUMNS.items():
        if name not in existing:
            await db.execute(f"ALTER TABLE tool_metrics ADD COLUMN {name} {dtype}")


async def record_metric(db_path: str, tool_name: str, *, response_bytes: int,
                        array_length: Optional[int] = None, fields_returned: Optional[int] = None,
                        route_type=None, detail=None, response_mode=None, token_budget_hint=None,
                        was_direct_execute=None, bundle_used=None, next_action=None,
                        elapsed_ms=None, worker_wait_ms=None, db_duration_ms=None,
                        fetched_rows=None, cache_hit=None, error_category=None) -> None:
    values = {"tool_name": tool_name, "response_bytes": response_bytes,
              "array_length": array_length, "fields_returned": fields_returned,
              "route_type": route_type, "detail": detail, "response_mode": response_mode,
              "token_budget_hint": token_budget_hint, "was_direct_execute": was_direct_execute,
              "bundle_used": bundle_used, "next_action": next_action, "elapsed_ms": elapsed_ms,
              "worker_wait_ms": worker_wait_ms, "db_duration_ms": db_duration_ms,
              "fetched_rows": fetched_rows, "cache_hit": cache_hit, "error_category": error_category,
              "recorded_at": datetime.now(timezone.utc).isoformat()}
    async with aiosqlite.connect(db_path) as db:
        await db.execute(f"INSERT INTO tool_metrics ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                         tuple(values.values()))
        await db.commit()


def _percentile(values: list, percentile: float):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, ceil(percentile * len(ordered)) - 1)]


def _p95(values: list[int]) -> int:
    return _percentile(values, 0.95) or 0


async def query_top_tools(db_path: str, *, limit: int = 10) -> list[dict]:
    if not 1 <= limit <= 100:
        raise ValueError("metrics limit must be between 1 and 100")
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT tool_name,COUNT(*) AS call_count,SUM(response_bytes) AS total_bytes,"
            "AVG(response_bytes) AS avg_bytes,MAX(response_bytes) AS max_bytes,"
            "SUM(CASE WHEN was_direct_execute=1 THEN 1 ELSE 0 END) AS direct_execute_count,"
            "SUM(CASE WHEN bundle_used=1 THEN 1 ELSE 0 END) AS bundle_count,"
            "SUM(CASE WHEN error_category IS NOT NULL THEN 1 ELSE 0 END) AS error_count,"
            "AVG(elapsed_ms) AS avg_elapsed_ms,AVG(worker_wait_ms) AS avg_worker_wait_ms,"
            "AVG(db_duration_ms) AS avg_db_duration_ms,SUM(fetched_rows) AS fetched_rows,"
            "AVG(cache_hit) AS observed_cache_hit_rate "
            "FROM tool_metrics GROUP BY tool_name ORDER BY total_bytes DESC LIMIT ?", (limit,))
        result = [dict(row) for row in await cur.fetchall()]
        for row in result:
            cur = await db.execute("SELECT response_bytes,elapsed_ms FROM tool_metrics WHERE tool_name=?",
                                   (row["tool_name"],))
            observations = await cur.fetchall()
            row["p95_bytes"] = _p95([item[0] for item in observations])
            durations = [item[1] for item in observations if item[1] is not None]
            row["p50_elapsed_ms"] = _percentile(durations, .50)
            row["p95_elapsed_ms"] = _percentile(durations, .95)
            row["avg_bytes"] = int(row["avg_bytes"] or 0)
        return result


async def clear_metrics(db_path: str) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("DELETE FROM tool_metrics")
        await db.commit()
        return cur.rowcount
