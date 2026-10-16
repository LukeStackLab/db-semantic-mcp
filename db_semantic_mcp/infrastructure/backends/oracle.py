"""Oracle driver handling; mode is process-wide and never changed implicitly."""

import importlib
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


# Fixed session formats avoid Python datetime's six-digit precision and its
# discarded TZ offset. LTZ has no original timezone stored: return it in UTC.
_SESSION_FORMATS = """ALTER SESSION SET
    TIME_ZONE = '+00:00'
    NLS_NUMERIC_CHARACTERS = '.,'
    NLS_DATE_LANGUAGE = 'AMERICAN'
    NLS_CALENDAR = 'GREGORIAN'
    NLS_DATE_FORMAT = 'SYYYY-MM-DD"T"HH24:MI:SS'
    NLS_TIMESTAMP_FORMAT = 'SYYYY-MM-DD"T"HH24:MI:SS.FF9'
    NLS_TIMESTAMP_TZ_FORMAT = 'SYYYY-MM-DD"T"HH24:MI:SS.FF9 TZR TZD'
"""


class OracleResultTypeError(ValueError):
    """Controlled diagnostic: native Thin temporal conversion would lose data."""

    def __init__(self):
        super().__init__(
            "Oracle Thin cannot safely fetch TIMESTAMP with precision above 6 or unknown precision, or TIMESTAMP WITH TIME ZONE/LOCAL TIME ZONE. "
            "Select explicit text instead of that native column (including SELECT *): "
            "TO_CHAR(ts_column, 'YYYY-MM-DD HH24:MI:SS.FF9') for TIMESTAMP/LTZ; "
            "TO_CHAR(tz_column, 'YYYY-MM-DD HH24:MI:SS.FF9 TZR TZD') for TIMESTAMP WITH TIME ZONE. "
            "LTZ text uses the session time zone. Alternatively use explicit Thick mode."
        )


def number_output_handler(driver, *, driver_mode="thin"):
    """Per-connection exact NUMBER and temporal handling (legacy API name)."""
    timestamp_types = {
        getattr(driver, name, None) for name in ("DB_TYPE_TIMESTAMP", "DB_TYPE_TIMESTAMP_TZ", "DB_TYPE_TIMESTAMP_LTZ")
    }
    timestamp_types.discard(None)

    def handler(cursor, metadata):
        if metadata.type_code == driver.DB_TYPE_NUMBER:
            return cursor.var(driver.DB_TYPE_VARCHAR, arraysize=cursor.arraysize, outconverter=Decimal)
        temporal = metadata.type_code in timestamp_types
        if temporal and driver_mode == "thin":
            scale = getattr(metadata, "scale", None)
            if metadata.type_code == driver.DB_TYPE_TIMESTAMP and isinstance(scale, int) and 0 <= scale <= 6:
                return None
            raise OracleResultTypeError()
        if driver_mode == "thick" and (temporal or metadata.type_code == getattr(driver, "DB_TYPE_DATE", None)):
            converter = (
                (lambda value: value.strip() + "Z" if value is not None else None)
                if metadata.type_code == driver.DB_TYPE_TIMESTAMP_LTZ
                else (lambda value: value.strip() if value is not None else None)
            )
            return cursor.var(driver.DB_TYPE_VARCHAR, size=128, arraysize=cursor.arraysize, outconverter=converter)
        return None

    return handler


class OracleBackend:
    def connect(self, cfg: Config):
        driver = _driver()
        options = cfg.oracle
        if options is None:
            raise ValueError("Oracle options are required")
        _configure_mode(driver, options)
        from .oracle_transport import connection_params

        params = connection_params(driver, cfg)
        connection = driver.connect(user=cfg.user, password=cfg.password, params=params)
        try:
            connection.autocommit = False
            # Oracle call_timeout bounds one network round trip, not total execution time.
            connection.call_timeout = cfg.query_timeout * 1000
            if options.driver_mode == "thick":
                with connection.cursor() as cursor:
                    cursor.execute(_SESSION_FORMATS)
            connection.outputtypehandler = number_output_handler(driver, driver_mode=options.driver_mode)
            if options.current_schema:
                connection.current_schema = options.current_schema.upper()
        except Exception:  # noqa: BLE001 (fail closed without driver or SQL disclosure)
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
        from sqlglot import exp

        from ...policy.intents.oracle_analyzer import parse_oracle

        try:
            statement, _ = parse_oracle(sql)
            if not isinstance(statement, exp.Query):
                raise ValueError("SELECT required")
        except Exception:  # noqa: BLE001 (fail closed without driver or SQL disclosure)
            raise ValueError("Oracle result description accepts proven SELECT only") from None
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
            "transport_required": "tcps" if cfg.encrypt else "tcp",
            "remote_transport": "DB-link transport is managed by the database; not verified end-to-end TLS",
            "inventory_scope": "Application schemas (ORACLE_MAINTAINED=N); explicit policy.allowed_schemas overrides",
            "definitions": True,
            "dependencies": True,
            "remote_sources": True,
            "remote_column_defaults": True,
            "native_timestamp_fetch": "all_precisions_and_zones"
            if cfg.oracle.driver_mode == "thick"
            else "plain_timestamp_precision_0_to_6_only",
            "native_timestamp_max_precision": 9 if cfg.oracle.driver_mode == "thick" else 6,
            "native_timestamp_timezone": cfg.oracle.driver_mode == "thick",
            "temporal_values": (
                "Thick: DATE seconds; TIMESTAMP FF9; TZ numeric offset or region/daylight abbreviation; LTZ UTC (Z)"
                if cfg.oracle.driver_mode == "thick"
                else "Thin: DATE and proven TIMESTAMP(p<=6) remain datetime; higher/unknown precision TIMESTAMP and all TZ/LTZ rejected"
            ),
            "timestamp_workaround": "Select TO_CHAR(ts, 'YYYY-MM-DD HH24:MI:SS.FF9'); for TZ use 'YYYY-MM-DD HH24:MI:SS.FF9 TZR TZD'",
            "session_timezone": "UTC (+00:00)" if cfg.oracle.driver_mode == "thick" else "database login default",
            "remote_lob_support": "driver/version dependent; no full-read fallback",
            "query_timeout_semantics": "call_timeout per network round trip (milliseconds); not a total deadline",
            "connect_timeout_semantics": "TCP connection establishment timeout per address; not total login deadline",
        }
