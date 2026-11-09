import pytest

from db_semantic_mcp.setup import clients, wizard
from db_semantic_mcp.setup.clients import all_scope_targets, client_targets
from db_semantic_mcp.setup.files import SetupError


@pytest.fixture
def places(tmp_path, monkeypatch):
    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(clients.sys, "platform", "darwin")
    targets = client_targets(home=home, cwd=project)
    monkeypatch.setattr(wizard, "client_targets", lambda: targets)
    return home, project, {t.name: t for t in targets}


def test_every_client_defaults_to_project_scope(places):
    home, project, targets = places
    assert targets["codex"].path == project / ".codex" / "config.toml"
    assert targets["claude-code"].path == project / ".mcp.json"
    assert targets["cursor"].path == project / ".cursor" / "mcp.json"
    assert {t.scope for n, t in targets.items() if n not in {"claude-desktop", "hermes"}} == {"project"}
    assert targets["codex"].label == "Codex（專案）"


def test_global_scope_paths(places):
    home, _, targets = places
    assert targets["codex"].with_scope("global").path == home / ".codex" / "config.toml"
    assert targets["claude-code"].with_scope("global").path == home / ".claude.json"
    assert targets["cursor"].with_scope("global").path == home / ".cursor" / "mcp.json"
    assert targets["cursor"].with_scope("global").label == "Cursor（全域）"


def test_claude_desktop_is_global_only(places):
    _, _, targets = places
    desktop = targets["claude-desktop"]
    assert desktop.scopes == ["global"]
    with pytest.raises(SetupError, match="不支援專案"):
        desktop.with_scope("project")


def test_hermes_is_global_yaml(places, tmp_path, monkeypatch):
    home, _, targets = places
    hermes = targets["hermes"]
    assert (hermes.path, hermes.format, hermes.scopes) == (home / ".hermes" / "config.yaml", "yaml", ["global"])
    assert hermes.label == "Hermes Agent（全域）"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "custom"))
    relocated = {t.name: t for t in client_targets(home=home, cwd=tmp_path)}["hermes"]
    assert relocated.path == tmp_path / "custom" / "config.yaml"


def test_hermes_project_scope_falls_back_to_global(places):
    home, _, _ = places
    messages = []
    (selected,) = wizard._select_clients(["hermes"], None, None, messages.append, "project")
    assert selected.path == home / ".hermes" / "config.yaml"
    assert any("/reload-mcp" in m for m in messages)


def test_interactive_selection_asks_scope_per_client(places):
    home, project, _ = places
    asked = []

    class UI:
        def checkbox(self, label, choices, **kwargs):
            return ["codex", "claude-code", "claude-desktop"]

        def select(self, label, choices, default=None):
            asked.append(label)
            assert default == "project" and choices[0][1] == "project"
            return "global" if label.startswith("Codex") else "project"

    selected = {t.name: t for t in wizard._select_clients(None, None, UI(), lambda _: None)}
    assert asked == ["Codex 的 MCP 要裝在哪裡？", "Claude Code 的 MCP 要裝在哪裡？"]
    assert selected["codex"].path == home / ".codex" / "config.toml"
    assert selected["claude-code"].path == project / ".mcp.json"
    assert selected["claude-desktop"].scope == "global"


def test_text_fallback_scope_defaults_to_project(places):
    _, project, _ = places
    answers = iter(["1", ""])  # Codex, then Enter for the default scope.
    messages = []
    [target] = wizard._select_clients(None, None, lambda _: next(answers), messages.append)
    assert target.path == project / ".codex" / "config.toml"
    assert any("trusted" in m for m in messages)


def test_cli_clients_use_scope_flag_without_prompting(places):
    home, _, _ = places

    def no_prompt(_):
        raise AssertionError("must not prompt")

    messages = []
    selected = wizard._select_clients(["codex", "claude-desktop"], None, no_prompt, messages.append, "global")
    assert [t.path for t in selected][0] == home / ".codex" / "config.toml"
    selected = wizard._select_clients(["claude-desktop"], None, no_prompt, messages.append, "project")
    assert selected[0].scope == "global"
    assert any("只支援全域" in m for m in messages)


def test_cli_clients_default_to_project(places):
    _, project, _ = places
    [target] = wizard._select_clients(["cursor"], None, lambda _: "", lambda _: None)
    assert target.path == project / ".cursor" / "mcp.json"


def test_migration_scans_both_scopes(places):
    home, project, targets = places
    paths = {t.path for t in all_scope_targets(list(targets.values()))}
    assert project / ".codex" / "config.toml" in paths
    assert home / ".codex" / "config.toml" in paths
    assert home / ".claude.json" in paths


def test_scope_cli_flag(monkeypatch):
    from db_semantic_mcp.main import main

    received = {}
    monkeypatch.setattr(wizard, "terminal_setup", lambda **kw: received.update(kw) or 0)
    with pytest.raises(SystemExit):
        main(["setup", "--client", "codex", "--scope", "global"])
    assert received["scope"] == "global"
