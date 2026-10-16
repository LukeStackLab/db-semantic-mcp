"""Read-only Oracle diagnostics, isolated from the wizard's driver mode."""

import json
import subprocess
import sys

from ..config import Config
from ..infrastructure.backends.oracle import MissingOracleDriverError, OracleBackend, _driver
from ..infrastructure.backends.oracle_catalog import _fetch, catalog_suffix
from ..infrastructure.backends.oracle_resolution import OracleResolver
from ..infrastructure.connection import open_connection
from ..services.policy_service import PolicyService
from .files import SetupError


def check_isolated(cfg):
    # Password travels over stdin, never argv, disk or diagnostics. Each row has
    # its own driver process, so Thin and Thick checks cannot poison each other.
    try:
        _driver()
    except MissingOracleDriverError as exc:
        raise SetupError(str(exc)) from None
    try:
        process = subprocess.run(
            [sys.executable, "-I", "-m", "db_semantic_mcp.setup.oracle_checks"],
            input=cfg.model_dump_json(),
            text=True,
            capture_output=True,
            timeout=max(30, cfg.oracle.connect_timeout + cfg.query_timeout * (len(cfg.sources) + 2) + 10),
            check=False,
        )
        result = json.loads(process.stdout)
        if process.returncode or not isinstance(result, dict) or "error" in result:
            raise ValueError("failed")
        return result
    except Exception:  # noqa: BLE001 (redact driver/credential failures)
        raise SetupError(
            "Oracle 連線或 metadata 檢查失敗，請檢查 driver mode、主機、帳密、TLS 與 catalog 權限。"
        ) from None


def _version(conn, suffix=""):
    # Product branding changed to Oracle AI Database 26ai; VERSION and the
    # Oracle prefix are stable across the supported database releases.
    rows = _fetch(conn, f"SELECT product,version_full FROM sys.product_component_version{suffix}")
    versions = [str(version) for product, version in rows if str(product).startswith("Oracle")]
    if not versions or int(versions[0].split(".")[0]) < 19:
        raise ValueError("Oracle 19c or later is required")
    return versions[0]


def check_oracle(cfg):
    policy = PolicyService(cfg)
    policy.load()
    with open_connection(cfg) as conn:
        version = _version(conn)
        transport = _fetch(conn, "SELECT SYS_CONTEXT('USERENV','NETWORK_PROTOCOL') FROM sys.dual")[0][0]
        expected = "tcps" if cfg.encrypt else "tcp"
        if str(transport).lower() != expected:
            raise SetupError("Oracle 實際傳輸協定與 encrypt 設定不一致。")
        actual = _fetch(conn, "SELECT SYS_CONTEXT('USERENV','SERVICE_NAME') FROM sys.dual")[0][0]
        if cfg.oracle.service_name and str(actual).casefold() != cfg.oracle.service_name.casefold():
            raise SetupError("Oracle 實際 service 與配置不一致。")
        if cfg.oracle.sid:
            instance = _fetch(conn, "SELECT SYS_CONTEXT('USERENV','INSTANCE_NAME') FROM sys.dual")[0][0]
            if str(instance).casefold() != cfg.oracle.sid.casefold():
                raise SetupError("Oracle 實際 instance 與 SID 配置不一致。")
        count = _fetch(conn, "SELECT COUNT(*) FROM sys.all_tables")[0][0]
        # Only route names are safe for clients. Never return HOST/descriptors.
        links = _fetch(
            conn, "SELECT owner,db_link FROM sys.all_db_links WHERE owner IN (USER,'PUBLIC') ORDER BY db_link,owner"
        )
        sources = {}
        for source in cfg.sources:
            try:
                resolved = OracleResolver(cfg, conn, policy.current_policy()).source_config(source)
                suffix = catalog_suffix(resolved, conn)
                source_version = _version(conn, suffix)
                visible = _fetch(conn, f"SELECT COUNT(*) FROM sys.all_objects{suffix}")[0][0]
                sources[source] = {
                    "status": "ok",
                    "version": source_version,
                    "visible_objects": visible,
                    "db_link": resolved.source_dblink,
                    "transport": "database-managed; not verified",
                    "capabilities": OracleBackend().capabilities(resolved),
                }
            except Exception:  # noqa: BLE001 (redact driver/credential failures)
                sources[source] = {
                    "status": "unavailable",
                    "reason": "Source policy, route, version or catalog validation failed",
                }
        return {
            "database_type": "oracle",
            "database": cfg.database,
            "service": actual,
            "version": version,
            "driver_mode": "thin" if conn.thin else "thick",
            "transport": str(transport).lower(),
            "visible_tables": count,
            "available_db_links": [{"owner": owner, "db_link": link} for owner, link in links],
            "sources": sources,
            "capabilities": OracleBackend().capabilities(cfg),
        }


if __name__ == "__main__":
    try:
        result = check_oracle(Config.from_fixed(**json.loads(sys.stdin.read())))
        print(json.dumps(result, default=str))
    except Exception:  # noqa: BLE001 (redact driver/credential failures)
        print('{"error":"Oracle validation failed"}')
        raise SystemExit(1) from None
