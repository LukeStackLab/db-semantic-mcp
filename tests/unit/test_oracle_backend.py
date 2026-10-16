"""Oracle driver/config boundary without an Oracle dependency or live server."""

import builtins
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from db_semantic_mcp.config import Config, OracleOptions
from db_semantic_mcp.infrastructure.backends import backend_for, oracle
from db_semantic_mcp.profiles import ConnectionProfile, config_for_profile, read_profile
from db_semantic_mcp.setup.files import SetupError


def oracle_config(**kwargs):
    defaults = dict(
        database_type="oracle",
        server="host",
        user="app",
        password="secret",
        oracle={"service_name": "FREEPDB1"},
    )
    defaults.update(kwargs)
    return Config.from_fixed(**defaults)


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"service_name": "x", "sid": "x"},
        {"dsn": "x", "service_name": "x"},
        {"service_name": "x", "client_lib_dir": "/client"},
        {"service_name": "x", "typo": 3},
        {"service_name": "x", "wallet_password_env": "SECRET"},
        {"dsn": "x", "current_schema": "X;DROP TABLE Y"},
        {"dsn": "x", "driver_mode": "thick", "wallet_location": "/wallet"},
    ],
)
def test_invalid_oracle_options(options):
    with pytest.raises(ValueError):
        OracleOptions(**options)


def test_default_ports_dsn_and_exclusive_provider():
    assert oracle_config().port == 1521
    assert Config(_env_file=None, server="s", database="d").port == 1433
    assert oracle_config(server="", oracle={"dsn": "my_alias"}).database == "oracle"
    assert oracle_config(oracle={"sid": "ORCL"}).database == "ORCL"
    with pytest.raises(ValueError):
        oracle_config(server="")
    with pytest.raises(ValueError):
        oracle_config(windows_auth=True)
    with pytest.raises(ValueError):
        Config(_env_file=None, server="s", database="d", oracle={"sid": "x"})


def test_sources_are_independent_and_routes_are_not_auth():
    cfg = oracle_config(sources={"erp": {"db_link": "erp.example"}, "crm": {"db_link": "crm.example"}})
    erp, crm = cfg.for_source("erp"), cfg.for_source("crm")
    assert erp.source_dblink == "ERP.EXAMPLE"
    assert len({erp.cache_path, crm.cache_path, cfg.cache_path}) == 3
    assert erp.for_source("crm").cache_path == crm.cache_path
    assert erp.for_source("local").cache_path == cfg.cache_path
    erp.sources.clear()
    assert len(cfg.sources) == 2
    with pytest.raises(ValueError):
        cfg.for_source("not-configured")


@pytest.mark.parametrize(
    "source", [{"local": {"db_link": "X"}}, {"x": {"db_link": "X;DROP"}}, {"../x": {"db_link": "X"}}]
)
def test_invalid_sources(source):
    with pytest.raises(ValueError):
        oracle_config(sources=source)


def test_profile_v2_and_env_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_ORACLE", '{"dsn":"bad"}')
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE_TYPE", "oracle")
    monkeypatch.setenv("SEMANTIC_MCP_SOURCES", '{"evil":{"db_link":"EVIL"}}')
    monkeypatch.setenv("SEMANTIC_MCP_SOURCE_ID", "evil")
    profile = ConnectionProfile(name="test", server="s", database="d", user="u")
    cfg = config_for_profile(profile, tmp_path, "password")
    assert profile.format_version == 2
    assert cfg.database_type == "sqlserver" and cfg.oracle is None and cfg.sources == {}
    assert cfg.source_id == "local"
    profile = ConnectionProfile(
        name="ora", database="service", user="app", database_type="oracle", oracle={"dsn": "alias"}
    )
    assert profile.port == 1521
    with pytest.raises(ValueError):
        ConnectionProfile(name="test", server="s", database="d", user="u", format_version=2, budget="tiny")
    path = tmp_path / "connection.json"
    path.write_text(json.dumps({"format_version": 1, "name": "old"}))
    with pytest.raises(SetupError, match="migrate"):
        read_profile(path)


@pytest.fixture
def fake_driver(monkeypatch):
    driver = MagicMock()
    driver.is_thin_mode.return_value = True

    def params(**kwargs):
        value = MagicMock(**kwargs)
        value.protocol = kwargs.get("protocol", "tcp")
        return value

    driver.ConnectParams.side_effect = params
    monkeypatch.setattr(oracle, "_driver", lambda: driver)
    monkeypatch.setattr(oracle, "_thick_options", None)
    return driver


def test_thin_connect_timeout_decimal_and_current_schema(fake_driver):
    cfg = oracle_config(oracle={"service_name": "service", "current_schema": "app", "connect_timeout": 3})
    conn = backend_for(cfg).connect(cfg)
    fake_driver.enable_thin_mode.assert_called_once()
    kwargs = fake_driver.ConnectParams.call_args.kwargs
    assert kwargs["tcp_connect_timeout"] == 3 and kwargs["service_name"] == "service"
    assert kwargs["host"] == "host" and kwargs["port"] == 1521
    assert conn.call_timeout == 30000 and conn.autocommit is False
    assert conn.current_schema == "APP"
    cursor = MagicMock(arraysize=100)
    conn.outputtypehandler(cursor, SimpleNamespace(type_code=fake_driver.DB_TYPE_NUMBER))
    assert cursor.var.call_args.kwargs["outconverter"]("1.234567890123456789") == Decimal("1.234567890123456789")


def test_thick_initialized_once_with_same_options(fake_driver):
    cfg = oracle_config(oracle={"sid": "x", "driver_mode": "thick", "client_lib_dir": "/oracle"})
    backend_for(cfg).connect(cfg)
    backend_for(cfg).connect(cfg)
    fake_driver.init_oracle_client.assert_called_once_with(lib_dir="/oracle", config_dir=None)
    other = oracle_config(oracle={"sid": "x", "driver_mode": "thick", "client_lib_dir": "/other"})
    with pytest.raises(RuntimeError, match="differ"):
        backend_for(other).connect(other)
    fake_driver.is_thin_mode.return_value = False
    with pytest.raises(RuntimeError, match="separate process"):
        backend_for(oracle_config()).connect(oracle_config())


def test_wallet_reference_and_connection_cleanup(fake_driver, monkeypatch):
    cfg = oracle_config(oracle={"dsn": "alias", "wallet_location": "/wallet", "wallet_password_env": "ORA_WALLET_TEST"})
    monkeypatch.delenv("ORA_WALLET_TEST", raising=False)
    with pytest.raises(ValueError, match="unavailable"):
        backend_for(cfg).connect(cfg)
    monkeypatch.setenv("ORA_WALLET_TEST", "private")
    backend_for(cfg).connect(cfg)
    assert fake_driver.ConnectParams.call_args.kwargs["wallet_password"] == "private"
    assert "private" not in cfg.model_dump_json()


def test_optional_driver_import_is_lazy(monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "oracledb":
            raise AssertionError("eager Oracle driver import")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    assert (
        backend_for(Config(_env_file=None, server="s", database="d")).capabilities(oracle_config())["database_type"]
        == "sqlserver"
    )
    assert backend_for(oracle_config()).capabilities(oracle_config())["database_type"] == "oracle"


def test_bounded_lob_and_exact_decimal():
    cfg = oracle_config(max_cell_chars=16)
    backend = backend_for(cfg)
    lob = MagicMock()
    lob.read.return_value = "x" * 34
    assert backend.convert_value(lob, cfg) == "x" * 17
    lob.read.assert_called_once_with(offset=1, amount=34)
    value = Decimal("999999999999999.1234")
    assert backend.convert_value(value, cfg) is value
    lob.reset_mock()
    lob.read.side_effect = [UnicodeDecodeError("utf-16", b"x", 0, 1, "surrogate"), "a😀" * 16]
    assert len(backend.convert_value(lob, cfg)) == 17
    assert [c.kwargs["amount"] for c in lob.read.call_args_list] == [34, 33]


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE bad(id INT)",
        "BEGIN NULL; END;",
        "SELECT 1 FROM dual; DROP TABLE x",
        "SELECT * FROM x FOR UPDATE",
        "UPDATE x SET a=1",
    ],
)
def test_description_never_parses_non_select(sql):
    cursor = MagicMock()
    with pytest.raises(ValueError):
        backend_for(oracle_config()).describe_result_schema(cursor, sql)
    cursor.parse.assert_not_called()


def test_description_does_not_execute_select():
    cursor = MagicMock()
    cursor.description = [("AMOUNT", SimpleNamespace(name="DB_TYPE_NUMBER"), None, None, 19, 4, True)]
    result = backend_for(oracle_config()).describe_result_schema(cursor, "SELECT amount FROM orders")
    assert result[0]["precision"] == 19 and result[0]["scale"] == 4
    cursor.parse.assert_called_once_with("SELECT amount FROM orders")
    cursor.execute.assert_not_called()


def test_catalog_route_identity_changes_cache():
    cfg = oracle_config(sources={"remote": {"db_link": "LINK"}})
    a = cfg.for_source("remote", route_identity={"owner": "APP", "username": "FIRST", "host": "one"})
    b = cfg.for_source("remote", route_identity={"owner": "APP", "username": "SECOND", "host": "one"})
    assert a.cache_path != b.cache_path
    assert cfg.source_id == "local"


def test_lob_conversion_happens_before_connection_close(monkeypatch):
    from db_semantic_mcp.infrastructure import connection

    cfg = oracle_config(max_cell_chars=16)
    conn, cursor, lob = MagicMock(), MagicMock(), MagicMock()
    conn.cursor.return_value = cursor
    cursor.fetchone.return_value = (lob, Decimal("1.23"))

    def read(**kwargs):
        conn.close.assert_not_called()
        return "bounded"

    lob.read.side_effect = read
    monkeypatch.setattr(oracle.OracleBackend, "connect", lambda *_: conn)
    assert connection.fetch_one(cfg, "SELECT data, amount FROM t") == ("bounded", Decimal("1.23"))
    conn.close.assert_called_once()


@pytest.mark.parametrize("field", ["database", "user"])
def test_profile_rejects_whitespace_identifiers(field):
    values = {"name": "test", "server": "s", "database": "d", "user": "u", field: "   "}
    with pytest.raises(ValueError):
        ConnectionProfile(**values)


@pytest.mark.parametrize(
    "kind,scale,allowed",
    [
        ("DB_TYPE_TIMESTAMP", 6, True),
        ("DB_TYPE_TIMESTAMP", 0, True),
        ("DB_TYPE_TIMESTAMP", 9, False),
        ("DB_TYPE_TIMESTAMP", None, False),
        ("DB_TYPE_TIMESTAMP_TZ", 6, False),
        ("DB_TYPE_TIMESTAMP_LTZ", 6, False),
        ("DB_TYPE_DATE", 0, True),
    ],
)
def test_thin_temporal_output_never_silently_loses_precision_or_timezone(kind, scale, allowed):
    driver = SimpleNamespace(
        **{
            k: k
            for k in [
                "DB_TYPE_NUMBER",
                "DB_TYPE_VARCHAR",
                "DB_TYPE_TIMESTAMP",
                "DB_TYPE_TIMESTAMP_TZ",
                "DB_TYPE_TIMESTAMP_LTZ",
                "DB_TYPE_DATE",
            ]
        }
    )
    handler = oracle.number_output_handler(driver, driver_mode="thin")
    cursor = MagicMock()
    metadata = SimpleNamespace(type_code=getattr(driver, kind), scale=scale)
    if allowed:
        assert handler(cursor, metadata) is None
    else:
        with pytest.raises(oracle.OracleResultTypeError, match="TO_CHAR"):
            handler(cursor, metadata)
    cursor.var.assert_not_called()


def test_thick_temporal_strings_keep_ff9_and_zone_without_datetime_conversion():
    driver = SimpleNamespace(
        **{
            k: k
            for k in [
                "DB_TYPE_NUMBER",
                "DB_TYPE_VARCHAR",
                "DB_TYPE_TIMESTAMP",
                "DB_TYPE_TIMESTAMP_TZ",
                "DB_TYPE_TIMESTAMP_LTZ",
                "DB_TYPE_DATE",
            ]
        }
    )
    cursor = MagicMock(arraysize=100)
    handler = oracle.number_output_handler(driver, driver_mode="thick")
    handler(cursor, SimpleNamespace(type_code=driver.DB_TYPE_TIMESTAMP_TZ, scale=9))
    kwargs = cursor.var.call_args.kwargs
    assert kwargs["size"] >= 100
    assert (
        kwargs["outconverter"]("2026-10-15T12:34:56.123456789 ASIA/TAIPEI CST")
        == "2026-10-15T12:34:56.123456789 ASIA/TAIPEI CST"
    )
    handler(cursor, SimpleNamespace(type_code=driver.DB_TYPE_TIMESTAMP_LTZ, scale=9))
    assert (
        cursor.var.call_args.kwargs["outconverter"]("2026-10-15T04:34:56.123456789") == "2026-10-15T04:34:56.123456789Z"
    )
    assert "FF9 TZR TZD" in oracle._SESSION_FORMATS
    assert "TIME_ZONE = '+00:00'" in oracle._SESSION_FORMATS


def test_temporal_capabilities_are_precision_and_timezone_specific():
    thin = oracle.OracleBackend().capabilities(oracle_config())
    thick = oracle.OracleBackend().capabilities(oracle_config(oracle={"service_name": "db", "driver_mode": "thick"}))
    assert thin["native_timestamp_max_precision"] == 6 and not thin["native_timestamp_timezone"]
    assert thick["native_timestamp_max_precision"] == 9 and thick["native_timestamp_timezone"]
    assert "TO_CHAR" in thin["timestamp_workaround"]
