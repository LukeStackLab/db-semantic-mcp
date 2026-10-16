import json

import pytest

from db_semantic_mcp.credentials import CredentialError, CredentialReference
from db_semantic_mcp.profiles import load_profile
from db_semantic_mcp.setup import wizard
from db_semantic_mcp.setup.files import SetupError


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    client = tmp_path / "client.json"
    passwords = []

    def store(password):
        key = f"WIZARD_PASSWORD_{len(passwords)}"
        passwords.append(password)
        monkeypatch.setenv(key, password)
        return CredentialReference(storage="env", key=key)

    monkeypatch.setattr(wizard, "store_password", store)
    monkeypatch.setattr(wizard, "check_connection", lambda cfg: {"visible_tables": 5})
    monkeypatch.setattr(wizard, "check_mcp", lambda path: 12)
    return root, client, passwords


def run(environment, answers, **kwargs):
    root, client, _ = environment
    iterator = iter(answers)
    output = []
    status = wizard.run_setup(
        root=root,
        client="cursor",
        client_config=client,
        input_fn=lambda prompt: "sqlserver" if "資料庫類型" in prompt else next(iterator),
        password_fn=lambda prompt: "NEVER_ECHO_PASSWORD",
        emit=output.append,
        **kwargs,
    )
    return status, output


def test_five_independent_profiles_and_password_redaction(environment):
    root, client, passwords = environment
    client.write_text('{"mcpServers":{"existing":{"command":"keep"}},"theme":"dark"}')
    answers = []
    for n in range(5):
        answers.extend([f"erp{n}", f"sql{n}", "", "ERP", "reader", "n", "", "y" if n < 4 else "n"])
    answers.append("")
    status, output = run(environment, answers)
    assert status == 0
    data = json.loads(client.read_text())
    assert len(data["mcpServers"]) == 6
    assert data["mcpServers"]["existing"]["command"] == "keep"
    assert data["theme"] == "dark"
    configs = [load_profile(root / f"erp{n}" / "connection.json") for n in range(5)]
    assert len({cfg.cache_path for cfg in configs}) == 5
    assert len({cfg.policy_file for cfg in configs}) == 5
    assert all(cfg.max_response_bytes == 65536 for cfg in configs)
    assert all(cfg.policy_profile == "readonly" for cfg in configs)
    assert len(passwords) == 5
    all_text = "\n".join(output) + client.read_text() + "".join(p.read_text() for p in root.rglob("connection.json"))
    assert "NEVER_ECHO_PASSWORD" not in all_text
    assert len(list(client.parent.glob("client.json.bak-*"))) == 1


def test_cancel_writes_nothing(environment):
    root, client, passwords = environment
    status, _ = run(environment, ["erp", "sql", "", "ERP", "reader", "n", "", "n", "n"])
    assert status == 0
    assert not root.exists()
    assert not client.exists()
    assert passwords == []


def test_setup_can_enable_execution_recording_and_edit_keeps_it(environment):
    status, _ = run(environment, ["erp", "sql", "", "ERP", "reader", "n", "y", "n", ""])
    assert status == 0
    path = environment[0] / "erp" / "connection.json"
    assert load_profile(path).execution_recording_enabled is True
    assert run(environment, ["erp", "y", "", "", "", "", "", "", "n", ""])[0] == 0
    assert load_profile(path).execution_recording_enabled is True
    assert json.loads((environment[0] / "erp" / "policy.json").read_text())["active_profile"] == "readonly"


def test_setup_tool_profile_is_saved(environment):
    status, _ = run(environment, ["erp", "sql", "", "ERP", "reader", "n", "", "n", ""], tool_profile="explore")
    assert status == 0
    assert load_profile(environment[0] / "erp" / "connection.json").tool_profile == "explore"


def test_connection_retry_keeps_defaults_and_does_not_repeat_permission(environment, monkeypatch):
    attempts = []

    def check(cfg):
        attempts.append(cfg.server)
        if len(attempts) == 1:
            raise SetupError("連線失敗")
        return {"visible_tables": 1}

    monkeypatch.setattr(wizard, "check_connection", check)
    status, _ = run(
        environment,
        [
            "erp",
            "bad-host",
            "",
            "ERP",
            "reader",
            "n",
            "",
            "y",
            "good-host",
            "",
            "",
            "",
            "",
            "n",
            "",
        ],
    )
    assert status == 0
    assert attempts == ["bad-host", "good-host"]
    assert load_profile(environment[0] / "erp" / "connection.json").server == "good-host"


def test_failed_handshake_does_not_register_profile(environment, monkeypatch):
    def fail(path):
        raise SetupError("MCP 握手失敗")

    monkeypatch.setattr(wizard, "check_mcp", fail)
    status, _ = run(environment, ["erp", "sql", "", "ERP", "reader", "n", "", "n", ""])
    assert status == 1
    assert not environment[1].exists()
    assert not (environment[0] / "erp" / "connection.json").exists()
    assert not (environment[0] / "erp" / "policy.json").exists()


def test_failed_update_restores_existing_profile(environment, monkeypatch):
    answers = ["erp", "sql", "", "ERP", "reader", "n", "", "n", ""]
    assert run(environment, answers)[0] == 0
    path = environment[0] / "erp" / "connection.json"
    original = path.read_bytes()
    client_original = environment[1].read_bytes()

    def fail(path):
        raise SetupError("MCP 握手失敗")

    monkeypatch.setattr(wizard, "check_mcp", fail)
    status, _ = run(environment, ["erp", "y", "changed", "", "", "", "y", "", "n", ""])
    assert status == 1
    assert path.read_bytes() == original
    assert environment[1].read_bytes() == client_original


def test_concurrent_client_change_rolls_back_profiles(environment, monkeypatch):
    client = environment[1]

    def handshake(path):
        client.write_text('{"external":"change"}')
        return 12

    monkeypatch.setattr(wizard, "check_mcp", handshake)
    with pytest.raises(SetupError, match="其他程序"):
        run(environment, ["erp", "sql", "", "ERP", "reader", "n", "", "n", ""])
    assert json.loads(client.read_text()) == {"external": "change"}
    assert not (environment[0] / "erp" / "connection.json").exists()


def test_keyboard_interrupt_during_questions_writes_nothing(environment):
    def cancel(prompt):
        raise KeyboardInterrupt

    output = []
    status = wizard.run_setup(
        root=environment[0],
        client="cursor",
        client_config=environment[1],
        input_fn=cancel,
        emit=output.append,
    )
    assert status == 130
    assert not environment[1].exists()


def test_keyring_failure_does_not_silently_write_plaintext(environment, monkeypatch):
    def fail(password):
        raise CredentialError("不可用")

    monkeypatch.setattr(wizard, "store_password", fail)
    status, _ = run(environment, ["erp", "sql", "", "ERP", "reader", "n", "", "n", "", "n"])
    assert status == 1
    assert not environment[1].exists()
    assert not (environment[0] / "erp" / "credentials.json").exists()


def test_os_error_rolls_back_all_unregistered_profiles(environment, monkeypatch):
    apply = wizard._apply

    def apply_with_failure(draft, *args):
        if draft.profile.name == "second":
            raise OSError("disk full")
        return apply(draft, *args)

    monkeypatch.setattr(wizard, "_apply", apply_with_failure)
    with pytest.raises(OSError):
        run(
            environment,
            [
                "first",
                "sql",
                "",
                "ERP",
                "reader",
                "n",
                "",
                "y",
                "second",
                "sql",
                "",
                "ERP",
                "reader",
                "n",
                "",
                "n",
                "",
            ],
        )
    assert not environment[1].exists()
    assert not (environment[0] / "first" / "connection.json").exists()
    assert not (environment[0] / "first" / "policy.json").exists()


def test_update_preserves_custom_policy_scope_and_tighter_caps(environment):
    assert run(environment, ["erp", "sql", "", "ERP", "reader", "n", "", "n", ""])[0] == 0
    policy_path = environment[0] / "erp" / "policy.json"
    policy = json.loads(policy_path.read_text())
    policy["profiles"]["readonly"]["constraints"]["max_rows_returned"] = 10
    policy["profiles"]["readonly"]["scope"]["denied_tables"] = ["Payroll"]
    policy_path.write_text(json.dumps(policy))
    assert run(environment, ["erp", "y", "", "", "", "", "n", "", "n", ""])[0] == 0
    updated = json.loads(policy_path.read_text())["profiles"]["readonly"]
    assert updated["constraints"]["max_rows_returned"] == 10
    assert updated["scope"]["denied_tables"] == ["Payroll"]


def test_post_handshake_edit_is_not_registered_and_other_files_are_restored(environment, monkeypatch):
    def handshake(path):
        if path.parent.name == "second":
            (environment[0] / "first" / "connection.json").write_text("external-change")
        return 12

    monkeypatch.setattr(wizard, "check_mcp", handshake)
    with pytest.raises(SetupError, match="驗證後"):
        run(
            environment,
            [
                "first",
                "sql",
                "",
                "ERP",
                "reader",
                "n",
                "",
                "y",
                "second",
                "sql",
                "",
                "ERP",
                "reader",
                "n",
                "",
                "n",
                "",
            ],
        )
    assert not environment[1].exists()
    assert (environment[0] / "first" / "connection.json").read_text() == "external-change"
    assert not (environment[0] / "first" / "policy.json").exists()
    assert not (environment[0] / "second" / "connection.json").exists()


def test_update_does_not_reenable_disabled_operation(environment):
    assert run(environment, ["erp", "sql", "", "ERP", "reader", "y", "", "n", ""])[0] == 0
    policy_path = environment[0] / "erp" / "policy.json"
    policy = json.loads(policy_path.read_text())
    policy["profiles"]["read_write_safe"]["operations"]["insert"] = False
    policy_path.write_text(json.dumps(policy))
    assert run(environment, ["erp", "y", "", "", "", "", "", "", "n", ""])[0] == 0
    updated = json.loads(policy_path.read_text())["profiles"]["read_write_safe"]
    assert updated["operations"]["insert"] is False
    assert updated["operations"]["update"] is True


def test_explicit_permission_change_enables_only_limited_writes(environment):
    assert run(environment, ["erp", "sql", "", "ERP", "reader", "n", "", "n", ""])[0] == 0
    assert run(environment, ["erp", "y", "", "", "", "", "y", "", "n", ""])[0] == 0
    cfg = load_profile(environment[0] / "erp" / "connection.json")
    from db_semantic_mcp.services.policy_service import PolicyService

    policy = PolicyService(cfg).current_policy()
    assert policy.operations.insert is True
    assert policy.operations.update is True
    assert policy.operations.delete is False
    assert policy.operations.execute is False


def test_stale_draft_does_not_delete_another_installers_identical_policy(environment):
    from db_semantic_mcp.profiles import ConnectionProfile

    directory = environment[0] / "erp"
    stale = wizard.Draft(
        ConnectionProfile(name="erp", server="sql", database="ERP", user="reader"),
        "unused",
        directory,
        {directory / name: None for name in ("connection.json", "policy.json", "credentials.json")},
    )
    assert run(environment, ["erp", "sql", "", "ERP", "reader", "n", "", "n", ""])[0] == 0
    existing = {p: p.read_bytes() for p in directory.glob("*.json")}
    with pytest.raises(SetupError, match="其他程序"):
        wizard._apply(stale, lambda prompt: "n", lambda message: None)
    for path, data in existing.items():
        assert path.read_bytes() == data


def test_interrupt_after_replace_restores_the_owned_file(environment, monkeypatch):
    assert run(environment, ["erp", "sql", "", "ERP", "reader", "n", "", "n", ""])[0] == 0
    directory = environment[0] / "erp"
    before = {p: p.read_bytes() for p in directory.glob("*.json")}
    client_before = environment[1].read_bytes()
    from db_semantic_mcp.setup import files

    real_replace = files.os.replace
    interrupted = False

    def interrupt_once(source, target):
        nonlocal interrupted
        real_replace(source, target)
        if target.name == "policy.json" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    monkeypatch.setattr(files.os, "replace", interrupt_once)
    assert run(environment, ["erp", "y", "", "", "", "", "y", "", "n", ""])[0] == 130
    assert environment[1].read_bytes() == client_before
    for path, data in before.items():
        assert path.read_bytes() == data


@pytest.mark.skipif(wizard.os.name == "nt", reason="POSIX credential-file fallback")
def test_finalize_preserves_newer_configuration_and_identical_credentials(environment, monkeypatch):
    store = wizard.store_password

    def unavailable(password):
        raise CredentialError("keyring unavailable")

    monkeypatch.setattr(wizard, "store_password", unavailable)
    assert run(environment, ["erp", "sql", "", "ERP", "reader", "n", "", "n", "", "y"])[0] == 0
    directory = environment[0] / "erp"
    old_credential = json.loads((directory / "connection.json").read_text())["credential"]
    secret_bytes = (directory / "credentials.json").read_bytes()
    monkeypatch.setattr(wizard, "store_password", store)
    real_register = wizard.register_servers

    def register_then_concurrent_update(*args, **kwargs):
        backup = real_register(*args, **kwargs)
        from db_semantic_mcp.setup.files import atomic_write

        atomic_write(directory / "credentials.json", secret_bytes)
        updated = json.loads((directory / "connection.json").read_text())
        updated["credential"] = old_credential
        atomic_write(directory / "connection.json", json.dumps(updated).encode())
        return backup

    monkeypatch.setattr(wizard, "register_servers", register_then_concurrent_update)
    assert run(environment, ["erp", "y", "", "", "", "", "n", "", "n", ""])[0] == 0
    assert (directory / "credentials.json").read_bytes() == secret_bytes
    assert load_profile(directory / "connection.json").password == "NEVER_ECHO_PASSWORD"
