"""MCP client adapters with preserving merges and explicit conflict checks."""

import json
import os
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import tomlkit

from .files import SetupError, atomic_write

SCOPE_LABELS = {"project": "專案", "global": "全域"}


@dataclass(frozen=True)
class ClientTarget:
    name: str
    label: str
    path: Path
    format: str
    detected: bool = False
    scope: str | None = None
    # Every install location this client supports, in (scope, path) form.
    scope_paths: tuple[tuple[str, Path], ...] = ()
    base_label: str = ""

    @property
    def scopes(self) -> list[str]:
        return [scope for scope, _ in self.scope_paths]

    def with_scope(self, scope: str) -> "ClientTarget":
        paths = dict(self.scope_paths)
        if scope not in paths:
            raise SetupError(f"{self.base_label or self.label} 不支援{SCOPE_LABELS.get(scope, scope)}安裝。")
        base = self.base_label or self.label
        return replace(self, path=paths[scope], scope=scope, label=f"{base}（{SCOPE_LABELS[scope]}）")


def _target(name, label, fmt, detected, paths, default="project") -> ClientTarget:
    scope_paths = tuple(paths)
    scope = default if default in dict(scope_paths) else scope_paths[0][0]
    target = ClientTarget(name, label, scope_paths[0][1], fmt, detected, scope, scope_paths, label)
    return target.with_scope(scope)


def client_targets(home: Path | None = None, cwd: Path | None = None) -> list[ClientTarget]:
    """Clients default to the current project; ``with_scope`` switches location."""
    home, cwd = home or Path.home(), cwd or Path.cwd()
    codex_root = Path(os.environ.get("CODEX_HOME", str(home / ".codex")))
    targets = [
        _target(
            "codex",
            "Codex",
            "toml",
            codex_root.exists() or (cwd / ".codex").exists() or shutil.which("codex") is not None,
            [("project", cwd / ".codex" / "config.toml"), ("global", codex_root / "config.toml")],
        ),
        _target(
            "claude-code",
            "Claude Code",
            "json",
            (cwd / ".mcp.json").exists() or shutil.which("claude") is not None,
            # User scope lives in ~/.claude.json under the top-level mcpServers.
            [("project", cwd / ".mcp.json"), ("global", home / ".claude.json")],
        ),
        _target(
            "cursor",
            "Cursor",
            "json",
            (home / ".cursor").exists() or (cwd / ".cursor").exists() or shutil.which("cursor") is not None,
            [("project", cwd / ".cursor" / "mcp.json"), ("global", home / ".cursor" / "mcp.json")],
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
        targets.append(
            _target("claude-desktop", "Claude Desktop", "json", desktop.parent.exists(), [("global", desktop)])
        )
    return targets


def all_scope_targets(targets: list[ClientTarget] | None = None) -> list[ClientTarget]:
    """Expand each client into one target per supported location, without duplicates."""
    expanded, seen = [], set()
    for target in targets if targets is not None else client_targets():
        for scoped in [target.with_scope(s) for s in target.scopes] or [target]:
            key = scoped.path.absolute()
            if key not in seen:
                seen.add(key)
                expanded.append(scoped)
    return expanded


def server_entry(profile_path: Path) -> dict:
    # Do not resolve the Python symlink: that would leave a virtualenv.
    return {
        "command": os.path.abspath(sys.executable),
        "args": ["-I", "-m", "db_semantic_mcp.main", "serve", "--config", str(profile_path.absolute())],
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
