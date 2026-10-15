from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

AffectedRowsPolicy = Literal["strict", "report"]
StartupMode = Literal["full", "cache_first"]


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SEMANTIC_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- DB connection ----
    connection_name: str | None = None
    mssql_server: str
    mssql_user: str | None = None
    mssql_password: str | None = None
    mssql_database: str
    mssql_port: int = 1433
    mssql_windows_auth: bool = False
    mssql_encrypt: bool = False
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


_config: Config | None = None


def get_config() -> Config:
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
