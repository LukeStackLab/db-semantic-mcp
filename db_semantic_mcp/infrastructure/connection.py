import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pymssql as pymssql

from ..config import Config, get_config

logger = logging.getLogger(__name__)


def build_pymssql_kwargs(cfg: Config) -> dict[str, Any]:
    server = cfg.server
    if server.lower().startswith("(localdb)\\"):
        instance = server.split("\\", 1)[1]
        server = f".\\{instance}"

    kwargs: dict[str, Any] = {
        "server": server,
        "database": cfg.database,
        "port": cfg.port,
        "timeout": cfg.query_timeout,
        "login_timeout": min(cfg.query_timeout, 10),
    }

    if ".database.windows.net" in server.lower():
        kwargs["tds_version"] = "7.4"

    if cfg.encrypt:
        kwargs["tds_version"] = "7.4"
        kwargs["encryption"] = "require"

    if not cfg.windows_auth:
        if cfg.user is None or cfg.password is None:
            raise ValueError("SQL auth requires SEMANTIC_MCP_USER and SEMANTIC_MCP_PASSWORD")
        kwargs["user"] = cfg.user
        kwargs["password"] = cfg.password

    return kwargs


@contextmanager
def open_connection(cfg: Config | None = None) -> Iterator[Any]:
    cfg = cfg or get_config()
    from .backends import backend_for

    conn = backend_for(cfg).connect(cfg)
    from .recording.dbapi import instrument_db_connection

    conn = instrument_db_connection(conn)
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
        from .backends import backend_for

        backend = backend_for(cfg)
        return [tuple(backend.convert_value(v, cfg) for v in row) for row in rows]


def fetch_one(cfg: Config, sql: str, params: tuple = ()) -> tuple | None:
    with open_connection(cfg) as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        row = cursor.fetchone()
        cursor.close()
        from .backends import backend_for

        backend = backend_for(cfg)
        return tuple(backend.convert_value(v, cfg) for v in row) if row is not None else None


def execute(cfg: Config, sql: str, params: tuple = ()) -> int:
    with open_connection(cfg) as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        affected = cursor.rowcount
        conn.commit()
        cursor.close()
        return affected
