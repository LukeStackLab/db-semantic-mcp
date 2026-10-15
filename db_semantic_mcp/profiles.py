"""One immutable connection profile per MCP process."""

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .config import Config
from .credentials import CredentialReference, read_password
from .policy.models import PolicyFile, PolicyOperations, PolicyProfile
from .setup.files import SetupError, read_snapshot


class ConnectionProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format_version: Literal[1] = 1
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    server: str = Field(min_length=1)
    database: str = Field(min_length=1)
    user: str = Field(min_length=1)
    port: int = Field(default=1433, ge=1, le=65535)
    readonly: bool = True
    max_response_bytes: int = Field(default=65536, ge=1024)
    max_cell_chars: int = Field(default=2000, ge=16)
    tool_profile: str = "all"
    discovery_aliases: dict[str, list[str]] = Field(default_factory=dict)
    db_worker_limit: int = Field(default=4, ge=1, le=32)
    schema_cache_ttl_s: float = Field(default=300.0, ge=0)
    encrypt: bool = True
    max_rows_returned: int = Field(default=1000, ge=1)
    max_rows_affected: int = Field(default=100, ge=1)
    query_timeout: int = Field(default=30, ge=1)
    credential: CredentialReference | None = None

    @model_validator(mode="before")
    @classmethod
    def drop_retired_fields(cls, data):
        # Profiles written before the token-budget tiers were removed.
        if isinstance(data, dict) and "budget" in data:
            data = {k: v for k, v in data.items() if k != "budget"}
        return data

    @field_validator("tool_profile")
    @classmethod
    def valid_tool_profile(cls, value: str) -> str:
        # Keep validation independent of server imports and registration.
        aliases = {"all", "query", "explore", "maintenance"}
        groups = {"metadata", "policy", "query", "cache", "relationship", "object", "semantic", "metrics", "workflow"}
        value = value.strip()
        if value in aliases:
            return value
        selected = [part.strip() for part in value.split(",") if part.strip()]
        if not selected or any(part not in groups for part in selected):
            raise ValueError("Unknown tool profile")
        return ",".join(dict.fromkeys(selected))

    @field_validator("name")
    @classmethod
    def portable_name(cls, value: str) -> str:
        reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)}
        if value.upper() in reserved:
            raise ValueError("Reserved file name")
        return value

    @field_validator("server", "database", "user")
    @classmethod
    def clean_text(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(ch) < 32 for ch in value):
            raise ValueError("Empty value or control character")
        return value

    def policy(self) -> PolicyProfile:
        return PolicyProfile.model_validate(
            {
                "profile_name": "readonly" if self.readonly else "read_write_safe",
                "operations": {"select": True, "insert": not self.readonly, "update": not self.readonly},
                "constraints": {
                    "require_where_for_update": True,
                    "allow_multi_statement": False,
                    "max_rows_returned": self.max_rows_returned,
                    "max_rows_affected": self.max_rows_affected,
                    "query_timeout_seconds": self.query_timeout,
                },
                "scope": {"allowed_databases": [self.database]},
            }
        )


def default_config_root() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "db-semantic-mcp" / "connections"


def read_profile(path: Path) -> ConnectionProfile:
    data = read_snapshot(path)
    if data is None:
        raise SetupError(f"找不到連線配置：{path}")
    try:
        return ConnectionProfile.model_validate_json(data)
    except ValidationError as exc:
        fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors())
        raise SetupError(f"連線配置格式錯誤：{fields}") from None


def config_for_profile(
    profile: ConnectionProfile,
    directory: Path,
    password: str,
    *,
    policy_path: Path | None = None,
) -> Config:
    # Pass every field explicitly: inherited env and cwd .env must not
    # override even fields that this profile leaves at their defaults.
    values = {
        name: field.get_default(call_default_factory=True)
        for name, field in Config.model_fields.items()
        if not field.is_required()
    }
    values.update(
        connection_name=profile.name,
        server=profile.server,
        database=profile.database,
        user=profile.user,
        password=password,
        port=profile.port,
        encrypt=profile.encrypt,
        cache_path=str(directory / "cache" / "semantic.db"),
        policy_file=str(policy_path) if policy_path else None,
        policy_profile=profile.policy().profile_name,
        max_response_bytes=profile.max_response_bytes,
        max_cell_chars=profile.max_cell_chars,
        tool_profile=profile.tool_profile,
        discovery_aliases=profile.discovery_aliases,
        db_worker_limit=profile.db_worker_limit,
        schema_cache_ttl_s=profile.schema_cache_ttl_s,
        max_rows_returned=profile.max_rows_returned,
        max_rows_affected=profile.max_rows_affected,
        query_timeout=profile.query_timeout,
    )
    return Config(_env_file=None, **values)


def profile_bytes(profile: ConnectionProfile) -> bytes:
    return (profile.model_dump_json(indent=2) + "\n").encode()


def policy_bytes(
    profile: ConnectionProfile,
    *,
    previous_policy: bytes | None = None,
    previous_database: str | None = None,
    previous_readonly: bool | None = None,
) -> bytes:
    policy = profile.policy()
    if previous_policy is not None:
        try:
            previous_file = PolicyFile.model_validate_json(previous_policy)
            previous = previous_file.profiles[previous_file.active_profile]
        except (ValidationError, KeyError):
            raise SetupError("既有 policy 無效，拒絕覆寫。請先修正 policy。") from None
        operations = policy.operations.model_dump()
        for key, enabled in previous.operations.model_dump().items():
            if key in {"insert", "update"} and previous_readonly is True and not profile.readonly:
                continue  # The user explicitly enabled limited writes.
            operations[key] = operations[key] and enabled
        policy.operations = PolicyOperations.model_validate(operations)
        constraints = previous.constraints.model_copy(deep=True)
        constraints.require_where_for_update = True
        constraints.allow_multi_statement = False
        for key in ("max_rows_returned", "max_rows_affected", "query_timeout_seconds"):
            setattr(constraints, key, min(getattr(constraints, key), getattr(policy.constraints, key)))
        policy.constraints = constraints
        policy.scope = previous.scope.model_copy(deep=True)
        if not policy.scope.allowed_databases or policy.scope.allowed_databases == [previous_database]:
            policy.scope.allowed_databases = [profile.database]
    return (
        json.dumps(
            {
                "active_profile": policy.profile_name,
                "profiles": {policy.profile_name: policy.model_dump()},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode()


def load_profile(path: Path) -> Config:
    path = path.expanduser().absolute()
    profile = read_profile(path)
    if profile.credential is None:
        raise SetupError("連線配置未指定憑證。")
    policy_path = path.parent / "policy.json"
    raw_policy = read_snapshot(policy_path)
    if raw_policy is None:
        raise SetupError("找不到配置的 policy.json，拒絕啟動。")
    try:
        policy_file = PolicyFile.model_validate_json(raw_policy)
        policy = policy_file.profiles[policy_file.active_profile]
    except (ValidationError, KeyError):
        raise SetupError("配置的 policy.json 無效，拒絕啟動。") from None
    expected = profile.policy()
    if policy.profile_name != expected.profile_name:
        raise SetupError("配置與 policy 的權限模式不一致，拒絕啟動。")
    allowed = expected.operations.model_dump()
    if any(v and not allowed[k] for k, v in policy.operations.model_dump().items()):
        raise SetupError("policy 超過配置允許的權限，拒絕啟動。")
    if policy.constraints.allow_multi_statement:
        raise SetupError("獨立配置不允許多敘述查詢。")
    password = read_password(profile.credential, path.parent)
    cfg = config_for_profile(profile, path.parent, password, policy_path=policy_path)
    cfg.max_rows_returned = min(cfg.max_rows_returned, policy.constraints.max_rows_returned)
    cfg.max_rows_affected = min(cfg.max_rows_affected, policy.constraints.max_rows_affected)
    cfg.query_timeout = min(cfg.query_timeout, policy.constraints.query_timeout_seconds)
    if not profile.readonly and not policy.constraints.require_where_for_update:
        raise SetupError("受限寫入必須要求 UPDATE 使用 WHERE。")
    identity = hashlib.sha256(
        json.dumps(
            {
                "server": profile.server,
                "port": profile.port,
                "database": profile.database,
                "user": profile.user,
                "policy": policy.model_dump(),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    # A changed target/principal/policy gets a fresh cache without deleting
    # another process's existing cache.
    cfg.cache_path = str(path.parent / "cache" / identity[:24] / "semantic.db")
    return cfg
