from unittest.mock import MagicMock

import pytest

from sqlserver_semantic_mcp.config import Config
from sqlserver_semantic_mcp.credentials import CredentialReference
from sqlserver_semantic_mcp.infrastructure.cache.store import init_store
from sqlserver_semantic_mcp.infrastructure.cache.structural import StructuralSnapshot, write_structural_snapshot
from sqlserver_semantic_mcp.profiles import ConnectionProfile, load_profile, policy_bytes, profile_bytes
from sqlserver_semantic_mcp.setup import checks
from sqlserver_semantic_mcp.setup.files import SetupError, atomic_write


def test_connection_check_is_read_only_and_closes_cursor(monkeypatch):
    cfg = Config(_env_file=None, mssql_server="s", mssql_database="ERP", mssql_user="u", mssql_password="SECRET")
    opened = MagicMock()
    cursor = opened.return_value.__enter__.return_value.cursor.return_value
    cursor.fetchone.side_effect = [("ERP",), (5,)]
    monkeypatch.setattr(checks, "open_connection", opened)
    assert checks.check_connection(cfg) == {"database": "ERP", "visible_tables": 5}
    assert [call.args[0] for call in cursor.execute.call_args_list] == [
        "SELECT DB_NAME()",
        "SELECT COUNT(*) FROM sys.tables",
    ]
    cursor.close.assert_called_once()


def test_connection_error_is_redacted(monkeypatch):
    def fail(cfg):
        raise RuntimeError("password=NEVER_ECHO")

    monkeypatch.setattr(checks, "open_connection", fail)
    cfg = Config(_env_file=None, mssql_server="s", mssql_database="ERP")
    with pytest.raises(SetupError) as error:
        checks.check_connection(cfg)
    assert "NEVER_ECHO" not in str(error.value)


@pytest.mark.asyncio
async def test_actual_mcp_subprocess_handshake_without_live_database(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDSHAKE_TEST_PASSWORD", "unused")
    profile = ConnectionProfile(
        name="isolated",
        server="example.invalid",
        database="ERP",
        user="reader",
        credential=CredentialReference(storage="env", key="HANDSHAKE_TEST_PASSWORD"),
    )
    path = tmp_path / "isolated" / "connection.json"
    atomic_write(path, profile_bytes(profile))
    atomic_write(path.parent / "policy.json", policy_bytes(profile))
    cfg = load_profile(path)
    await init_store(cfg.cache_path)
    await write_structural_snapshot(
        cfg.cache_path,
        "ERP",
        StructuralSnapshot(
            tables=[], columns=[], primary_keys=[], foreign_keys=[], indexes=[], objects=[], comments=[]
        ),
    )
    assert await checks._handshake(path, 10) == 12
