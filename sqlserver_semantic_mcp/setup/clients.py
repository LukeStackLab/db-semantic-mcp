"""MCP client adapters with preserving merges and explicit conflict checks."""

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import tomlkit

from .files import SetupError, atomic_write


@dataclass(frozen=True)
class ClientTarget:
    name: str
    label: str
    path: Path
    format: str
    detected: bool = False


def client_targets(home: Path | None = None, cwd: Path | None = None) -> list[ClientTarget]:
    home, cwd = home or Path.home(), cwd or Path.cwd()
    codex_root = Path(os.environ.get("CODEX_HOME", str(home / ".codex")))
    targets = [
        ClientTarget(
            "codex",
            "Codex",
            codex_root / "config.toml",
            "toml",
            codex_root.exists() or shutil.which("codex") is not None,
        ),
        ClientTarget(
            "claude-code",
            "Claude Code（目前專案）",
            cwd / ".mcp.json",
            "json",
            (cwd / ".mcp.json").exists() or shutil.which("claude") is not None,
        ),
        ClientTarget(
            "cursor",
            "Cursor（全域）",
            home / ".cursor" / "mcp.json",
            "json",
            (home / ".cursor").exists() or shutil.which("cursor") is not None,
        ),
    ]
    if sys.platform == "darwin":
        desktop = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "win32":
        desktop = (
            Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))) / "Claude" / "claude_desktop_config.json"
        )
    else:
        desktop = None
    if desktop:
        targets.append(ClientTarget("claude-desktop", "Claude Desktop", desktop, "json", desktop.parent.exists()))
    return targets


def server_entry(profile_path: Path) -> dict:
    # Do not resolve the Python symlink: that would leave a virtualenv.
    return {
        "command": os.path.abspath(sys.executable),
        "args": ["-I", "-m", "sqlserver_semantic_mcp.main", "serve", "--config", str(profile_path.absolute())],
    }


def parse_client(target: ClientTarget, original: bytes | None):
    try:
        text = original.decode("utf-8") if original is not None else ""
        document = tomlkit.parse(text) if target.format == "toml" else json.loads(text or "{}")
    except (ValueError, UnicodeDecodeError):
        raise SetupError(f"{target.label} 設定格式錯誤，未修改：{target.path}") from None
    if not isinstance(document, dict):
        raise SetupError(f"{target.label} 設定必須為物件。")
    key = "mcp_servers" if target.format == "toml" else "mcpServers"
    if key in document and not isinstance(document[key], dict):
        raise SetupError(f"{key} 必須為物件，未修改設定。")
    return document, key


def merge_servers(target: ClientTarget, original: bytes | None, entries: dict[str, dict]) -> bytes:
    document, key = parse_client(target, original)
    if key not in document:
        document[key] = tomlkit.table() if target.format == "toml" else {}
    servers = document[key]
    for name, entry in entries.items():
        if name in servers:
            current = servers[name]
            # An existing setup-owned entry may be refreshed; a different
            # server under the same name must never be silently replaced.
            if not isinstance(current, dict) or current.get("args") != entry["args"]:
                raise SetupError(f"MCP 名稱 {name} 已被其他設定使用，請改用不同名稱。")
            current["command"] = entry["command"]
            current["args"] = entry["args"]
        else:
            servers[name] = tomlkit.item(entry) if target.format == "toml" else entry
    text = (
        tomlkit.dumps(document)
        if target.format == "toml"
        else json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    )
    return text.encode("utf-8")


def register_servers(
    target: ClientTarget,
    entries: dict[str, dict],
    original: bytes | None,
    *,
    receipt: dict | None = None,
) -> Path | None:
    data = merge_servers(target, original, entries)
    return atomic_write(target.path, data, expected=original, backup=True, receipt=receipt)
