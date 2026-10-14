import hashlib

from mcp.types import Tool

from ...services import object_service
from ..app import get_context, register_tool
from .shape import project_describe_object, resolve_detail

_DETAIL_PROP = {
    "type": "string", "enum": ["brief", "standard", "full"], "default": "brief",
    "description": "brief=depends_on only; standard=+reads/writes/impact; "
                   "full=+definition.",
}


def register() -> None:
    register_tool(
        Tool(
            name="describe_object",
            description=(
                "Describe a VIEW / PROCEDURE / FUNCTION: dependencies, "
                "reads/writes, and (at full detail) its definition."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "schema": {"type": "string"},
                    "name":   {"type": "string"},
                    "type":   {"type": "string",
                               "enum": ["VIEW", "PROCEDURE", "FUNCTION"]},
                    "detail": _DETAIL_PROP,
                    "include_definition": {"type": "boolean", "default": False},
                },
                "required": ["schema", "name", "type"],
            },
        ),
        _describe_object_tool,
    )


def _attach_hash_and_bytes(obj: dict) -> dict:
    definition = obj.get("definition")
    if not isinstance(definition, str) or not definition:
        return obj
    if "definition_hash" in obj and "definition_bytes" in obj:
        return obj
    encoded = definition.encode("utf-8")
    out = dict(obj)
    out.setdefault("definition_hash", hashlib.sha1(encoded).hexdigest()[:8])
    out.setdefault("definition_bytes", len(encoded))
    return out


async def _describe_object(args: dict, object_type: str) -> dict:
    ctx = get_context()
    detail = resolve_detail(args)
    include = bool(args.get("include_definition", False))
    obj = await object_service.describe_object(
        args["schema"], args["name"], object_type, ctx.cfg, policy=ctx.policy,
    )
    obj = _attach_hash_and_bytes(obj)
    return project_describe_object(obj, detail=detail, include_definition=include)


async def _describe_object_tool(args: dict) -> dict:
    return await _describe_object(args, args["type"])
