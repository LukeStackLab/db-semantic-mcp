"""Oracle-specific questions; passwords remain explicit environment references."""

from ..config import OracleOptions, RemoteSource
from ..policy.models import RemoteSourceScope
from .files import SetupError


def gather_options(previous, input_fn):
    from .wizard import _ask, _confirm, _port

    old = previous.oracle if previous else None
    while True:
        mode = _ask(input_fn, "Oracle driver mode（thin/thick）", old.driver_mode if old else "thin").lower()
        if mode in {"thin", "thick"}:
            break
    values = {
        "driver_mode": mode,
        "current_schema": old.current_schema if old else None,
        "connect_timeout": old.connect_timeout if old else 10.0,
    }
    if mode == "thick":
        values["client_lib_dir"] = input_fn("Oracle Client lib 目錄 [使用系統搜尋路徑]：").strip() or (
            old.client_lib_dir if old else None
        )
    values["config_dir"] = input_fn("Oracle network config 目錄（TNS/sqlnet.ora，可留空）：").strip() or (
        old.config_dir if old else None
    )
    default_kind = "dsn" if old and old.dsn else "sid" if old and old.sid else "service"
    while True:
        kind = _ask(input_fn, "連線方式（service/sid/dsn）", default_kind).lower()
        if kind in {"service", "sid", "dsn"}:
            break
    if kind == "dsn":
        values["dsn"] = _ask(input_fn, "TNS alias 或不含帳密的 DSN", old.dsn if old and old.dsn else "")
        server, port = "", 1521
        database = _ask(input_fn, "資料庫顯示名稱", previous.database if previous else "oracle")
    else:
        server = _ask(input_fn, "Oracle 主機", previous.server if previous else "")
        port = _port(input_fn, previous.port if previous else 1521)
        field = "service_name" if kind == "service" else "sid"
        values[field] = _ask(
            input_fn,
            "Service name" if kind == "service" else "SID",
            getattr(old, field) if old and getattr(old, field) else "",
        )
        database = values[field]
    encrypt = _confirm(
        input_fn, "使用 TLS（TCPS；DSN 所有地址須與此設定一致）？", previous.encrypt if previous else False
    )
    if encrypt:
        values["ssl_server_cert_dn"] = input_fn("TLS server certificate DN（可留空以驗證主機名稱）：").strip() or (
            old.ssl_server_cert_dn if old else None
        )
        if mode == "thin":
            values["wallet_location"] = input_fn("Thin wallet 目錄（PEM trust/client wallet；可留空）：").strip() or (
                old.wallet_location if old else None
            )
            if values["wallet_location"]:
                values["wallet_password_env"] = input_fn(
                    "Wallet 密碼環境變數名稱（只存參照；無密碼可留空）："
                ).strip() or (old.wallet_password_env if old else None)
        # Thick reads its wallet/trust from config_dir/sqlnet.ora.
    return dict(
        database_type="oracle",
        oracle=OracleOptions(**values),
        server=server,
        port=port,
        database=database,
        encrypt=encrypt,
    )


def select_sources(profile, links, input_fn, emit):
    from ..policy.intents.oracle_analyzer import oracle_identifier
    from .wizard import _ask, _confirm

    if not links or not _confirm(input_fn, "選用既有 Oracle DB link 作為探索來源？"):
        return profile, {}
    routes = dict(profile.sources)
    scopes = {}
    names = sorted({row["db_link"] for row in links})
    while True:
        for index, name in enumerate(names, 1):
            emit(f"  {index}. {name}")
        choice = _ask(input_fn, "DB link 編號", "1")
        if not choice.isdigit() or not 1 <= int(choice) <= len(names):
            continue
        source = _ask(input_fn, "來源 id（英數、底線或連字號）")
        if source == "local" or source in routes:
            emit("來源 id 已存在或為保留名稱。")
            continue
        schemas = [
            value.strip() for value in _ask(input_fn, "明確允許探索的 schema（逗號分隔）").split(",") if value.strip()
        ]
        try:
            if not schemas:
                raise ValueError("At least one explicit schema is required")
            for schema in schemas:
                oracle_identifier(schema)
            route = RemoteSource(db_link=names[int(choice) - 1], readonly=True)
            candidate = profile.model_dump()
            candidate["sources"] = {**routes, source: route}
            from ..profiles import ConnectionProfile

            updated = ConnectionProfile.model_validate(candidate)
        except ValueError:
            emit("來源 id 或 schema 格式無效。")
            continue
        scope = RemoteSourceScope(allowed_databases=[profile.database], allowed_schemas=schemas)
        if not _confirm(input_fn, f"明確授權 {source} SELECT metadata/query，schemas={','.join(schemas)}？"):
            if not _confirm(input_fn, "改選另一個來源？"):
                break
            continue
        profile = updated
        routes[source] = route
        scopes[source] = scope
        if not _confirm(input_fn, "再選一個 DB link？"):
            break
    return profile, scopes


def merge_source_policy(data, scopes):
    from ..policy.models import PolicyFile

    document = PolicyFile.model_validate_json(data)
    active = document.profiles[document.active_profile]
    for name, scope in scopes.items():
        if name in active.scope.sources:
            raise SetupError("來源 policy 已存在，拒絕默默放寬權限。")
        active.scope.sources[name] = scope
    return (document.model_dump_json(indent=2) + "\n").encode()
