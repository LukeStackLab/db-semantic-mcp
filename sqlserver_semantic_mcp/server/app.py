import json
import logging
from typing import Any, Awaitable, Callable, Optional

from mcp.server import Server
from mcp.types import Tool, TextContent

from ..config import Config, get_config
from ..infrastructure.cache import revalidation
from ..services import metrics_service
from ..services.policy_service import PolicyService
from ..services.query_service import QueryService
from ..workflows.facade import WorkflowFacade
from .compact import compact

logger = logging.getLogger(__name__)


_WORKFLOW_TOOLS = frozenset({
    "plan_or_execute_query",
    "discover_relevant_tables",
    "find_join_path",
})


class Context:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.policy = PolicyService(cfg)
        self.query = QueryService(self.policy, cfg)
        self.workflow = WorkflowFacade(cfg, self.policy, self.query)


_ctx: Context | None = None


def get_context() -> Context:
    global _ctx
    if _ctx is None:
        _ctx = Context(get_config())
        _ctx.policy.load()
    return _ctx


def reset_context() -> None:
    global _ctx
    _ctx = None


app = Server("sqlserver-semantic-mcp")

ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]
_TOOL_REGISTRY: dict[str, tuple[Tool, ToolHandler]] = {}


def register_tool(tool: Tool, handler: ToolHandler) -> None:
    if tool.name in _TOOL_REGISTRY:
        raise ValueError(f"Duplicate tool registration: {tool.name}")
    _TOOL_REGISTRY[tool.name] = (tool, handler)


@app.list_tools()
async def _list_tools() -> list[Tool]:
    return [t for (t, _) in _TOOL_REGISTRY.values()]


def _infer_workflow_metrics(name: str, shaped: Any) -> dict[str, Any]:
    """Extract workflow-aware fields from the response envelope, if any."""
    extras: dict[str, Any] = {}
    if not isinstance(shaped, dict):
        return extras
    if name in _WORKFLOW_TOOLS:
        extras["route_type"] = shaped.get("kind")
    for key in ("detail", "response_mode", "token_budget_hint",
                "next_action", "bundle_key"):
        if key in shaped:
            extras[key] = shaped.get(key)
    data = shaped.get("data")
    if isinstance(data, dict):
        if "path" in data and "route_type" not in extras:
            extras["route_type"] = data.get("path")
        if name == "plan_or_execute_query":
            extras["was_direct_execute"] = (
                data.get("path") == "direct_execute"
                and bool(data.get("executed"))
            )
        if "response_mode" not in extras and "response_mode" in data:
            extras["response_mode"] = data.get("response_mode")
    if "bundle_key" in shaped:
        extras["bundle_used"] = True
    return extras


@app.call_tool()
async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name not in _TOOL_REGISTRY:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    _t, handler = _TOOL_REGISTRY[name]
    cfg = get_config()
    if name != "refresh_schema_cache":  # the refresh tool warms up itself
        await revalidation.maybe_revalidate(get_config())
    try:
        result = await handler(arguments or {})
        shaped = compact(result)
        array_length = len(shaped) if isinstance(shaped, list) else None
        if cfg.connection_name:
            source = {"connection": cfg.connection_name, "database": cfg.mssql_database}
            shaped = {**shaped, "source": source} if isinstance(shaped, dict) else {"data": shaped, "source": source}
        text = json.dumps(
            shaped, ensure_ascii=False, default=str, separators=(",", ":"),
        )
        if get_config().metrics_enabled:
            try:
                extras = _infer_workflow_metrics(name, shaped)
                await metrics_service.record_metric(
                    get_config().cache_path, name,
                    response_bytes=len(text.encode("utf-8")),
                    array_length=array_length,
                    fields_returned=len(shaped) if isinstance(shaped, dict) else None,
                    **extras,
                )
            except Exception:
                logger.exception("metrics_service.record_metric failed")
        return [TextContent(type="text", text=text)]
    except Exception as e:
        message = str(e)
        if cfg.mssql_password:
            message = message.replace(cfg.mssql_password, "***")
        error = {"error": message}
        if cfg.connection_name:
            logger.error("Tool %s failed for connection %s: %s", name, cfg.connection_name, message)
            error["source"] = {"connection": cfg.connection_name, "database": cfg.mssql_database}
        else:
            logger.exception("Tool %s raised", name)
        return [TextContent(type="text", text=json.dumps(error, ensure_ascii=False))]
