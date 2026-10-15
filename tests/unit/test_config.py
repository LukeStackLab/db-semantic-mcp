from sqlserver_semantic_mcp.config import get_config, reset_config


def test_config_defaults(monkeypatch):
    reset_config()
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_SERVER", "localhost")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_DATABASE", "testdb")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_USER", "sa")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_PASSWORD", "x")
    cfg = get_config()
    assert cfg.mssql_server == "localhost"
    assert cfg.mssql_port == 1433
    assert cfg.cache_enabled is True
    assert cfg.cache_path.endswith("semantic_mcp.db")
    assert cfg.startup_mode == "cache_first"
    assert cfg.probe_interval_s == 60.0
    assert cfg.max_rows_returned == 1000


def test_config_windows_auth(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # isolate from local .env
    reset_config()
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_SERVER", "localhost")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_DATABASE", "testdb")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_WINDOWS_AUTH", "true")
    monkeypatch.delenv("SEMANTIC_MCP_MSSQL_USER", raising=False)
    monkeypatch.delenv("SEMANTIC_MCP_MSSQL_PASSWORD", raising=False)
    cfg = get_config()
    assert cfg.mssql_windows_auth is True
    assert cfg.mssql_user is None
