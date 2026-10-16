"""Explicit v1 migration. Preview never reads credentials or writes files."""

import json
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit
from pydantic import ValidationError

from .. import credentials
from ..credentials import CredentialReference
from ..policy.models import PolicyFile
from ..policy.scope import parse_scope_entry
from ..profiles import ConnectionProfile, default_config_root, profile_bytes
from .checks import check_mcp
from .clients import ClientTarget, client_targets, parse_client, server_entry
from .files import SetupError, atomic_write, read_snapshot, restore_owned, snapshot_with_identity

LEGACY_SERVICE = "sqlserver-semantic-mcp"
LEGACY_MODULE = "sqlserver_semantic_mcp.main"


@dataclass
class LegacyProfile:
    source: Path
    destination: Path
    profile: ConnectionProfile
    originals: dict[Path, tuple[bytes, tuple]]
    policy: bytes


@dataclass
class MigrationPlan:
    profiles: list[LegacyProfile]
    clients: list[tuple[ClientTarget, bytes, bytes]] = field(default_factory=list)

    def preview(self):
        return {
            "apply": False,
            "profiles": [
                {
                    "name": p.profile.name,
                    "source": str(p.source),
                    "destination": str(p.destination),
                    "credential_storage": p.profile.credential.storage,
                }
                for p in self.profiles
            ],
            "clients": [str(target.path) for target, _, _ in self.clients],
            "cache": "Not migrated; fresh isolated cache",
            "originals": "Preserved; credentials copied only with --apply",
        }


def default_legacy_root():
    return default_config_root().parent.parent / LEGACY_SERVICE / "connections"


def _parse_legacy(path, destination):
    raw, identity = snapshot_with_identity(path)
    policy_path = path.parent / "policy.json"
    policy_raw, policy_identity = snapshot_with_identity(policy_path)
    if raw is None or policy_raw is None:
        raise SetupError("舊配置缺少 connection.json 或 policy.json。")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("format_version", 1) != 1:
            raise ValueError("Legacy v1 required")
        # v1 was flat. Do not accept v2/provider/source fields as a legacy grant.
        allowed = set(ConnectionProfile.model_fields) - {"database_type", "oracle", "sources"}
        if set(data) - (allowed | {"budget"}):
            raise ValueError("Unknown legacy field")
        data.pop("budget", None)
        data["format_version"] = 2
        profile = ConnectionProfile.model_validate(data)
        if profile.name != path.parent.name or profile.credential is None:
            raise ValueError("Invalid name or credential reference")
        policies = PolicyFile.model_validate_json(policy_raw)
        active = policies.profiles[policies.active_profile]
        expected = profile.policy()
        if policies.active_profile != active.profile_name or active.profile_name != expected.profile_name:
            raise ValueError("Policy mode mismatch")
        permitted = expected.operations.model_dump()
        if any(value and not permitted[name] for name, value in active.operations.model_dump().items()):
            raise ValueError("Unsafe operations")
        if active.constraints.allow_multi_statement or not active.constraints.require_where_for_update:
            raise ValueError("Unsafe constraints")
        for policy in policies.profiles.values():
            if policy.scope.sources:
                raise ValueError("Legacy SQL Server sources unsupported")
            for name in [*policy.scope.allowed_tables, *policy.scope.denied_tables]:
                parse_scope_entry(name)
        # Preserve the original policy bytes, all caps and restrictions exactly.
    except (ValueError, TypeError, KeyError, ValidationError):
        raise SetupError("舊版配置或 policy 無效，拒絕遷移；未放寬權限。") from None
    return LegacyProfile(
        path,
        destination / profile.name / "connection.json",
        profile,
        {path: (raw, identity), policy_path: (policy_raw, policy_identity)},
        policy_raw,
    )


def _rewrite_client(target, original, profiles):
    document, key = parse_client(target, original)
    servers = document.get(key, {})
    changed = False
    for item in profiles:
        name = item.profile.name
        if name not in servers:
            continue
        entry = servers[name]
        expected = ["-I", "-m", LEGACY_MODULE, "serve", "--config", str(item.source)]
        if (
            not isinstance(entry, dict)
            or entry.get("args") != expected
            or not isinstance(entry.get("command"), str)
            or not Path(entry["command"]).is_absolute()
        ):
            raise SetupError(f"Client 的 {name} 不屬於指定舊配置，拒絕覆寫。")
        replacement = server_entry(item.destination)
        entry["command"], entry["args"] = replacement["command"], replacement["args"]
        changed = True
    if not changed:
        return None
    return (
        tomlkit.dumps(document)
        if target.format == "toml"
        else json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    ).encode()


def preview_migration(*, legacy_root=None, destination_root=None, clients=None):
    legacy_root = (legacy_root or default_legacy_root()).expanduser().absolute()
    destination_root = (destination_root or default_config_root()).expanduser().absolute()
    if (
        legacy_root == destination_root
        or legacy_root.is_relative_to(destination_root)
        or destination_root.is_relative_to(legacy_root)
    ):
        raise SetupError("新舊配置目錄必須分開。")
    # read_snapshot checks all ancestors, including a symlinked root.
    direct = legacy_root / "connection.json"
    if read_snapshot(direct) is not None:
        paths = [direct]
    elif legacy_root.exists():
        paths = sorted(legacy_root.glob("*/connection.json"))
    else:
        paths = []
    if not paths:
        raise SetupError("找不到可遷移的舊版配置。")
    profiles = [_parse_legacy(path, destination_root) for path in paths]
    names = [item.profile.name.casefold() for item in profiles]
    if len(names) != len(set(names)):
        raise SetupError("舊配置名稱大小寫衝突。")
    for item in profiles:
        read_snapshot(item.destination)
        if item.destination.parent.exists() or (
            destination_root.exists()
            and any(p.name.casefold() == item.profile.name.casefold() for p in destination_root.iterdir())
        ):
            raise SetupError("目的配置已存在，拒絕覆寫。")
    changes = []
    for target in clients if clients is not None else client_targets():
        original = read_snapshot(target.path)
        if original is None:
            continue
        rewritten = _rewrite_client(target, original, profiles)
        if rewritten is not None:
            changes.append((target, original, rewritten))
    return MigrationPlan(profiles, changes)


def _legacy_password(profile, directory):
    reference = profile.credential
    if reference.storage != "keyring":
        return credentials.read_password(reference, directory)
    try:
        password = credentials._backend().get_password(LEGACY_SERVICE, reference.key)
    except Exception:  # noqa: BLE001 (redact driver/credential failures)
        raise SetupError("無法讀取舊版系統憑證。") from None
    if not isinstance(password, str) or not password:
        raise SetupError("找不到舊版系統憑證。")
    return password


def _verify_originals(plan):
    for item in plan.profiles:
        for path, original in item.originals.items():
            if snapshot_with_identity(path) != original:
                raise SetupError("舊配置在預覽後已變更，請重新遷移。")


def apply_migration(plan):
    written = []
    new_references = []
    backups = []

    def write(path, data, expected=None, backup=False):
        receipt = {}
        # Track before the write: a post-replace exception still needs rollback.
        written.append((path, expected, data, receipt))
        result = atomic_write(path, data, expected=expected, backup=backup, receipt=receipt)
        if result:
            backups.append(str(result))

    try:
        _verify_originals(plan)
        for item in plan.profiles:
            directory = item.destination.parent
            read_snapshot(item.destination)
            # Exclusive directory creation also rejects a concurrent migration.
            directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                raise SetupError("目的配置已存在，拒絕覆寫。") from None
            reference = item.profile.credential
            if reference.storage == "env":
                new_reference = reference
            else:
                password = _legacy_password(item.profile, item.source.parent)
                if reference.storage == "keyring":
                    new_reference = credentials.store_password(password)
                else:
                    new_reference = CredentialReference(storage="file", key="credentials.json")
                    write(directory / "credentials.json", credentials.private_password_bytes(password))
            new_references.append((item.destination, new_reference))
            profile = item.profile.model_copy(update={"credential": new_reference})
            write(directory / "policy.json", item.policy)
            write(item.destination, profile_bytes(profile))
            check_mcp(item.destination)
        _verify_originals(plan)
        for path, _, data, receipt in written:
            if snapshot_with_identity(path) != (data, receipt.get("identity")):
                raise SetupError("新配置在驗證後已被其他程序修改，拒絕註冊。")
        for target, original, data in plan.clients:
            write(target.path, data, expected=original, backup=True)
        return {
            "apply": True,
            "profiles": [item.profile.name for item in plan.profiles],
            "clients": [str(t.path) for t, _, _ in plan.clients],
            "backups": backups,
        }
    except BaseException:
        conflicts = set()
        client_records = {target.path: (target, original, data) for target, original, data in plan.clients}
        # Preserve a whole new profile if another writer now owns any of its
        # files. Deleting its credentials/policy would break that newer config.
        preserved = set()
        for path, original, data, receipt in written:
            if path in client_records:
                continue
            current = snapshot_with_identity(path)
            if current[0] not in (original, data) or (current[0] == data and current[1] != receipt.get("identity")):
                preserved.add(path.parent)
                conflicts.add(path)
        for path, original, data, receipt in reversed(written):
            if path not in client_records:
                continue
            try:
                target, before, after = client_records[path]
                if not restore_owned(path, original, data, receipt.get("identity")):
                    if not _restore_client_entries(target, before, after, plan.profiles):
                        conflicts.add(path)
                        preserved.update(item.destination.parent for item in plan.profiles)
            except (OSError, SetupError):
                conflicts.add(path)
                preserved.update(item.destination.parent for item in plan.profiles)
        for path, original, data, receipt in reversed(written):
            if path in client_records or path.parent in preserved:
                continue
            try:
                if not restore_owned(path, original, data, receipt.get("identity")):
                    conflicts.add(path)
                    preserved.add(path.parent)
            except (OSError, SetupError):
                conflicts.add(path)
                preserved.add(path.parent)
        for path, reference in new_references:
            if path.parent not in preserved:
                credentials.delete_password(reference)
        # No recursive deletion: other processes may have added files.
        if conflicts:
            raise SetupError("遷移失敗；其他程序修改的檔案已保留，未移除其憑證。") from None
        raise


def _restore_client_entries(target, before, after, profiles):
    """Undo only unchanged entries we rewrote, retaining unrelated newer edits."""
    current = read_snapshot(target.path)
    if current is None:
        return True
    document, key = parse_client(target, current)
    old, _ = parse_client(target, before)
    ours, _ = parse_client(target, after)
    entries = document.get(key, {})
    preserved = False
    changed = False
    for item in profiles:
        name = item.profile.name
        if name not in ours.get(key, {}) or old.get(key, {}).get(name) == ours[key][name]:
            continue
        if name not in entries or entries[name] == old.get(key, {}).get(name):
            continue
        if entries[name] != ours[key][name]:
            preserved = True
            continue
        entries[name] = old[key][name]
        changed = True
    if changed:
        data = (
            tomlkit.dumps(document)
            if target.format == "toml"
            else json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        ).encode()
        atomic_write(target.path, data, expected=current)
    return not preserved


def cli_migrate(args):
    targets = None
    if args.client or args.client_config or args.client_path:
        targets = []
        known = {t.name: t for t in client_targets()}
        from dataclasses import replace

        if args.client_config and not args.client:
            raise SetupError("--client-config 需要搭配 --client。")
        if args.client:
            if args.client not in known:
                raise SetupError("目前平台不支援該 Client 的預設路徑。")
            target = known[args.client]
            targets.append(
                replace(target, path=args.client_config.expanduser().absolute()) if args.client_config else target
            )
        for value in args.client_path or []:
            name, sep, path = value.partition("=")
            if not sep or name not in known or not path:
                raise SetupError("--client-path 格式為 CLIENT=PATH。")
            targets.append(replace(known[name], path=Path(path).expanduser().absolute()))
        if len({str(t.path) for t in targets}) != len(targets):
            raise SetupError("Client 設定路徑不可重複。")
    plan = preview_migration(legacy_root=args.legacy_root, destination_root=args.config_dir, clients=targets)
    print(json.dumps(apply_migration(plan) if args.apply else plan.preview(), ensure_ascii=False, indent=2))
