import asyncio
import argparse
import logging
import sys
from pathlib import Path

from mcp.server.stdio import stdio_server

from .config import configure, get_config
from .infrastructure.background import background_fill_loop
from .infrastructure.cache.semantic import enqueue_all_tables
from .infrastructure.cache.store import init_store
from .infrastructure.cache.structural import (
    read_schema_version, warmup_structural_cache,
)
from .server.app import app, get_context
from .server import resources  # noqa: F401
from .server.prompts import register_prompts
from .server.tools import register_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("sqlserver_semantic_mcp")


async def _startup() -> asyncio.Task | None:
    cfg = get_config()
    logger.info(
        "Starting sqlserver-semantic-mcp against %s/%s",
        cfg.mssql_server, cfg.mssql_database,
    )

    await init_store(cfg.cache_path)

    bg_task: asyncio.Task | None = None
    if cfg.cache_enabled:
        existing = await read_schema_version(cfg.cache_path, cfg.mssql_database)
        should_warmup = (
            cfg.startup_mode == "full"
            or existing is None
        )
        if should_warmup:
            if existing is None:
                logger.info("No cache found; running structural warmup")
            else:
                logger.info(
                    "Startup mode '%s' requires a fresh structural warmup "
                    "(cached_at=%s)",
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
        await enqueue_all_tables(cfg.cache_path, cfg.mssql_database)
        bg_task = asyncio.create_task(background_fill_loop(cfg))

    register_all()
    register_prompts()
    get_context()
    return bg_task


async def _run() -> None:
    bg_task = await _startup()
    try:
        async with stdio_server() as (r, w):
            await app.run(r, w, app.create_initialization_options())
    finally:
        if bg_task is not None:
            bg_task.cancel()
            try:
                await bg_task
            except (asyncio.CancelledError, Exception):
                pass


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="SQL Server Semantic MCP：獨立連線與互動式安裝",
    )
    commands = parser.add_subparsers(dest="command")
    setup = commands.add_parser("setup", help="互動式安裝與配置")
    setup.add_argument("--config-dir", type=Path, help="連線配置根目錄")
    setup.add_argument("--client", choices=["codex", "claude-code", "claude-desktop", "cursor"])
    setup.add_argument("--client-config", type=Path, help="指定 Client 設定檔")
    for name, help_text in [("serve", "啟動固定配置的 MCP"), ("doctor", "檢查配置、連線與 MCP 握手")]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config", type=Path, required=name == "doctor", help="獨立連線 JSON 配置")
    args = parser.parse_args(argv)
    try:
        if args.command == "setup":
            from .setup.wizard import terminal_setup
            raise SystemExit(terminal_setup(
                root=args.config_dir, client=args.client, client_config=args.client_config,
            ))
        if args.command in {"serve", "doctor"} and args.config:
            from .profiles import load_profile
            cfg = load_profile(args.config)
            configure(cfg)
            if args.command == "doctor":
                from .setup.checks import check_connection, check_mcp
                result = check_connection(cfg)
                count = check_mcp(args.config)
                print(f"配置 {cfg.connection_name}：連線成功；可見 {result['visible_tables']} 張表；MCP 握手成功，{count} 個工具。")
                return
        asyncio.run(_run())
    except (KeyboardInterrupt, EOFError):
        print("已取消。", file=sys.stderr)
        raise SystemExit(130) from None
    except Exception as exc:
        # Serving errors must not expose driver credentials or contaminate
        # stdout, which is reserved for MCP.
        from .setup.files import SetupError
        message = str(exc) if isinstance(exc, SetupError) else "請檢查指定配置、憑證與檔案權限；可執行 setup 重新配置。"
        print(f"啟動失敗：{message}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
