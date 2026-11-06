from unittest.mock import MagicMock

import pytest

from db_semantic_mcp.config import Config
from db_semantic_mcp.setup import checks
from db_semantic_mcp.setup.files import SetupError


def test_discovery_verifies_each_candidate_and_filters_failures(monkeypatch):
    cfg = Config(_env_file=None, server="s", database="master", user="u")
    opened = MagicMock()
    cursor = opened.return_value.__enter__.return_value.cursor.return_value
    cursor.fetchall.return_value = [("ERP",), ("CRM",)]
    monkeypatch.setattr(checks, "open_connection", opened)
    seen = []

    def verify(target):
        seen.append(target.database)
        if target.database == "CRM":
            raise SetupError("denied")
        return {"database": target.database, "visible_tables": 0}

    monkeypatch.setattr(checks, "check_connection", verify)
    assert checks.discover_databases(cfg) == ["ERP"]
    assert seen == ["ERP", "CRM"]
    assert "HAS_DBACCESS" in cursor.execute.call_args.args[0]


def test_database_set_keeps_other_results_and_redacts_failures(monkeypatch):
    cfg = Config(_env_file=None, server="s", database="ERP", user="u")
    cfg = cfg.model_copy(update={"profile_version": 3, "databases": ["ERP", "CRM"]})
    monkeypatch.setattr(checks, "_authorized_databases", lambda cfg: ["ERP", "CRM"])

    def verify(target):
        if target.database == "CRM":
            raise SetupError("password=SECRET")
        return {"database": target.database, "visible_tables": 2}

    monkeypatch.setattr(checks, "check_connection", verify)
    result = checks.check_database_set(cfg)
    assert result["databases"]["ERP"]["status"] == "ok"
    assert result["databases"]["CRM"]["status"] == "error"
    assert not result["ok"]
    assert "SECRET" not in str(result)
    with pytest.raises(SetupError):
        checks.check_database_set(cfg, "Forbidden")


def test_selection_retries_all_and_preserves_scope_inputs(monkeypatch):
    from db_semantic_mcp.profiles import ConnectionProfile
    from db_semantic_mcp.setup import sqlserver_wizard as wizard

    profile = ConnectionProfile(name="multi", server="s", database="ERP", user="u")
    monkeypatch.setattr(wizard, "discover_databases", lambda cfg: ["ERP", "CRM"])
    attempts = []

    def verify(cfg):
        attempts.append(cfg.database)
        if len(attempts) == 2:
            raise SetupError("driver secret")
        return {"database": cfg.database}

    monkeypatch.setattr(wizard, "check_connection", verify)

    class UI:
        def confirm(self, *args):
            return False

        def checkbox(self, *args, **kwargs):
            return ["ERP", "CRM"]

        def select(self, *args):
            return "CRM"

    output = []
    updated = wizard.gather_databases(profile, "secret", UI(), output.append)
    assert updated.format_version == 3
    assert updated.databases == ["ERP", "CRM"]
    assert updated.database == "CRM"
    assert attempts == ["ERP", "CRM", "ERP", "CRM"]
    assert "secret" not in str(output)


def test_manual_database_is_verified_before_selection(monkeypatch):
    from db_semantic_mcp.profiles import ConnectionProfile
    from db_semantic_mcp.setup import sqlserver_wizard as wizard

    profile = ConnectionProfile(name="multi", server="s", database="master", user="u")
    monkeypatch.setattr(wizard, "discover_databases", lambda cfg: [])
    verified = []
    monkeypatch.setattr(
        wizard, "check_connection", lambda cfg: verified.append(cfg.database) or {"database": cfg.database}
    )

    class UI:
        def __init__(self):
            self.answers = iter([True, False])

        def confirm(self, *args):
            return next(self.answers)

        def text(self, *args):
            return "Hidden DB"

        def checkbox(self, label, choices, **kwargs):
            assert choices == [("Hidden DB", "Hidden DB")]
            return ["Hidden DB"]

    updated = wizard.gather_databases(profile, "secret", UI(), lambda value: None, existing_databases=[])
    assert updated.databases == ["Hidden DB"]
    assert verified == ["Hidden DB", "Hidden DB"]


def test_v3_failed_handshake_rolls_back_profile_policy_and_credential(tmp_path, monkeypatch):
    from db_semantic_mcp.credentials import CredentialReference
    from db_semantic_mcp.profiles import ConnectionProfile
    from db_semantic_mcp.setup import wizard

    profile = ConnectionProfile(
        name="multi",
        server="s",
        database="ERP",
        databases=["ERP", "CRM"],
        format_version=3,
        user="u",
    )
    directory = tmp_path / "multi"
    originals = {directory / name: None for name in ("connection.json", "policy.json", "credentials.json")}
    draft = wizard.Draft(profile, "secret", directory, originals)
    reference = CredentialReference(storage="env", key="TEST_MULTI_PASSWORD")
    deleted = []
    monkeypatch.setattr(wizard, "store_password", lambda password: reference)
    monkeypatch.setattr(wizard, "delete_password", deleted.append)

    def fail(path):
        raise SetupError("MCP unavailable")

    monkeypatch.setattr(wizard, "check_mcp", fail)
    with pytest.raises(SetupError):
        wizard._apply(draft, lambda label: "", lambda message: None)
    assert all(not path.exists() for path in originals)
    assert deleted == [reference]


def test_database_checkbox_keyboard_preserves_existing_and_selects_second():
    from io import StringIO

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from rich.console import Console

    from db_semantic_mcp.setup.terminal import TerminalUI

    ui = TerminalUI(Console(file=StringIO()))
    prompt = ui._prompt
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[B \r")

        def keyboard(factory, label, **kwargs):
            return prompt(factory, label, input=pipe, output=DummyOutput(), **kwargs)

        ui._prompt = keyboard
        assert ui.checkbox(
            "Database",
            [("ERP", "ERP"), ("CRM", "CRM")],
            default=["ERP"],
            empty_message="請至少勾選一個 Database。",
        ) == ["ERP", "CRM"]


def test_v2_case_insensitive_database_name_remains_compatible(monkeypatch):
    cfg = Config(_env_file=None, server="s", database="erp", user="u")
    opened = MagicMock()
    cursor = opened.return_value.__enter__.return_value.cursor.return_value
    cursor.fetchone.side_effect = [("ERP",), (4,)]
    monkeypatch.setattr(checks, "open_connection", opened)
    assert checks.check_connection(cfg)["database"] == "ERP"
    assert cursor.execute.call_count == 2


@pytest.mark.parametrize("identity", [0, 1])
def test_v3_canonical_name_requires_server_identity_proof(monkeypatch, identity):
    cfg = Config(_env_file=None, server="s", database="erp", user="u")
    cfg = cfg.model_copy(update={"profile_version": 3})
    opened = MagicMock()
    cursor = opened.return_value.__enter__.return_value.cursor.return_value
    cursor.fetchone.side_effect = [("ERP",), (identity,), (1,), (1,), (4,)]
    monkeypatch.setattr(checks, "open_connection", opened)
    if identity:
        assert checks.check_connection(cfg)["database"] == "ERP"
    else:
        with pytest.raises(SetupError, match="不一致"):
            checks.check_connection(cfg)
    assert cursor.execute.call_args_list[1].args == ("SELECT CASE WHEN DB_ID(%s) = DB_ID() THEN 1 ELSE 0 END", ("erp",))


@pytest.mark.parametrize("permission", [0, None])
def test_v3_requires_database_metadata_permission(monkeypatch, permission):
    cfg = Config(_env_file=None, server="s", database="ERP", user="u")
    cfg = cfg.model_copy(update={"profile_version": 3})
    opened = MagicMock()
    cursor = opened.return_value.__enter__.return_value.cursor.return_value
    cursor.fetchone.side_effect = [("ERP",), (permission,)]
    monkeypatch.setattr(checks, "open_connection", opened)
    with pytest.raises(SetupError, match="VIEW DEFINITION"):
        checks.check_connection(cfg)
    assert cursor.execute.call_count == 2


@pytest.mark.parametrize("permission", [0, None])
def test_v3_refuses_incomplete_security_policy_catalog(monkeypatch, permission):
    cfg = Config(_env_file=None, server="s", database="ERP", user="u")
    cfg = cfg.model_copy(update={"profile_version": 3})
    opened = MagicMock()
    cursor = opened.return_value.__enter__.return_value.cursor.return_value
    cursor.fetchone.side_effect = [("ERP",), (1,), (permission,)]
    monkeypatch.setattr(checks, "open_connection", opened)
    with pytest.raises(SetupError, match="security policy metadata"):
        checks.check_connection(cfg)
    assert cursor.execute.call_count == 3
    assert cursor.execute.call_args.args == (
        "SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'ALTER ANY SECURITY POLICY')",
    )
    cursor.close.assert_called_once()
