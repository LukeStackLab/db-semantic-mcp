"""Traditional Chinese terminal wizard for independent MCP servers."""
import getpass
import os
import sys
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path

from pydantic import ValidationError

from ..credentials import (
    CredentialError, CredentialReference, delete_password, read_password,
    private_password_bytes, store_password, store_private_password,
)
from ..profiles import (
    ConnectionProfile, config_for_profile, default_config_root, policy_bytes,
    profile_bytes, read_profile,
)
from .checks import check_connection, check_mcp
from .clients import (
    ClientTarget, client_targets, merge_servers, parse_client,
    register_servers, server_entry,
)
from .files import (
    SetupError, _write_lock, atomic_write, read_snapshot, restore_owned,
    snapshot_with_identity,
)


def _ask(input_fn, label: str, default: str = "") -> str:
    while True:
        value = input_fn(f"{label}" + (f" [{default}]" if default else "") + "：").strip()
        if value or default:
            return value or default


def _confirm(input_fn, label: str, default: bool = False) -> bool:
    while True:
        value = input_fn(f"{label} [{'Y/n' if default else 'y/N'}]：").strip().lower()
        if not value:
            return default
        if value in {"y", "yes", "是"}:
            return True
        if value in {"n", "no", "否"}:
            return False


def _port(input_fn, default: int) -> int:
    while True:
        value = _ask(input_fn, "連接埠", str(default))
        if value.isdigit() and 1 <= int(value) <= 65535:
            return int(value)


def _select_client(client, client_config, input_fn, emit) -> ClientTarget:
    targets = client_targets()
    if client:
        matching = [t for t in targets if t.name == client]
        if not matching:
            raise SetupError("目前平台不支援此 Client 的預設路徑。")
        target = matching[0]
    else:
        detected = [t for t in targets if t.detected]
        if len(detected) == 1:
            target = detected[0]
            emit(f"偵測到 {target.label}。")
        else:
            ordered = sorted(targets, key=lambda t: not t.detected)
            emit("選擇要配置的 MCP Client：")
            for n, item in enumerate(ordered, 1):
                emit(f"  {n}. {item.label}" + ("（已偵測）" if item.detected else ""))
            while True:
                choice = _ask(input_fn, "Client", "1")
                if choice.isdigit() and 1 <= int(choice) <= len(ordered):
                    target = ordered[int(choice) - 1]
                    break
    if client_config:
        target = replace(target, path=client_config.expanduser().absolute())
    emit(f"Client 設定：{target.path}")
    return target


@dataclass
class Draft:
    profile: ConnectionProfile
    password: str
    directory: Path
    original: dict[Path, bytes | None]
    previous: ConnectionProfile | None = None
    original_identities: dict[Path, tuple[int, int] | None] = field(default_factory=dict)


@dataclass
class Applied:
    draft: Draft
    reference: CredentialReference
    written: dict[Path, bytes]
    receipts: dict[Path, dict] = field(default_factory=dict)
    tools: int = 0

    def verify(self) -> None:
        for path, data in self.written.items():
            if read_snapshot(path) != data:
                raise SetupError(f"驗證後配置已被其他程序修改，未註冊：{path}")
            info = path.stat()
            if (info.st_dev, info.st_ino) != self.receipts.get(path, {}).get("identity"):
                raise SetupError(f"驗證後配置已被其他程序取代，未註冊：{path}")

    def rollback(self) -> list[str]:
        conflicts = []
        for path, data in reversed(list(self.written.items())):
            try:
                original = self.draft.original.get(path)
                if not restore_owned(path, original, data, self.receipts.get(path, {}).get("identity")):
                    conflicts.append(str(path))
            except (SetupError, OSError):
                conflicts.append(str(path))
        config_path = self.draft.directory / "connection.json"
        if str(config_path) not in conflicts:
            delete_password(self.reference)
        return conflicts

    def finalize(self) -> None:
        config_path = self.draft.directory / "connection.json"
        with _write_lock(config_path):
            current, identity = snapshot_with_identity(config_path)
            if current != self.written[config_path] or identity != self.receipts[config_path].get("identity"):
                return  # A newer configuration owns its credential lifecycle.
            self._cleanup_previous_credential()

    def _cleanup_previous_credential(self) -> None:
        previous = self.draft.previous
        if previous and previous.credential != self.reference:
            delete_password(previous.credential)
            if previous.credential and previous.credential.storage == "file" and self.reference.storage != "file":
                path = self.draft.directory / "credentials.json"
                original = self.draft.original.get(path)
                if original is not None:
                    restore_owned(path, None, original, self.draft.original_identities.get(path))


def _gather_connection(name, previous, directory, input_fn, password_fn, *, readonly=None):
    server = _ask(input_fn, "SQL Server 主機", previous.server if previous else "")
    port = _port(input_fn, previous.port if previous else 1433)
    database = _ask(input_fn, "資料庫名稱", previous.database if previous else "")
    user = _ask(input_fn, "帳號", previous.user if previous else "")
    password = password_fn("密碼" + (" [Enter 沿用]" if previous and previous.credential else "") + "：")
    if not password and previous and previous.credential:
        password = read_password(previous.credential, directory)
    while not password:
        password = password_fn("密碼不得為空，請重新輸入：")
    if readonly is None:
        readonly = not _confirm(
            input_fn, "允許新增與修改資料？",
            default=previous is not None and not previous.readonly,
        )
    fields = previous.model_dump() if previous else {}
    fields.update(
        name=name, server=server, port=port, database=database,
        user=user, readonly=readonly, credential=None,
    )
    return ConnectionProfile.model_validate(fields), password


def _apply(draft: Draft, input_fn, emit) -> Applied:
    directory = draft.directory
    if directory.is_symlink():
        raise SetupError("配置目錄不得為符號連結。")
    read_snapshot(directory / "connection.json")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        directory.chmod(0o700)
    reference = None
    written = {}
    receipts = {}
    try:
        policy_data = policy_bytes(
            draft.profile,
            previous_policy=draft.original[directory / "policy.json"],
            previous_database=draft.previous.database if draft.previous else None,
            previous_readonly=draft.previous.readonly if draft.previous else None,
        )
        try:
            reference = store_password(draft.password)
        except CredentialError:
            if os.name == "nt":
                raise
            emit("系統憑證庫無法使用。本機憑證檔會以明碼保存，目錄權限 0700、檔案權限 0600。")
            if not _confirm(input_fn, "改用僅限目前使用者讀取的本機憑證檔？"):
                raise SetupError("未保存密碼，這組配置未套用。")
            credential_path = directory / "credentials.json"
            if read_snapshot(credential_path) != draft.original[credential_path]:
                raise SetupError("憑證檔已被其他程序修改，未套用。")
            written[credential_path] = private_password_bytes(draft.password)
            receipts[credential_path] = {}
            reference = CredentialReference(storage="file", key="credentials.json")
            store_private_password(
                directory, draft.password, expected=draft.original[credential_path],
                receipt=receipts[credential_path],
            )
        profile = draft.profile.model_copy(update={"credential": reference})
        for path, data in [
            (directory / "policy.json", policy_data),
            (directory / "connection.json", profile_bytes(profile)),
        ]:
            written[path] = data
            receipts[path] = {}
            atomic_write(path, data, expected=draft.original[path], backup=True, receipt=receipts[path])
        applied = Applied(draft, reference, written, receipts)
        emit(f"正在驗證 {profile.name} 的 MCP 啟動與握手……")
        applied.tools = check_mcp(directory / "connection.json")
        return applied
    except BaseException:
        if reference is not None:
            _rollback([Applied(draft, reference, written, receipts)], emit)
        raise


def _rollback(items: list[Applied], emit) -> None:
    conflicts = []
    for item in reversed(items):
        conflicts.extend(item.rollback())
    if conflicts:
        emit("以下檔案已由其他程序修改或無法復原，已保留：")
        for path in conflicts:
            emit(f"  {path}")


def run_setup(
    *, root: Path | None = None, client: str | None = None,
    client_config: Path | None = None, input_fn=None, password_fn=None, emit=print,
    tool_profile: str | None = None,
) -> int:
    interactive = input_fn is None
    if interactive and not sys.stdin.isatty():
        raise SetupError("setup 需要互動式終端機。請直接在終端機執行此指令。")
    input_fn = input_fn or input
    password_fn = password_fn or getpass.getpass
    root = (root or default_config_root()).expanduser().absolute()
    if tool_profile is not None:
        ConnectionProfile(name="validation", server="x", database="x", user="x", tool_profile=tool_profile)
    if root.is_symlink():
        raise SetupError("配置根目錄不得為符號連結。")
    applied: list[Applied] = []
    registered = False
    try:
        emit("SQL Server MCP 安裝精靈：每組配置使用獨立程序、policy 與快取。")
        target = _select_client(client, client_config, input_fn, emit)
        original_client = read_snapshot(target.path)
        parse_client(target, original_client)
        drafts: list[Draft] = []
        while True:
            while True:
                name = _ask(input_fn, "MCP 名稱")
                try:
                    ConnectionProfile(name=name, server="x", database="x", user="x")
                except ValidationError:
                    emit("名稱須為 1–64 個英文字母、數字、底線或連字號，且不能是保留檔名。")
                    continue
                if name.casefold() in {d.profile.name.casefold() for d in drafts}:
                    emit("這次已設定相同名稱，請使用不同名稱。")
                    continue
                directory = root / name
                path = directory / "connection.json"
                try:
                    if root.exists() and any(
                        child.name.casefold() == name.casefold() and child.name != name
                        for child in root.iterdir()
                    ):
                        raise SetupError("名稱大小寫與既有配置衝突，請使用既有名稱。")
                    merge_servers(target, original_client, {name: server_entry(path)})
                    previous = read_profile(path) if read_snapshot(path) is not None else None
                    if previous and previous.name != name:
                        raise SetupError("既有配置名稱與目錄不一致，請先修正或使用新名稱。")
                except SetupError as exc:
                    emit(str(exc))
                    continue
                if previous and not _confirm(input_fn, f"更新既有配置 {name}？"):
                    continue
                break
            snapshots = {
                directory / filename: snapshot_with_identity(directory / filename)
                for filename in ("connection.json", "policy.json", "credentials.json")
            }
            original = {path: snapshot[0] for path, snapshot in snapshots.items()}
            original_identities = {path: snapshot[1] for path, snapshot in snapshots.items()}
            profile, password = _gather_connection(
                name, previous, directory, input_fn, password_fn,
            )
            if tool_profile is not None:
                profile = profile.model_copy(update={"tool_profile": tool_profile})
            while True:
                emit("正在測試登入與 metadata 存取……")
                try:
                    result = check_connection(config_for_profile(profile, directory, password))
                    emit(f"連線成功，可見 {result['visible_tables']} 張資料表。")
                    break
                except SetupError as exc:
                    emit(str(exc))
                    if not _confirm(input_fn, "重新輸入這組連線資訊？", default=True):
                        profile = None
                        break
                    profile, password = _gather_connection(
                        name, profile, directory, input_fn, password_fn,
                        readonly=profile.readonly,
                    )
            if profile is not None:
                emit(f"{name}：{'唯讀' if profile.readonly else '受限寫入'}，預算 {profile.budget}。")
                drafts.append(Draft(profile, password, directory, original, previous, original_identities))
            if not _confirm(input_fn, "再新增一組連線？"):
                break
        if not drafts:
            emit("沒有可套用的配置。")
            return 1
        emit("準備建立或更新以下獨立 MCP：")
        for draft in drafts:
            p = draft.profile
            emit(f"  {p.name}: {p.server}:{p.port}/{p.database}；{'唯讀' if p.readonly else '受限寫入'}；{p.budget}")
        if not _confirm(input_fn, f"套用至 {target.label}？", default=True):
            emit("已取消，未修改配置與 Client 設定。")
            return 0
        failed = 0
        for draft in drafts:
            try:
                applied.append(_apply(draft, input_fn, emit))
            except SetupError as exc:
                failed += 1
                emit(f"{draft.profile.name} 未套用：{exc}")
        if not applied:
            return 1
        for item in applied:
            item.verify()
        entries = {
            a.draft.profile.name: server_entry(a.draft.directory / "connection.json")
            for a in applied
        }
        merged_client = merge_servers(target, original_client, entries)
        client_receipt = {}
        try:
            backup = register_servers(target, entries, original_client, receipt=client_receipt)
        except BaseException:
            restore_owned(target.path, original_client, merged_client, client_receipt.get("identity"))
            _rollback(applied, emit)
            applied.clear()
            raise
        registered = True
        for item in applied:
            item.finalize()
            emit(f"完成 {item.draft.profile.name}：MCP 握手成功，{item.tools} 個工具。")
            emit(f"  配置：{item.draft.directory / 'connection.json'}")
        if backup:
            emit(f"原 Client 設定備份：{backup}")
        emit(f"已註冊 {len(applied)} 個獨立 MCP。請在 {target.label} 重新載入 MCP 設定或重新啟動 Client。")
        emit("Agent 可依 MCP 名稱選用連線；每個程序固定自己的配置。")
        return 1 if failed else 0
    except (KeyboardInterrupt, EOFError):
        if not registered:
            _rollback(applied, emit)
            emit("已取消，未修改 Client 設定。")
        else:
            emit("設定已套用，已停止輸出。")
        return 130
    except BaseException:
        if not registered:
            _rollback(applied, emit)
        raise


def terminal_setup(**kwargs) -> int:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return run_setup(**kwargs)
        except getpass.GetPassWarning:
            raise SetupError("目前終端機無法隱藏密碼輸入，已停止設定。") from None
