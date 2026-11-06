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
                v3 = getattr(cfg, "profile_version", 2) == 3
                if v3 and str(actual) != cfg.database:
                    cursor.execute("SELECT CASE WHEN DB_ID(%s) = DB_ID() THEN 1 ELSE 0 END", (cfg.database,))
                    if cursor.fetchone()[0] != 1:
                        raise SetupError("實際連線資料庫與配置不一致。")
                elif not v3 and str(actual).casefold() != cfg.database.casefold():
                    raise SetupError("實際連線資料庫與配置不一致。")
                if v3:
                    cursor.execute("SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DEFINITION')")
                    if cursor.fetchone()[0] != 1:
                        raise SetupError("Database 缺少 VIEW DEFINITION 權限，無法驗證完整 metadata。")
                    cursor.execute("SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'ALTER ANY SECURITY POLICY')")
                    if cursor.fetchone()[0] != 1:
                        raise SetupError("無法確認 security policy metadata 完整性，已拒絕驗證此 Database。")
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
                    if profile.format_version == 3:
                        by_name = {tool.name: tool for tool in tools.tools}
                        if "list_databases" not in by_name:
                            raise SetupError("MCP 缺少 Database 選擇工具。")
                        if len(profile.databases) > 1:
                            for tool in tools.tools:
                                if tool.name != "list_databases" and "database" not in tool.inputSchema.get(
                                    "required", []
                                ):
                                    raise SetupError("MCP 多庫工具未要求 Database 參數。")
                        response = await session.call_tool("list_databases", {})
                        if response.isError:
                            raise SetupError("MCP Database 清單驗證失敗。")
                    return len(tools.tools)


def check_mcp(path: Path, *, timeout: float = 60) -> int:
    try:
        return asyncio.run(_handshake(path, timeout))
    except Exception:  # noqa: BLE001 (security boundary – never echo driver errors)
        raise SetupError("MCP 啟動或握手失敗。請使用 doctor 檢查配置與執行環境。") from None


def discover_databases(cfg: Config) -> list[str]:
    """Return only online, visible non-system databases verified with this login."""
    try:
        with open_connection(cfg) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT name FROM sys.databases "
                    "WHERE database_id > 4 AND state = 0 AND HAS_DBACCESS(name) = 1 ORDER BY name"
                )
                names = [str(row[0]) for row in cursor.fetchall()]
            finally:
                cursor.close()
    except Exception:  # noqa: BLE001 (driver errors may contain credentials)
        raise SetupError("無法列舉 Database。可改用手動輸入並逐庫驗證。") from None
    verified = []
    for name in names:
        try:
            result = check_connection(cfg.model_copy(update={"database": name}))
        except SetupError:
            continue
        canonical = result["database"]
        if canonical not in verified:
            verified.append(canonical)
    return sorted(verified)


def _authorized_databases(cfg: Config) -> list[str]:
    names = list(cfg.databases) if getattr(cfg, "profile_version", 2) == 3 else [cfg.database]
    if getattr(cfg, "profile_version", 2) == 3:
        from ..policy.loader import load_active_policy

        policy = load_active_policy(cfg)
        allowed = policy.scope.allowed_databases
        names = [name for name in names if name in allowed]
    return names


def check_database_set(cfg: Config, database: str | None = None) -> dict:
    """Read-only diagnostics retain every requested database's independent result."""
    names = _authorized_databases(cfg)
    if database is not None:
        if database not in names:
            raise SetupError("指定 Database 未配置或未經 policy 授權。")
        names = [database]
    results = {}
    for name in names:
        try:
            result = check_connection(cfg.model_copy(update={"database": name}))
            results[name] = {"status": "ok", **result}
        except SetupError:
            results[name] = {"status": "error", "message": "無法連線或讀取 metadata。"}
    return {"ok": bool(results) and all(row["status"] == "ok" for row in results.values()), "databases": results}
