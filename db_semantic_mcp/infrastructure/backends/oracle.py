"""Oracle driver handling; mode is process-wide and never changed implicitly."""

import importlib
import os
import threading
from decimal import Decimal

from ...config import Config

_mode_lock = threading.Lock()
_thick_options: tuple | None = None


def _driver():
    try:
        return importlib.import_module("oracledb")
    except ImportError:
        raise RuntimeError(
            "Oracle requires the optional dependency: pip install 'lukestack-db-semantic-mcp[oracle]'"
        ) from None


def _configure_mode(driver, options):
    global _thick_options
    with _mode_lock:
        if options.driver_mode == "thin":
            if not driver.is_thin_mode():
                raise RuntimeError("Oracle Thick mode is already active; Thin requires a separate process")
            # Pin Thin before leaving the lock; init_oracle_client must not race a first connection.
            driver.enable_thin_mode()
        else:
            identity = (options.client_lib_dir, options.config_dir)
            if _thick_options is not None and _thick_options != identity:
                raise RuntimeError("Oracle Thick initialization options differ from this process")
            if _thick_options is None:
                driver.init_oracle_client(lib_dir=options.client_lib_dir, config_dir=options.config_dir)
                _thick_options = identity


def number_output_handler(driver):
    def handler(cursor, metadata):
        if metadata.type_code == driver.DB_TYPE_NUMBER:
            return cursor.var(driver.DB_TYPE_VARCHAR, arraysize=cursor.arraysize, outconverter=Decimal)
        return None

    return handler


class OracleBackend:
    def connect(self, cfg: Config):
        driver = _driver()
        options = cfg.oracle
        if options is None:
            raise ValueError("Oracle options are required")
        _configure_mode(driver, options)
        params = {
            "tcp_connect_timeout": options.connect_timeout,
            "config_dir": options.config_dir,
        }
        if options.dsn is None:
            params.update(host=cfg.server, port=cfg.port, service_name=options.service_name, sid=options.sid)
        if options.wallet_location:
            params["wallet_location"] = options.wallet_location
        if options.wallet_password_env:
            password = os.environ.get(options.wallet_password_env)
            if password is None:
                raise ValueError("Oracle wallet password reference is unavailable")
            params["wallet_password"] = password
        connection = driver.connect(user=cfg.user, password=cfg.password, dsn=options.dsn, **params)
        try:
            connection.autocommit = False
            # Oracle call_timeout bounds one network round trip, not total execution time.
            connection.call_timeout = cfg.query_timeout * 1000
            connection.outputtypehandler = number_output_handler(driver)
            if options.current_schema:
                connection.current_schema = options.current_schema.upper()
        except Exception:
            connection.close()
            raise
        return connection

    def fetch_snapshot(self, cfg: Config, conn=None):
        from .oracle_catalog import fetch_snapshot

        return fetch_snapshot(cfg, conn)

    def fetch_fingerprints(self, cfg: Config, conn=None):
        from .oracle_catalog import fetch_fingerprints

        return fetch_fingerprints(cfg, conn)

    def get_object_definition(self, cfg: Config, schema: str, name: str, object_type: str | None = None):
        from .oracle_catalog import get_object_definition

        return get_object_definition(cfg, schema, name, object_type)

    def get_object_dependencies(self, cfg: Config, schema: str, name: str):
        from .oracle_catalog import get_object_dependencies

        return get_object_dependencies(cfg, schema, name)

    def describe_result_schema(self, cursor, sql: str):
        # parse() executes DDL! Independently reject non-SELECT even if callers forget authorization.
        import sqlglot
        from sqlglot import exp

        try:
            statements = sqlglot.parse(sql, read="oracle", error_level="RAISE")
        except sqlglot.errors.ParseError:
            raise ValueError("Oracle result description requires a parsed SELECT") from None
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise ValueError("Oracle result description accepts SELECT only")
        if any(statements[0].find_all(exp.Lock, exp.Into, exp.Insert, exp.Update, exp.Delete, exp.Command)):
            raise ValueError("Oracle result description accepts read-only SELECT only")
        cursor.parse(sql)
        return [
            {
                "ordinal": i,
                "name": d[0],
                "type": getattr(d[1], "name", str(d[1])),
                "is_nullable": d[6],
                "precision": d[4],
                "scale": d[5],
            }
            for i, d in enumerate(cursor.description or (), 1)
        ]

    def convert_value(self, value, cfg: Config):
        # Duck-typing supports driver LOBs without importing Oracle in SQL Server processes.
        if callable(getattr(value, "read", None)) and callable(getattr(value, "size", None)):
            # One extra unit lets the shared output shaper report truncation. Never read() without amount.
            amount = 2 * (cfg.max_cell_chars + 1)
            try:
                prefix = value.read(offset=1, amount=amount)
            except UnicodeDecodeError:
                # CLOB offsets count UCS-2 units; avoid half of a supplementary character.
                prefix = value.read(offset=1, amount=amount - 1)
            return prefix[: cfg.max_cell_chars + 1]
        return value

    @staticmethod
    def quote_identifier(value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    def capabilities(self, cfg: Config) -> dict:
        return {
            "database_type": "oracle",
            "source": cfg.source_id,
            "driver_mode": cfg.oracle.driver_mode,
            "inventory_scope": "Application schemas (ORACLE_MAINTAINED=N); explicit policy.allowed_schemas overrides",
            "definitions": True,
            "dependencies": True,
            "remote_sources": True,
            "remote_column_defaults": True,
            "remote_lob_support": "driver/version dependent; no full-read fallback",
            "query_timeout_semantics": "call_timeout per network round trip (milliseconds); not a total deadline",
            "connect_timeout_semantics": "TCP connection establishment timeout per address; not total login deadline",
        }
