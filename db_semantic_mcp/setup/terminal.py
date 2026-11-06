"""Keyboard-first installer presentation, separate from configuration writes."""

import os

import questionary
from prompt_toolkit.output import ColorDepth
from questionary import Choice, Style
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

STYLE = Style(
    [
        ("qmark", "fg:#22d3ee bold"),
        ("question", "bold"),
        ("answer", "fg:#34d399 bold"),
        ("pointer", "fg:#c084fc bold"),
        ("highlighted", "fg:#22d3ee bold"),
        ("selected", "fg:#34d399"),
        ("instruction", "fg:#94a3b8"),
        ("text", ""),
    ]
)


class TerminalUI:
    def __init__(self, console=None):
        self.console = console or Console(highlight=False)
        self.style = Style([]) if "NO_COLOR" in os.environ else STYLE

    def _prompt(self, factory, label, **kwargs):
        # Let interrupts reach the wizard's transaction cleanup.
        if "NO_COLOR" in os.environ:
            kwargs["color_depth"] = ColorDepth.DEPTH_1_BIT
        return factory(label, style=self.style, qmark="◆", **kwargs).unsafe_ask()

    def __call__(self, label):
        return self.text(label)

    def text(self, label, default=""):
        return self._prompt(questionary.text, label, default=default)

    def password(self, label):
        return self._prompt(questionary.password, label)

    def select(self, label, choices, default=None):
        return self._prompt(
            questionary.select,
            label,
            choices=[Choice(title, value=value) for title, value in choices],
            default=default,
            pointer="❯",
            instruction="↑↓ 移動 · Enter 確認",
        )

    def checkbox(self, label, choices, default=None, empty_message="請至少勾選一個 Client。"):
        return self._prompt(
            questionary.checkbox,
            label,
            choices=[Choice(title, value=value, checked=value in (default or [])) for title, value in choices],
            pointer="❯",
            instruction="↑↓ 移動 · Space 勾選 · Enter 確認",
            validate=lambda values: True if values else empty_message,
        )

    def confirm(self, label, default=False):
        return self.select(label, [("是，繼續", True), ("否", False)], default)

    def welcome(self):
        title = Text("DB SEMANTIC MCP", style="bold cyan")
        title.append("\n資料庫連線安裝精靈", style="bold")
        title.append("\n\n選擇 Client → 設定連線 → 確認套用 → 完成", style="magenta")
        title.append("\n↑↓ 選擇  ·  Space 多選  ·  Enter 確認  ·  Ctrl+C 取消", style="dim")
        self.console.print(Panel(title, border_style="cyan", padding=(1, 2)))

    def stage(self, number, title):
        self.console.print()
        self.console.rule(Text(f" {number:02d} / 04  {title} ", style="bold cyan"), align="left", style="magenta")

    def summary(self, drafts, targets):
        table = Table(title="即將套用的連線", border_style="cyan", header_style="bold cyan", show_lines=True)
        for title in ("MCP 名稱", "資料庫", "端點", "存取權限"):
            table.add_column(title, overflow="fold")
        for draft in drafts:
            p = draft.profile
            table.add_row(
                Text(p.name, style="bold"),
                Text(
                    f"{p.database_type}\n"
                    + "、".join(getattr(p, "databases", None) or [p.database])
                    + f"\n啟動：{p.database}"
                ),
                Text(f"{p.server}:{p.port}" if p.server else "TNS / DSN"),
                Text("唯讀" if p.readonly else "受限寫入", style="green" if p.readonly else "yellow"),
            )
        self.console.print(table)
        for draft in drafts:
            p = draft.profile
            self.console.print(
                Text(
                    f"{p.name} · 輸出上限 {p.max_response_bytes} bytes · 工具集合 {p.tool_profile}"
                    f" · SQL 執行紀錄：{'開啟' if p.execution_recording_enabled else '關閉'}",
                    style="dim",
                )
            )
        clients = Text()
        for target in targets:
            clients.append(f"✓ {target.label}\n", style="bold green")
            clients.append(f"  {target.path}\n", style="dim")
        self.console.print(Panel(clients, title="MCP Clients", border_style="magenta"))

    def status(self, label):
        return self.console.status(Text(label, style="cyan"), spinner="dots")

    def emit(self, message):
        # Configuration values are literal text, never terminal markup.
        style = "green" if message.startswith(("完成", "已註冊", "連線成功")) else ""
        if any(word in message for word in ("未套用", "無法", "失敗", "已取消")):
            style = "yellow"
        self.console.print(Text(message, style=style))
