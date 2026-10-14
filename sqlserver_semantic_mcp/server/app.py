import json
import logging
from time import perf_counter
from typing import Any, Awaitable, Callable, Optional

from mcp.server import Server
from mcp.types import Tool, TextContent, CallToolResult, ToolAnnotations

from ..config import Config, get_config
from ..infrastructure.cache import revalidation
from ..infrastructure.workers import take_worker_timings
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
    cfg = get_config()
    if _ctx is None or _ctx.cfg is not cfg:
        candidate = Context(cfg)
        candidate.policy.load()
        _ctx = candidate
    return _ctx


def reset_context() -> None:
    global _ctx
    _ctx = None


app = Server("sqlserver-semantic-mcp")

ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]
_TOOL_REGISTRY: dict[str, tuple[Tool, ToolHandler]] = {}
_OUTPUT_VALIDATORS: dict[str, Any] = {}


def register_tool(tool: Tool, handler: ToolHandler) -> None:
    if tool.name in _TOOL_REGISTRY:
        raise ValueError(f"Duplicate tool registration: {tool.name}")
    if tool.outputSchema is None:
        # Advertise each tool's own contract rather than repeating unrelated
        # fields across every descriptor loaded into a client's context.
        properties = {}
        required = []
        if tool.name in {"get_tables", "get_table_relationships", "get_dependency_chain", "detect_lookup_tables"}:
            properties["data"] = {"type": "array", "items": {"type": "object"}}
            required = ["data"]
        elif tool.name in {"plan_or_execute_query", "discover_relevant_tables"}:
            properties.update(kind={"const": tool.name}, data={"type": "object"})
            required = ["kind", "data"]
        elif tool.name == "describe_table":
            properties.update(table={"type": "string"}, column_count={"type": "integer"}, columns={"type": "array"})
        elif tool.name == "find_join_path":
            properties.update(found={"type": "boolean"}, ambiguous={"type": "boolean"}, candidates={"type": "array"})
        tool.outputSchema = {"type": "object"}
        if properties:
            tool.outputSchema["properties"] = properties
        if required:
            tool.outputSchema["required"] = required
    if tool.annotations is None:
        mutating = tool.name in {"plan_or_execute_query", "refresh_schema_cache", "tool_metrics"}
        tool.annotations = ToolAnnotations(readOnlyHint=not mutating, openWorldHint=False)
    from jsonschema.validators import validator_for
    validator = validator_for(tool.outputSchema)
    validator.check_schema(tool.outputSchema)
    _OUTPUT_VALIDATORS[tool.name] = validator(tool.outputSchema)
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
        if "path" in data:
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


def _failure_category(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    if result.get("allowed") is False:
        return "policy_denied"
    if result.get("failure_category"):
        return result["failure_category"]
    for key in ("data", "validation"):
        category = _failure_category(result.get(key))
        if category:
            return category
    if result.get("error") or result.get("status") == "error":
        return "execution_error"
    return None


async def _dispatch_tool(name: str, arguments: dict) -> CallToolResult:
    if name not in _TOOL_REGISTRY:
        payload = {"error": f"Unknown tool: {name}"}
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                              structuredContent=payload, isError=True)
    tool, handler = _TOOL_REGISTRY[name]
    cfg = get_config()
    started = perf_counter()
    take_worker_timings()
    array_length = None
    error_category = None
    try:
        if name != "refresh_schema_cache":
            await revalidation.maybe_revalidate(cfg)
        result = await handler(arguments or {})
        error_category = _failure_category(result)
        cache_metadata = result.get("cache", {}) if isinstance(result, dict) else {}
        shaped = compact(result)
        array_length = len(shaped) if isinstance(shaped, list) else None
        if not isinstance(shaped, dict):
            shaped = {"data": shaped} if shaped is not None else {"found": False}
        if cfg.connection_name:
            shaped["source"] = {"connection": cfg.connection_name, "database": cfg.mssql_database}
        if cfg.cache_enabled and name not in {"tool_metrics", "get_execution_policy"}:
            try:
                shaped["cache"] = revalidation.merge_cache_metadata(
                    cfg, cache_metadata, await revalidation.cache_freshness(cfg),
                )
            except Exception:
                # Cache diagnostics must never turn an already committed
                # operation into a failure that encourages a duplicate write.
                shaped["cache"] = revalidation.merge_cache_metadata(
                    cfg, cache_metadata, {"freshness_unknown": True},
                )
                logger.warning("Cache freshness unavailable for tool %s", name)
        # Normalize values exactly once; JSON text and structuredContent agree.
        text = json.dumps(shaped, ensure_ascii=False, default=str, separators=(",", ":"))
        shaped = json.loads(text)
        if not error_category and tool.outputSchema:
            _OUTPUT_VALIDATORS[name].validate(shaped)
    except Exception as exc:
        message = str(exc)
        if cfg.mssql_password:
            message = message.replace(cfg.mssql_password, "***")
        shaped = {"error": message}
        if cfg.connection_name:
            shaped["source"] = {"connection": cfg.connection_name, "database": cfg.mssql_database}
        error_category = "invalid_arguments" if isinstance(exc, (ValueError, KeyError, TypeError)) else "execution_error"
        # Raw driver tracebacks may include SQL or credentials.
        logger.error("Tool %s failed (%s)", name, error_category)
        text = json.dumps(shaped, ensure_ascii=False, separators=(",", ":"))
    if cfg.metrics_enabled:
        try:
            extras = _infer_workflow_metrics(name, shaped)
            data = shaped.get("data") if isinstance(shaped.get("data"), dict) else shaped
            extras.pop("bundle_key", None)
            extras["detail"] = arguments.get("detail", shaped.get("detail", cfg.default_detail))
            extras["response_mode"] = data.get("response_mode") or arguments.get("return_mode") or cfg.default_response_mode
            extras["token_budget_hint"] = data.get("token_budget_hint") or arguments.get("token_budget_hint") or cfg.default_token_budget_hint
            cache = shaped.get("cache", {})
            await metrics_service.record_metric(
                cfg.cache_path, name, response_bytes=len(text.encode("utf-8")),
                array_length=array_length, fields_returned=len(shaped),
                elapsed_ms=(perf_counter() - started) * 1000,
                fetched_rows=data.get("fetched_row_count"),
                cache_hit=cache.get("hit"), error_category=error_category,
                **(take_worker_timings() or {}), **extras)
        except Exception:
            logger.warning("Metric recording failed for tool %s", name)
    return CallToolResult(content=[TextContent(type="text", text=text)],
                          structuredContent=shaped, isError=error_category is not None)


@app.call_tool()
async def _call_tool_result(name: str, arguments: dict) -> CallToolResult:
    return await _dispatch_tool(name, arguments)


async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Compatibility adapter for callers consuming content blocks directly."""
    return (await _dispatch_tool(name, arguments)).content
