"""Exercise actual key bindings without a user's terminal or configuration."""

from io import StringIO

import pytest
import questionary
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from db_semantic_mcp.setup.terminal import TerminalUI


@pytest.mark.parametrize(
    "kind,keys,expected",
    [
        ("select", "\x1b[B\r", "oracle"),
        ("checkbox", " \x1b[B \r", ["sqlserver", "oracle"]),
        ("checkbox", "\r \r", ["sqlserver"]),  # Empty Enter must stay on the question.
        ("select", "\x1b[B\x1b[A\r", "sqlserver"),
    ],
)
def test_real_arrow_space_enter_bindings(monkeypatch, kind, keys, expected):
    factory = getattr(questionary, kind)
    with create_pipe_input() as pipe:
        monkeypatch.setattr(questionary, kind, lambda *a, **kw: factory(*a, input=pipe, output=DummyOutput(), **kw))
        pipe.send_text(keys)
        ui = TerminalUI()
        choices = [("SQL Server", "sqlserver"), ("Oracle", "oracle")]
        assert getattr(ui, kind)("選擇", choices) == expected


def test_ctrl_c_propagates_for_transaction_cleanup(monkeypatch):
    factory = questionary.select
    with create_pipe_input() as pipe:
        monkeypatch.setattr(questionary, "select", lambda *a, **kw: factory(*a, input=pipe, output=DummyOutput(), **kw))
        pipe.send_text("\x03")
        with pytest.raises(KeyboardInterrupt):
            TerminalUI().select("選擇", [("A", "a")])


def test_rich_display_preserves_literal_values_and_no_color(monkeypatch, tmp_path):
    from types import SimpleNamespace

    monkeypatch.setenv("NO_COLOR", "1")
    stream = StringIO()
    ui = TerminalUI(Console(file=stream, width=72))
    ui.welcome()
    ui.stage(3, "確認與套用")
    profile = SimpleNamespace(
        name="erp",
        database_type="sqlserver",
        database="[red]literal[/red]",
        server="host",
        port=1433,
        readonly=True,
        max_response_bytes=65536,
        tool_profile="all",
        execution_recording_enabled=False,
    )
    ui.summary([SimpleNamespace(profile=profile)], [SimpleNamespace(label="Codex", path=tmp_path / "config.toml")])
    ui.emit("完成 [red]literal[/red]")
    rendered = stream.getvalue()
    assert "[red]literal[/red]" in rendered
    assert "03 / 04" in rendered
    assert "Space" in rendered
    assert "\x1b[" not in rendered
