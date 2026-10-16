from unittest.mock import AsyncMock

import pytest

from db_semantic_mcp import main
from db_semantic_mcp.config import reset_config


def test_legacy_no_argument_starts_server(monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr(main, "_run", run)
    main.main([])
    run.assert_awaited_once()


def test_help_does_not_require_database_configuration(capsys):
    with pytest.raises(SystemExit) as result:
        main.main(["--help"])
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert "setup" in output
    assert "DB Semantic MCP" in output


def test_bad_fixed_profile_fails_on_stderr_without_traceback(tmp_path, capsys):
    with pytest.raises(SystemExit) as result:
        main.main(["serve", "--config", str(tmp_path / "missing.json")])
    assert result.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "找不到" in output.err
    assert "Traceback" not in output.err
    reset_config()


def test_registered_command_ignores_cwd_and_pythonpath_package_shadowing(tmp_path):
    import os
    import subprocess

    from db_semantic_mcp.setup.clients import server_entry

    fake = tmp_path / "db_semantic_mcp"
    fake.mkdir()
    (fake / "__init__.py").write_text("")
    (fake / "main.py").write_text("print('HIJACKED_PACKAGE')")
    entry = server_entry(tmp_path / "missing.json")
    result = subprocess.run(
        [entry["command"], *entry["args"]],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "HIJACKED_PACKAGE" not in result.stderr
    assert "找不到" in result.stderr


def test_distribution_exposes_only_the_new_product_entrypoint():
    import tomllib
    from pathlib import Path

    from db_semantic_mcp import __version__

    project = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())["project"]
    assert project["name"] == "lukestack-db-semantic-mcp"
    assert project["version"] == __version__ == "0.8.1"
    assert project["scripts"] == {"db-semantic-mcp": "db_semantic_mcp.main:main"}
