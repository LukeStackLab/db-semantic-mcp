"""Explicit SQL Server database selection without granting discovery candidates."""

from pathlib import Path

from ..profiles import ConnectionProfile, config_for_profile
from .checks import check_connection, discover_databases
from .files import SetupError


def gather_databases(profile, password, input_fn, emit, *, existing_databases=None):
    from .wizard import _ask, _choose, _confirm

    cfg = config_for_profile(profile, Path("."), password)
    # Setup creates v3 even when upgrading a v2 draft. Probe under v3 rules so
    # every accepted name is resolved by the server and metadata is sufficient.
    cfg = cfg.model_copy(update={"profile_version": 3})
    try:
        names = discover_databases(cfg)
    except SetupError as exc:
        emit(str(exc))
        names = []
    previous = list(profile.databases) if profile.format_version == 3 else [profile.database]
    if existing_databases is not None:
        previous = existing_databases
    names = sorted(set(names + previous))
    manual = _confirm(input_fn, "手動加入 Database 名稱（包含清單未顯示的庫）？", default=not names)
    if manual:
        while True:
            name = _ask(input_fn, "Database 名稱")
            try:
                result = check_connection(cfg.model_copy(update={"database": name}))
                canonical = result["database"]
                # Metadata names are authoritative, including case-sensitive servers.
                if canonical not in names:
                    names.append(canonical)
            except SetupError as exc:
                emit(str(exc))
            if not _confirm(input_fn, "再手動加入一個 Database？"):
                break
    if not names:
        raise SetupError("沒有可選取的 Database。")
    while True:
        selected = input_fn.checkbox(
            "明確授權哪些 Database？",
            [(name, name) for name in sorted(names)],
            default=[name for name in previous if name in names],
            empty_message="請至少勾選一個 Database。",
        )
        if not selected:
            emit("請至少勾選一個 Database。")
            continue
        if any(name not in names for name in selected):
            raise SetupError("Database 選取值無效。")
        verified = []
        failed = False
        for name in selected:
            try:
                result = check_connection(cfg.model_copy(update={"database": name}))
                canonical = result["database"]
                if canonical not in verified:
                    verified.append(canonical)
            except SetupError:
                emit(f"{name}：無法連線或讀取 metadata。請重試或取消勾選。")
                failed = True
        if failed:
            continue
        bootstrap = (
            verified[0]
            if len(verified) == 1
            else _choose(
                input_fn,
                "啟動驗證 Database",
                [(name, name) for name in verified],
                profile.database if profile.database in verified else verified[0],
            )
        )
        values = profile.model_dump()
        values.update(format_version=3, database=bootstrap, databases=verified)
        return ConnectionProfile.model_validate(values)
