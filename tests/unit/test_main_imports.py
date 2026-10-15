def test_main_module_imports(monkeypatch):
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_DATABASE", "x")
    monkeypatch.setenv("SEMANTIC_MCP_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_PASSWORD", "p")
    from db_semantic_mcp import main

    assert hasattr(main, "main")
