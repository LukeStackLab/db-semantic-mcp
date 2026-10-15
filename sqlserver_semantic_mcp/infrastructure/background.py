import asyncio
import logging

from ..config import Config, get_config
from ..services import semantic_service
from .cache.semantic import list_pending_table_analyses, record_table_analysis_failure

logger = logging.getLogger(__name__)


async def run_background_fill_once(cfg: Config | None = None) -> int:
    cfg = cfg or get_config()
    pending = await list_pending_table_analyses(
        cfg.cache_path,
        cfg.mssql_database,
        cfg.background_batch_size,
    )
    for s, t in pending:
        try:
            await semantic_service.classify_table(
                cfg.cache_path,
                cfg.mssql_database,
                s,
                t,
                force=True,
            )
        except Exception as exc:  # noqa: BLE001 (background task must not crash the server)
            message = str(exc)
            if cfg.mssql_password:
                message = message.replace(cfg.mssql_password, "***")
            await record_table_analysis_failure(
                cfg.cache_path,
                cfg.mssql_database,
                s,
                t,
                error_message=message,
                retry_base_s=cfg.background_retry_base_s,
                retry_max_s=cfg.background_retry_max_s,
            )
            logger.error("Background classify failed for %s.%s: %s", s, t, message)
    return len(pending)


async def background_fill_loop(
    cfg: Config | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    cfg = cfg or get_config()
    stop_event = stop_event or asyncio.Event()
    interval = cfg.background_interval_ms / 1000.0

    consecutive_errors = 0
    max_backoff = 60.0

    while not stop_event.is_set():
        try:
            processed = await run_background_fill_once(cfg)
            consecutive_errors = 0  # reset on any successful iteration
            if processed == 0:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=10.0)
                except TimeoutError:
                    continue
                continue
            await asyncio.sleep(interval)
        except Exception:  # noqa: BLE001 (background task must not crash the server)
            consecutive_errors += 1
            backoff = min(max_backoff, 2.0**consecutive_errors)
            logger.exception(
                "Background fill iteration failed (attempt=%d, next retry in %.1fs)",
                consecutive_errors,
                backoff,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except TimeoutError:
                continue
