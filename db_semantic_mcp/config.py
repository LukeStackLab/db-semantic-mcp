import hashlib
import json
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AffectedRowsPolicy = Literal["strict", "report"]
StartupMode = Literal["full", "cache_first"]
_fixed_settings = ContextVar("fixed_connection_settings", default=False)


class OracleOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    service_name: str | None = None
    sid: str | None = None
    dsn: str | None = None
    driver_mode: Literal["thin", "thick"] = "thin"
    client_lib_dir: str | None = None
    config_dir: str | None = None
    wallet_location: str | None = None
    wallet_password_env: str | None = None
    ssl_server_cert_dn: str | None = None
    current_schema: str | None = None
    connect_timeout: float = Field(default=10.0, gt=0, le=300)

    @model_validator(mode="after")
    def valid_options(self):
        if self.dsn and (
            re.search(r"(?i)(?:password|passwd|pwd|user|username|access_token|wallet_password)\s*=", self.dsn)
            or re.search(r"^[^()\s]+/[^()\s]*@", self.dsn)
            or re.search(r"://[^/()\s]*@", self.dsn)
        ):
            raise ValueError("Oracle DSN must not contain credentials; use explicit credential references")
        if sum(bool(v) for v in (self.service_name, self.sid, self.dsn)) != 1:
            raise ValueError("Oracle requires exactly one of service_name, sid, dsn")
        if self.driver_mode == "thin" and self.client_lib_dir:
            raise ValueError("client_lib_dir requires explicit thick mode")
        if self.driver_mode == "thick" and (self.wallet_location or self.wallet_password_env):
            raise ValueError("Thick mode wallets must be configured in sqlnet.ora via config_dir")
        if self.wallet_password_env and not self.wallet_location:
            raise ValueError("wallet_password_env requires wallet_location")
        if self.wallet_password_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.wallet_password_env):
            raise ValueError("Invalid wallet password environment reference")
        if self.current_schema and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_$#]{0,127}", self.current_schema):
            raise ValueError("current_schema must be a simple Oracle identifier")
        return self


class RemoteSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    db_link: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_$#]*(?:\.[A-Za-z0-9_$#]+)*$")
    readonly: bool = True

    @field_validator("db_link")
    @classmethod
    def uppercase_link(cls, value):
        return value.upper()


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SEMANTIC_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        if _fixed_settings.get():
            return (init_settings,)
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    @classmethod
    def from_fixed(cls, **values) -> "Config":
        """Profiles must not merge nested mappings from process env or cwd .env."""
        token = _fixed_settings.set(True)
        try:
            return cls(_env_file=None, **values)
        finally:
            _fixed_settings.reset(token)

    # ---- DB connection ----
    connection_name: str | None = None
    server: str = ""
    user: str | None = None
    password: str | None = None
    database: str = ""
    port: int | None = Field(default=None, ge=1, le=65535)
    database_type: Literal["sqlserver", "oracle"] = "sqlserver"
    oracle: OracleOptions | None = None
    sources: dict[str, RemoteSource] = Field(default_factory=dict)
    source_id: str = "local"
    source_dblink: str | None = None
    _base_cache_path: str | None = PrivateAttr(default=None)
    _policy_snapshot: object | None = PrivateAttr(default=None)
    _resolved_dblink: str | None = PrivateAttr(default=None)
    windows_auth: bool = False
    encrypt: bool = False
    db_worker_limit: int = Field(default=4, ge=1, le=32)

    # ---- Cache ----
    cache_path: str = "./cache/semantic_mcp.db"
    cache_enabled: bool = True
    startup_mode: StartupMode = "cache_first"
    background_batch_size: int = Field(default=5, ge=1)
    background_interval_ms: int = Field(default=500, ge=0)
    # Minimum seconds between schema drift probes (throttle window)
    probe_interval_s: float = Field(default=60.0, ge=0.0)
    schema_cache_ttl_s: float = Field(default=300.0, ge=0.0)
    background_retry_base_s: float = Field(default=1.0, gt=0.0)
    background_retry_max_s: float = Field(default=60.0, gt=0.0)

    # ---- Policy ----
    policy_file: str | None = None
    policy_profile: str | None = None

    # ---- Policy overrides ----
    max_rows_returned: int = Field(default=1000, ge=1)
    max_rows_affected: int = Field(default=100, ge=1)
    query_timeout: int = Field(default=30, ge=1)

    # ---- Output safety (hard caps; never tiered by the caller) ----
    # A result stops at whichever limit is reached first: row count,
    # serialized bytes, or per-cell characters. Truncation is always reported.
    max_response_bytes: int = Field(default=65536, ge=1024)
    max_cell_chars: int = Field(default=2000, ge=16)
    max_list_items: int = Field(default=500, ge=1)

    # ---- Tool surface ----
    # "all" is the default surface and now resolves to the full consolidated
    # 12-tool set (see server/tools/__init__.py::_GROUP_REGISTRATIONS).
    tool_profile: str = "all"
    discovery_aliases: dict[str, list[str]] = Field(default_factory=dict)

    # ---- Metrics ----
    metrics_enabled: bool = True

    # ---- Agent-oriented defaults ----
    direct_execute_enabled: bool = True
    strict_rows_affected_cap: bool = False
    workflow_tools_enabled: bool = True

    # ---- Analyzer router ----
    # Routing may use regex; authorization always uses the strict TSQL AST.
    intent_analyzer: Literal["regex", "ast"] = "ast"

    @model_validator(mode="after")
    def validate_database(self):
        if self.port is None:
            self.port = 1521 if self.database_type == "oracle" else 1433
        for name in self.sources:
            if name == "local" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
                raise ValueError("Invalid or reserved source id")
        if self.database_type == "oracle":
            if self.oracle is None:
                raise ValueError("Oracle options are required")
            if self.windows_auth:
                raise ValueError("Oracle does not support windows_auth")
            if not self.oracle.dsn and not self.server:
                raise ValueError("Oracle service/SID connections require server")
            if not self.database:
                self.database = self.oracle.service_name or self.oracle.sid or "oracle"
        elif self.oracle is not None or self.sources or self.source_dblink:
            raise ValueError("Oracle options and sources require database_type=oracle")
        elif not self.server or not self.database:
            raise ValueError("SQL Server requires server and database")
        if self.source_id != "local":
            route = self.sources.get(self.source_id)
            if route is None or self.source_dblink != route.db_link:
                raise ValueError("Source must match a configured route")
        elif self.source_dblink is not None:
            raise ValueError("Local source cannot have a DB link")
        return self

    def for_source(self, source_id: str = "local", *, route_identity: dict | None = None) -> "Config":
        """Clone request context; route selection itself does not grant authorization."""
        if source_id != "local" and source_id not in self.sources:
            raise ValueError("Unknown source")
        clone = self.model_copy(deep=True)
        base = self._base_cache_path or self.cache_path
        clone._base_cache_path = base
        clone.source_id = source_id
        clone._resolved_dblink = (route_identity or {}).get("db_link") if source_id != "local" else None
        clone.source_dblink = None if source_id == "local" else self.sources[source_id].db_link
        if source_id == "local":
            clone.cache_path = base
        else:
            identity = hashlib.sha256(
                json.dumps(
                    {
                        "source": source_id,
                        "catalog_route": route_identity,
                        "route": self.sources[source_id].model_dump(),
                        "server": self.server,
                        "database": self.database,
                        "port": self.port,
                        "user": self.user,
                        "oracle": self.oracle.model_dump() if self.oracle else None,
                        "policy_file": self.policy_file,
                        "policy_profile": self.policy_profile,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()[:24]
            path = Path(base)
            clone.cache_path = str(path.parent / "sources" / identity / path.name)
        return clone


_config: Config | None = None
_request_config: ContextVar[Config | None] = ContextVar("request_config", default=None)


def get_config() -> Config:
    if (request := _request_config.get()) is not None:
        return request
    global _config
    if _config is None:
        _config = Config()
    return _config


def reset_config() -> None:
    """Test helper only."""
    global _config
    _config = None


def configure(cfg: Config) -> None:
    """Select the fixed configuration before starting this process."""
    global _config
    _config = cfg
