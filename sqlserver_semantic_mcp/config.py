from typing import Literal, Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DetailTier = Literal["brief", "standard", "full"]
ResponseMode = Literal["summary", "rows", "sample", "count_only"]
TokenBudgetHint = Literal["tiny", "low", "medium", "high"]
DEFAULT_TOKEN_BUDGET_HINT: TokenBudgetHint = "medium"
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
    mssql_server: str
    mssql_user: Optional[str] = None
    mssql_password: Optional[str] = None
    mssql_database: str
    mssql_port: int = 1433
    mssql_windows_auth: bool = False
    mssql_encrypt: bool = False

    # ---- Cache ----
    cache_path: str = "./cache/semantic_mcp.db"
    cache_enabled: bool = True
    startup_mode: StartupMode = "cache_first"
    background_batch_size: int = Field(default=5, ge=1)
    background_interval_ms: int = Field(default=500, ge=0)
    # Minimum seconds between schema drift probes (throttle window)
    probe_interval_s: float = Field(default=60.0, ge=0.0)

    # ---- Policy ----
    policy_file: Optional[str] = None
    policy_profile: Optional[str] = None

    # ---- Policy overrides ----
    max_rows_returned: int = Field(default=1000, ge=1)
    max_rows_affected: int = Field(default=100, ge=1)
    query_timeout: int = Field(default=30, ge=1)

    # ---- Tool surface ----
    # "all" is the default surface and now resolves to the full consolidated
    # 12-tool set (see server/tools/__init__.py::_GROUP_REGISTRATIONS).
    tool_profile: str = "all"

    # ---- Metrics ----
    metrics_enabled: bool = True

    # ---- v0.5 agent-oriented defaults ----
    default_detail: DetailTier = "brief"
    default_response_mode: ResponseMode = "summary"
    default_token_budget_hint: TokenBudgetHint = DEFAULT_TOKEN_BUDGET_HINT
    direct_execute_enabled: bool = True
    strict_rows_affected_cap: bool = True
    workflow_tools_enabled: bool = True

    # ---- Analyzer router ----
    # regex (default, regex-based) | ast (placeholder; falls back to regex)
    intent_analyzer: Literal["regex", "ast"] = "regex"


_config: Optional[Config] = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config


def reset_config() -> None:
    """Test helper only."""
    global _config
    _config = None
