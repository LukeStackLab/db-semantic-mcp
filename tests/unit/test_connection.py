from sqlserver_semantic_mcp.config import Config
from sqlserver_semantic_mcp.infrastructure.connection import build_pymssql_kwargs


def test_build_kwargs_sql_auth():
    cfg = Config(
        mssql_server="localhost", mssql_database="db",
        mssql_user="sa", mssql_password="x",
    )
    kwargs = build_pymssql_kwargs(cfg)
    assert kwargs["server"] == "localhost"
    assert kwargs["user"] == "sa"
    assert kwargs["password"] == "x"
    assert kwargs["database"] == "db"
    assert kwargs["port"] == 1433


def test_build_kwargs_windows_auth():
    cfg = Config(
        mssql_server="localhost", mssql_database="db",
        mssql_windows_auth=True,
    )
    kwargs = build_pymssql_kwargs(cfg)
    assert "user" not in kwargs
    assert "password" not in kwargs


def test_build_kwargs_azure():
    cfg = Config(
        mssql_server="my.database.windows.net",
        mssql_database="db", mssql_user="u", mssql_password="p",
    )
    kwargs = build_pymssql_kwargs(cfg)
    assert kwargs.get("tds_version") == "7.4"


def test_timeout_and_required_encryption_are_passed_to_driver():
    cfg = Config(
        _env_file=None, mssql_server="s", mssql_database="db",
        mssql_user="reader", mssql_password="x",
        query_timeout=5, mssql_encrypt=True,
    )
    kwargs = build_pymssql_kwargs(cfg)
    assert kwargs["timeout"] == 5
    assert kwargs["login_timeout"] == 5
    assert kwargs["encryption"] == "require"
