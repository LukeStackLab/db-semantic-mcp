import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from mcp.server.stdio import stdio_server

from . import __version__
from .config import configure, get_config
from .infrastructure.background import background_fill_loop
from .infrastructure.cache.semantic import enqueue_all_tables
from .infrastructure.cache.store import init_store
from .infrastructure.cache.structural import (
    read_schema_version,
    warmup_structural_cache,
)
from .infrastructure.workers import shutdown_db_workers
from .server import resources  # noqa: F401
from .server.app import app, get_context
from .server.prompts import register_prompts
from .server.tools import register_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("db_semantic_mcp")


async def _startup() -> asyncio.Task | None:
    cfg = get_config()
    logger.info(
        "Starting db-semantic-mcp against %s/%s",
        cfg.server,
        cfg.database,
    )

    get_context()  # Bind effective policy/cache identity before any cache or DB work.
    await init_store(cfg.cache_path)

    bg_task: asyncio.Task | None = None
    if cfg.cache_enabled:
        existing = await read_schema_version(cfg.cache_path, cfg.database)
        should_warmup = cfg.startup_mode == "full" or existing is None
        if should_warmup:
            if existing is None:
                logger.info("No cache found; running structural warmup")
            else:
                logger.info(
                    "Startup mode '%s' requires a fresh structural warmup (cached_at=%s)",
                    cfg.startup_mode,
                    existing["captured_at"],
                )
            await warmup_structural_cache(cfg)
        else:
            logger.info(
                "Startup mode '%s' reuses existing cache (captured_at=%s)",
                cfg.startup_mode,
                existing["captured_at"],
            )
        await enqueue_all_tables(cfg.cache_path, cfg.database)
        bg_task = asyncio.create_task(background_fill_loop(cfg))

    register_all()
    register_prompts()
    get_context()
    return bg_task


async def _run() -> None:
    bg_task = None
    try:
        bg_task = await _startup()
        async with stdio_server() as (r, w):
            await app.run(r, w, app.create_initialization_options())
    finally:
        if bg_task is not None:
            bg_task.cancel()
            try:
                await bg_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 (shutdown cleanup)
                pass
        from .infrastructure.cache.revalidation import _BACKGROUND_TASKS

        pending = list(_BACKGROUND_TASKS)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        from .server.source_context import shutdown_source_contexts

        await shutdown_source_contexts()
        await shutdown_db_workers()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="DB Semantic MCP：獨立連線與互動式安裝",
    )
    parser.add_argument("--version", action="version", version=f"db-semantic-mcp {__version__}")
    commands = parser.add_subparsers(dest="command")
    setup = commands.add_parser("setup", help="互動式安裝與配置")
    setup.add_argument("--config-dir", type=Path, help="連線配置根目錄")
    setup.add_argument("--client", choices=["codex", "claude-code", "claude-desktop", "cursor"])
    setup.add_argument("--client-config", type=Path, help="指定 Client 設定檔")
    setup.add_argument(
        "--tool-profile",
        choices=["all", "query", "explore", "maintenance"],
        help="選擇此批連線啟用的工具集合；省略時保留既有配置",
    )
    for name, help_text in [("serve", "啟動固定配置的 MCP"), ("doctor", "檢查配置、連線與 MCP 握手")]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config", type=Path, required=name == "doctor", help="獨立連線 JSON 配置")
    migration = commands.add_parser("migrate", help="預覽舊版 v1 配置遷移；--apply 才套用")
    migration.add_argument("--legacy-root", type=Path, help="舊 connections 根目錄或單一配置目錄")
    migration.add_argument("--config-dir", type=Path, help="新的 connections 根目錄")
    action = migration.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="明確套用遷移並驗證 MCP")
    action.add_argument("--dry-run", action="store_true", help="只預覽、不讀取憑證、不寫入；預設行為")
    migration.add_argument("--client", choices=["codex", "claude-code", "claude-desktop", "cursor"])
    migration.add_argument("--client-config", type=Path)
    migration.add_argument("--client-path", action="append", help="額外 Client 路徑 CLIENT=PATH，可重複")
    args = parser.parse_args(argv)
    try:
        if args.command == "migrate":
            from .setup.migration import cli_migrate

            cli_migrate(args)
            return
        if args.command == "setup":
            from .setup.wizard import terminal_setup

            raise SystemExit(
                terminal_setup(
                    root=args.config_dir,
                    client=args.client,
                    client_config=args.client_config,
                    tool_profile=args.tool_profile,
                )
            )
        if args.command in {"serve", "doctor"} and args.config:
            from .profiles import load_profile

            cfg = load_profile(args.config)
            configure(cfg)
            if args.command == "doctor":
                from .setup.checks import check_connection, check_mcp

                result = check_connection(cfg)
                count = check_mcp(args.config)
                print(
                    json.dumps(
                        {"connection": cfg.connection_name, **result, "mcp_tools": count},
                        ensure_ascii=False,
                        default=str,
                        indent=2,
                    )
                )
                return
        asyncio.run(_run())
    except (KeyboardInterrupt, EOFError):
        print("已取消。", file=sys.stderr)
        raise SystemExit(130) from None
    except Exception as exc:  # noqa: BLE001 (security boundary – never echo driver errors)
        # Serving errors must not expose driver credentials or contaminate
        # stdout, which is reserved for MCP.
        from .infrastructure.backends.oracle import MissingOracleDriverError
        from .setup.files import SetupError

        message = (
            str(exc)
            if isinstance(exc, (SetupError, MissingOracleDriverError))
            else "請檢查指定配置、憑證與檔案權限；可執行 setup 重新配置。"
        )
        print(f"啟動失敗：{message}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
