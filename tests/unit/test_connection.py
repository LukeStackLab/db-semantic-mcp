from db_semantic_mcp.config import Config
from db_semantic_mcp.infrastructure.connection import build_pymssql_kwargs


def test_build_kwargs_sql_auth():
    cfg = Config(
        server="localhost",
        database="db",
        user="sa",
        password="x",
    )
    kwargs = build_pymssql_kwargs(cfg)
    assert kwargs["server"] == "localhost"
    assert kwargs["user"] == "sa"
    assert kwargs["password"] == "x"
    assert kwargs["database"] == "db"
    assert kwargs["port"] == 1433


def test_build_kwargs_windows_auth():
    cfg = Config(
        server="localhost",
        database="db",
        windows_auth=True,
    )
    kwargs = build_pymssql_kwargs(cfg)
    assert "user" not in kwargs
    assert "password" not in kwargs


def test_build_kwargs_azure():
    cfg = Config(
        server="my.database.windows.net",
        database="db",
        user="u",
        password="p",
    )
    kwargs = build_pymssql_kwargs(cfg)
    assert kwargs.get("tds_version") == "7.4"


def test_timeout_and_required_encryption_are_passed_to_driver():
    cfg = Config(
        _env_file=None,
        server="s",
        database="db",
        user="reader",
        password="x",
        query_timeout=5,
        encrypt=True,
    )
    kwargs = build_pymssql_kwargs(cfg)
    assert kwargs["timeout"] == 5
    assert kwargs["login_timeout"] == 5
    assert kwargs["encryption"] == "require"
