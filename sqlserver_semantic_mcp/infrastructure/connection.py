import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pymssql

from ..config import Config, get_config

logger = logging.getLogger(__name__)


def build_pymssql_kwargs(cfg: Config) -> dict[str, Any]:
    server = cfg.mssql_server
    if server.lower().startswith("(localdb)\\"):
        instance = server.split("\\", 1)[1]
        server = f".\\{instance}"

    kwargs: dict[str, Any] = {
        "server": server,
        "database": cfg.mssql_database,
        "port": cfg.mssql_port,
        "timeout": cfg.query_timeout,
        "login_timeout": min(cfg.query_timeout, 10),
    }

    if ".database.windows.net" in server.lower():
        kwargs["tds_version"] = "7.4"

    if cfg.mssql_encrypt:
        kwargs["tds_version"] = "7.4"
        kwargs["encryption"] = "require"

    if not cfg.mssql_windows_auth:
        if cfg.mssql_user is None or cfg.mssql_password is None:
            raise ValueError("SQL auth requires SEMANTIC_MCP_MSSQL_USER and SEMANTIC_MCP_MSSQL_PASSWORD")
        kwargs["user"] = cfg.mssql_user
        kwargs["password"] = cfg.mssql_password

    return kwargs


@contextmanager
def open_connection(cfg: Config | None = None) -> Iterator[Any]:
    cfg = cfg or get_config()
    kwargs = build_pymssql_kwargs(cfg)
    conn = pymssql.connect(**kwargs)
    try:
        yield conn
    finally:
        conn.close()


def fetch_all(cfg: Config, sql: str, params: tuple = ()) -> list[tuple]:
    with open_connection(cfg) as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        cursor.close()
        return rows


def fetch_one(cfg: Config, sql: str, params: tuple = ()) -> tuple | None:
    with open_connection(cfg) as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        row = cursor.fetchone()
        cursor.close()
        return row


def execute(cfg: Config, sql: str, params: tuple = ()) -> int:
    with open_connection(cfg) as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        affected = cursor.rowcount
        conn.commit()
        cursor.close()
        return affected
