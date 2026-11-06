"""Traditional Chinese terminal wizard for independent MCP servers."""

import getpass
import os
import sys
import warnings
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path

from pydantic import ValidationError

from ..credentials import (
    CredentialError,
    CredentialReference,
    delete_password,
    private_password_bytes,
    read_password,
    store_password,
    store_private_password,
)
from ..profiles import (
    ConnectionProfile,
    config_for_profile,
    default_config_root,
    policy_bytes,
    profile_bytes,
    read_profile,
)
from .checks import check_connection, check_mcp
from .clients import (
    ClientTarget,
    client_targets,
    merge_servers,
    parse_client,
    register_servers,
    server_entry,
)
from .files import (
    SetupError,
    _write_lock,
    atomic_write,
    read_snapshot,
    restore_owned,
    snapshot_with_identity,
)


def _ask(input_fn, label: str, default: str = "") -> str:
    while True:
        value = (
            input_fn.text(label, default)
            if hasattr(input_fn, "text")
            else input_fn(f"{label}" + (f" [{default}]" if default else "") + "：")
        ).strip()
        if value or default:
            return value or default


def _confirm(input_fn, label: str, default: bool = False) -> bool:
    if hasattr(input_fn, "confirm"):
        return input_fn.confirm(label, default)
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


def _choose(input_fn, label, choices, default=None):
    if hasattr(input_fn, "select"):
        return input_fn.select(label, choices, default)
    # Injectable text input supports programmatic callers and tests.
    while True:
        value = _ask(input_fn, label, str(default) if default is not None else "")
        for index, (_, candidate) in enumerate(choices, 1):
            if value == str(candidate) or value == str(index):
                return candidate


def _stage(input_fn, number, title):
    if hasattr(input_fn, "stage"):
        input_fn.stage(number, title)


def _busy(input_fn, label):
    return input_fn.status(label) if hasattr(input_fn, "status") else nullcontext()


def _select_clients(client, client_config, input_fn, emit) -> list[ClientTarget]:
    targets = client_targets()
    if client:
        names = [client] if isinstance(client, str) else list(dict.fromkeys(client))
        selected = []
        for name in names:
            matching = [t for t in targets if t.name == name]
            if not matching:
                raise SetupError("目前平台不支援此 Client 的預設路徑。")
            selected.append(matching[0])
    else:
        ordered = sorted(targets, key=lambda t: not t.detected)
        choices = [(t.label + ("（已偵測）" if t.detected else ""), t.name) for t in ordered]
        if hasattr(input_fn, "checkbox"):
            names = input_fn.checkbox("要配置哪些 MCP Client？", choices)
            selected = [t for t in ordered if t.name in names]
        else:
            emit("選擇要配置的 MCP Client（可用逗號分隔多選）：")
            for n, (label, _) in enumerate(choices, 1):
                emit(f"  {n}. {label}")
            while True:
                values = _ask(input_fn, "Client", "1").split(",")
                if all(v.strip().isdigit() and 1 <= int(v) <= len(ordered) for v in values):
                    selected = list(dict.fromkeys(ordered[int(v) - 1] for v in values))
                    break
    if not selected:
        raise SetupError("請至少選擇一個 Client。")
    if client_config:
        if len(selected) != 1:
            raise SetupError("--client-config 僅能搭配單一 Client，請分別執行或使用預設路徑。")
        selected = [replace(selected[0], path=client_config.expanduser().absolute())]
    if len({t.path.absolute() for t in selected}) != len(selected):
        raise SetupError("多個 Client 不得共用同一個設定檔。")
    for target in selected:
        emit(f"{target.label} 設定：{target.path}")
    return selected


@dataclass
class Draft:
    profile: ConnectionProfile
    password: str
    directory: Path
    original: dict[Path, bytes | None]
    previous: ConnectionProfile | None = None
    original_identities: dict[Path, tuple[int, int] | None] = field(default_factory=dict)
    source_scopes: dict = field(default_factory=dict)


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
    provider = previous.database_type if previous else None
    while provider is None:
        choice = _choose(input_fn, "資料庫類型", [("SQL Server", "sqlserver"), ("Oracle", "oracle")], "sqlserver")
        if choice in {"sqlserver", "oracle"}:
            provider = choice
    if provider == "oracle":
        from .oracle_wizard import gather_options

        provider_fields = gather_options(previous, input_fn)
        server, port, database = (provider_fields[key] for key in ("server", "port", "database"))
    else:
        provider_fields = {"database_type": "sqlserver"}
        server = _ask(input_fn, "SQL Server 主機", previous.server if previous else "")
        port = _port(input_fn, previous.port if previous else 1433)
        database = (
            (previous.database if previous else "master")
            if hasattr(input_fn, "checkbox")
            else _ask(input_fn, "資料庫名稱", previous.database if previous else "")
        )
    user = _ask(input_fn, "帳號", previous.user if previous else "")
    password = password_fn("密碼" + (" [Enter 沿用]" if previous and previous.credential else "") + "：")
    if not password and previous and previous.credential:
        password = read_password(previous.credential, directory)
    while not password:
        password = password_fn("密碼不得為空，請重新輸入：")
    if provider == "sqlserver" and hasattr(input_fn, "checkbox"):
        from .sqlserver_wizard import gather_databases

        candidate = previous.model_dump() if previous else {}
        if candidate.get("format_version", 2) == 2:
            candidate.pop("databases", None)
        candidate.update(name=name, server=server, port=port, database=database, user=user, credential=None)
        selected = gather_databases(
            ConnectionProfile.model_validate(candidate),
            password,
            input_fn,
            getattr(input_fn, "emit", print),
            existing_databases=(list(previous.databases) if previous else []),
        )
        provider_fields.update(format_version=3, databases=selected.databases)
        database = selected.database
    if readonly is None:
        readonly = not _confirm(
            input_fn,
            "允許新增與修改資料？",
            default=previous is not None and not previous.readonly,
        )
    execution_recording_enabled = _confirm(
        input_fn,
        "記錄工具執行的 SQL 與參數到本機 cache？",
        default=bool(previous and previous.execution_recording_enabled),
    )
    fields = previous.model_dump() if previous else {}
    if fields.get("format_version", 2) == 2:
        fields.pop("databases", None)
    fields.update(provider_fields)
    fields.update(
        name=name,
        server=server,
        port=port,
        database=database,
        user=user,
        readonly=readonly,
        execution_recording_enabled=execution_recording_enabled,
        credential=None,
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
        if draft.source_scopes:
            from .oracle_wizard import merge_source_policy

            policy_data = merge_source_policy(policy_data, draft.source_scopes)
        try:
            reference = store_password(draft.password)
        except CredentialError:
            if os.name == "nt":
                raise
            emit("系統憑證庫無法使用。本機憑證檔會以明碼保存，目錄權限 0700、檔案權限 0600。")
            if not _confirm(input_fn, "改用僅限目前使用者讀取的本機憑證檔？"):
                raise SetupError("未保存密碼，這組配置未套用。") from None
            credential_path = directory / "credentials.json"
            if read_snapshot(credential_path) != draft.original[credential_path]:
                raise SetupError("憑證檔已被其他程序修改，未套用。") from None
            written[credential_path] = private_password_bytes(draft.password)
            receipts[credential_path] = {}
            reference = CredentialReference(storage="file", key="credentials.json")
            store_private_password(
                directory,
                draft.password,
                expected=draft.original[credential_path],
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
        if profile.database_type == "oracle":
            from ..profiles import load_profile

            diagnostics = check_connection(load_profile(directory / "connection.json"))
            if any(item.get("status") != "ok" for item in diagnostics.get("sources", {}).values()):
                raise SetupError("已選 Oracle 來源無法通過 policy、route 或 catalog 檢查。")
        applied = Applied(draft, reference, written, receipts)
        emit(f"正在驗證 {profile.name} 的 MCP 啟動與握手……")
        with _busy(input_fn, "正在驗證 MCP 握手與工具…"):
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


def _prepare_setup(root, input_fn, password_fn, tool_profile):
    interactive = input_fn is None
    if interactive and not sys.stdin.isatty():
        raise SetupError("setup 需要互動式終端機。請直接在終端機執行此指令。")
    if interactive:
        from .terminal import TerminalUI

        input_fn = TerminalUI()
        password_fn = password_fn or input_fn.password
    password_fn = password_fn or getpass.getpass
    root = (root or default_config_root()).expanduser().absolute()
    if tool_profile is not None:
        ConnectionProfile(name="validation", server="x", database="x", user="x", tool_profile=tool_profile)
    if root.is_symlink():
        raise SetupError("配置根目錄不得為符號連結。")
    return input_fn, password_fn, root


def _ask_connection_name(root, clients, drafts, input_fn, emit):
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
                child.name.casefold() == name.casefold() and child.name != name for child in root.iterdir()
            ):
                raise SetupError("名稱大小寫與既有配置衝突，請使用既有名稱。")
            for target, original_client in clients:
                merge_servers(target, original_client, {name: server_entry(path)})
            previous = read_profile(path) if read_snapshot(path) is not None else None
            if previous and previous.name != name:
                raise SetupError("既有配置名稱與目錄不一致，請先修正或使用新名稱。")
        except SetupError as exc:
            emit(str(exc))
            continue
        if previous and not _confirm(input_fn, f"更新既有配置 {name}？"):
            continue
        return directory, path, previous


def _collect_connection(name, previous, directory, input_fn, password_fn, emit, tool_profile):
    profile, password = _gather_connection(name, previous, directory, input_fn, password_fn)
    if tool_profile is not None:
        profile = profile.model_copy(update={"tool_profile": tool_profile})
    while True:
        emit("正在測試登入與 metadata 存取……")
        try:
            policy_path = directory / "policy.json"
            probe_cfg = config_for_profile(
                profile, directory, password, policy_path=policy_path if policy_path.exists() else None
            )
            if profile.database_type == "oracle":
                # This is a read-only preflight before policy files exist (or
                # before an explicitly changed readonly mode is applied).
                # The final fixed profile is checked again after atomic writes.
                probe_cfg.connection_name = None
                probe_cfg.policy_profile = None
            with _busy(input_fn, "正在測試登入與 metadata 存取…"):
                result = check_connection(probe_cfg)
            emit(f"連線成功，可見 {result['visible_tables']} 張資料表。")
            return profile, password, result
        except SetupError as exc:
            emit(str(exc))
            if not _confirm(input_fn, "重新輸入這組連線資訊？", default=True):
                return None, None, {}
            profile, password = _gather_connection(
                name,
                profile,
                directory,
                input_fn,
                password_fn,
                readonly=profile.readonly,
            )


def _apply_drafts(drafts, input_fn, emit, applied) -> int:
    failed = 0
    for draft in drafts:
        try:
            applied.append(_apply(draft, input_fn, emit))
        except SetupError as exc:
            failed += 1
            emit(f"{draft.profile.name} 未套用：{exc}")
    return failed


def _collect_drafts(root, clients, input_fn, password_fn, emit, tool_profile) -> list[Draft]:
    drafts: list[Draft] = []
    while True:
        directory, path, previous = _ask_connection_name(
            root,
            clients,
            drafts,
            input_fn,
            emit,
        )
        name = directory.name
        snapshots = {
            directory / filename: snapshot_with_identity(directory / filename)
            for filename in ("connection.json", "policy.json", "credentials.json")
        }
        original = {path: snapshot[0] for path, snapshot in snapshots.items()}
        original_identities = {path: snapshot[1] for path, snapshot in snapshots.items()}
        profile, password, diagnostics = _collect_connection(
            name,
            previous,
            directory,
            input_fn,
            password_fn,
            emit,
            tool_profile,
        )
        if profile is not None:
            emit(f"{name}：{'唯讀' if profile.readonly else '受限寫入'}，輸出上限 {profile.max_response_bytes} bytes。")
            if profile.format_version == 3:
                emit(f"一個 MCP；Database：{'、'.join(profile.databases)}；啟動驗證：{profile.database}。")
                emit("跨庫 SELECT 僅限明確勾選的 Database，並受共同與每庫 policy scope 限制。")
                if previous:
                    emit("更新會保留既有 schema/table、deny 與更嚴格限制；新增 Database 也受這些限制。")
            source_scopes = {}
            if profile.database_type == "oracle":
                from .oracle_wizard import select_sources

                emit(
                    f"Oracle {diagnostics.get('version', 'unknown')}；{diagnostics.get('driver_mode', profile.oracle.driver_mode)}；傳輸 {diagnostics.get('transport', 'unknown')}"
                )
                emit(str(diagnostics.get("capabilities", {}).get("temporal_values", "")))
                profile, source_scopes = select_sources(
                    profile, diagnostics.get("available_db_links", []), input_fn, emit
                )
            drafts.append(Draft(profile, password, directory, original, previous, original_identities, source_scopes))
        if not _confirm(input_fn, "再新增一組連線？"):
            break
    return drafts


def _register_with_clients(clients, applied: list[Applied], emit) -> list[Path]:
    for item in applied:
        item.verify()
    entries = {a.draft.profile.name: server_entry(a.draft.directory / "connection.json") for a in applied}
    # Validate every merge before writing the first client.
    writes = [(target, original, merge_servers(target, original, entries), {}) for target, original in clients]
    attempted = []
    backups = []
    try:
        for target, original, merged, receipt in writes:
            attempted.append((target, original, merged, receipt))
            backup = register_servers(target, entries, original, receipt=receipt)
            if backup:
                backups.append(backup)
        return backups
    except BaseException:
        conflicts = []
        for target, original, merged, receipt in reversed(attempted):
            if not receipt.get("identity"):
                continue  # This attempt never reached the write; nothing belongs to us.
            try:
                if not restore_owned(target.path, original, merged, receipt.get("identity")):
                    conflicts.append(str(target.path))
            except (SetupError, OSError):
                conflicts.append(str(target.path))
        if conflicts:
            emit("Client 已由其他程序修改或無法復原，已保留其設定與本次連線配置：")
            for path in conflicts:
                emit(path)
        else:
            _rollback(applied, emit)
        applied.clear()
        raise


def run_setup(
    *,
    root: Path | None = None,
    client: str | list[str] | None = None,
    client_config: Path | None = None,
    input_fn=None,
    password_fn=None,
    emit=print,
    tool_profile: str | None = None,
) -> int:
    input_fn, password_fn, root = _prepare_setup(root, input_fn, password_fn, tool_profile)
    applied: list[Applied] = []
    registered = False
    try:
        if hasattr(input_fn, "welcome"):
            input_fn.welcome()
            if emit is print:
                emit = input_fn.emit
        emit("DB Semantic MCP 安裝精靈：每組配置使用獨立程序、policy 與快取。")
        _stage(input_fn, 1, "選擇 MCP Clients")
        targets = _select_clients(client, client_config, input_fn, emit)
        clients = [(target, read_snapshot(target.path)) for target in targets]
        for target, original in clients:
            parse_client(target, original)
        _stage(input_fn, 2, "設定資料庫連線")
        drafts = _collect_drafts(root, clients, input_fn, password_fn, emit, tool_profile)
        if not drafts:
            emit("沒有可套用的配置。")
            return 1
        _stage(input_fn, 3, "確認與套用")
        if hasattr(input_fn, "summary"):
            input_fn.summary(drafts, targets)
        else:
            emit("準備建立或更新以下獨立 MCP：")
            for draft in drafts:
                p = draft.profile
                emit(
                    f"  {p.name}: {p.server}:{p.port}/{'、'.join(p.databases or [p.database])}；啟動 {p.database}；{'唯讀' if p.readonly else '受限寫入'}；{p.max_response_bytes} bytes"
                )
        labels = "、".join(target.label for target in targets)
        if not _confirm(input_fn, f"套用至 {labels}？", default=True):
            emit("已取消，未修改配置與 Client 設定。")
            return 0
        failed = _apply_drafts(drafts, input_fn, emit, applied)
        if not applied:
            return 1
        with _busy(input_fn, "正在註冊 MCP Clients…"):
            backups = _register_with_clients(clients, applied, emit)
        registered = True
        _stage(input_fn, 4, "安裝完成")
        for item in applied:
            item.finalize()
            emit(f"完成 {item.draft.profile.name}：MCP 握手成功，{item.tools} 個工具。")
            emit(f"  配置：{item.draft.directory / 'connection.json'}")
        for backup in backups:
            emit(f"原 Client 設定備份：{backup}")
        emit(f"已註冊 {len(applied)} 個獨立 MCP。請在 {labels} 重新載入 MCP 設定或重新啟動 Client。")
        emit("Agent 可依 MCP 名稱選用連線；每個程序固定自己的配置。")
        return 1 if failed else 0
    except (KeyboardInterrupt, EOFError):
        if not registered:
            _rollback(applied, emit)
            emit("已取消設定流程。")
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
