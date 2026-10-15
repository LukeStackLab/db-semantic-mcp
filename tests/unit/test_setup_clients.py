import json

import pytest
import tomlkit

from db_semantic_mcp.setup.clients import (
    ClientTarget,
    merge_servers,
    register_servers,
    server_entry,
)
from db_semantic_mcp.setup.files import SetupError


@pytest.mark.parametrize("format", ["json", "toml"])
def test_registration_preserves_unrelated_settings_and_creates_backup(tmp_path, format):
    path = tmp_path / f"config.{format}"
    target = ClientTarget("test", "Test", path, format)
    if format == "json":
        original = b'{"theme":"dark","mcpServers":{"other":{"command":"other"}}}'
    else:
        original = b'# keep this comment\nmodel = "existing"\n[mcp_servers.other]\ncommand = "other"\n'
    path.write_bytes(original)
    entry = server_entry(tmp_path / "erp" / "connection.json")
    backup = register_servers(target, {"erp": entry}, original)
    assert backup.read_bytes() == original
    text = path.read_text()
    parsed = json.loads(text) if format == "json" else tomlkit.parse(text)
    key = "mcpServers" if format == "json" else "mcp_servers"
    assert parsed[key]["other"]["command"] == "other"
    assert parsed[key]["erp"]["args"] == entry["args"]
    assert "password" not in text.lower()
    if format == "toml":
        assert "# keep this comment" in text
        assert parsed["model"] == "existing"
    else:
        assert parsed["theme"] == "dark"


@pytest.mark.parametrize(
    "format,original",
    [
        ("json", b'{"mcpServers":{"erp":{"command":"other","args":[]}}}'),
        ("toml", b'[mcp_servers.erp]\ncommand="other"\nargs=[]\n'),
    ],
)
def test_name_collision_does_not_overwrite(tmp_path, format, original):
    target = ClientTarget("test", "Test", tmp_path / "config", format)
    with pytest.raises(SetupError, match="已被其他"):
        merge_servers(target, original, {"erp": server_entry(tmp_path / "erp.json")})


@pytest.mark.parametrize(
    "format,original",
    [
        ("json", b"not-json"),
        ("json", b"[]"),
        ("json", b'{"mcpServers":[]}'),
        ("toml", b"[invalid"),
        ("toml", b'mcp_servers="invalid"'),
    ],
)
def test_malformed_client_config_is_rejected(tmp_path, format, original):
    target = ClientTarget("test", "Test", tmp_path / "config", format)
    with pytest.raises(SetupError):
        merge_servers(target, original, {})


def test_concurrent_client_edit_is_preserved(tmp_path):
    target = ClientTarget("test", "Test", tmp_path / "config.json", "json")
    target.path.write_bytes(b'{"external":true}')
    with pytest.raises(SetupError, match="其他程序"):
        register_servers(target, {"erp": server_entry(tmp_path / "erp.json")}, None)
    assert json.loads(target.path.read_text()) == {"external": True}
