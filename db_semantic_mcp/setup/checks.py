"""Read-only connection checks and an actual MCP subprocess handshake."""

import asyncio
import os
import tempfile
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from ..config import Config
from ..infrastructure.connection import open_connection
from .clients import server_entry
from .files import SetupError


def check_connection(cfg: Config) -> dict:
    if cfg.database_type == "oracle":
        from .oracle_checks import check_isolated

        return check_isolated(cfg)
    try:
        with open_connection(cfg) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("SELECT DB_NAME()")
                actual = cursor.fetchone()[0]
                if str(actual).casefold() != cfg.database.casefold():
                    raise SetupError("實際連線資料庫與配置不一致。")
                cursor.execute("SELECT COUNT(*) FROM sys.tables")
                count = cursor.fetchone()[0]
            finally:
                cursor.close()
    except SetupError:
        raise
    except Exception:  # noqa: BLE001 (security boundary – never echo driver errors)
        # Database/driver errors can contain credentials. Do not echo them.
        raise SetupError("無法連線或讀取 metadata，請檢查主機、port、帳號、密碼與 TLS 設定。") from None
    return {"database": actual, "visible_tables": count}


async def _handshake(path: Path, timeout: float) -> int:
    entry = server_entry(path)
    from ..profiles import read_profile

    profile = read_profile(path)
    references = []
    if profile.credential and profile.credential.storage == "env":
        references.append(profile.credential.key)
    if profile.oracle and profile.oracle.wallet_password_env:
        references.append(profile.oracle.wallet_password_env)
    entry["env"] = {key: os.environ[key] for key in references if key in os.environ}
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors:
        async with asyncio.timeout(timeout):
            async with stdio_client(StdioServerParameters(**entry), errlog=errors) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    if not tools.tools:
                        raise SetupError("MCP 未公開任何工具。")
                    return len(tools.tools)


def check_mcp(path: Path, *, timeout: float = 60) -> int:
    try:
        return asyncio.run(_handshake(path, timeout))
    except Exception:  # noqa: BLE001 (security boundary – never echo driver errors)
        raise SetupError("MCP 啟動或握手失敗。請使用 doctor 檢查配置與執行環境。") from None
