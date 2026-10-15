import hashlib

from mcp.types import Tool

from ...services import object_service
from ..app import get_context, register_tool


def register() -> None:
    register_tool(
        Tool(
            name="describe_object",
            description="Describe a VIEW, PROCEDURE, or FUNCTION: dependencies, IO, and optional definition.",
            inputSchema={
                "type": "object",
                "properties": {
                    "schema": {"type": "string"},
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": ["VIEW", "PROCEDURE", "FUNCTION"]},
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


def _project_describe_object(obj: dict, include_definition: bool, max_definition_bytes: int) -> dict:
    schema = obj.get("schema", "")
    name = obj.get("object_name", "")
    out: dict = {
        "object": f"{schema}.{name}",
        "type": obj.get("object_type") or obj.get("type"),
        "depends_on": list(obj.get("dependencies", []) or []),
        "definition_bytes": obj.get("definition_bytes"),
        "definition_hash": obj.get("definition_hash"),
        "read_tables": list(obj.get("read_tables", []) or []),
        "write_tables": list(obj.get("write_tables", []) or []),
        "affected_tables": list(obj.get("affected_tables", []) or []),
        "description": obj.get("description"),
        "status": obj.get("status"),
    }
    if obj.get("status") == "error":
        out["error_message"] = obj.get("error_message")
    for key in ("definition_withheld", "definition_omission_reason", "dependencies_omitted"):
        if key in obj:
            out[key] = obj[key]
    definition = obj.get("definition")
    if include_definition and definition:
        if len(definition.encode("utf-8")) > max_definition_bytes:
            definition = definition.encode("utf-8")[:max_definition_bytes].decode("utf-8", "ignore")
            out["definition_truncated"] = True
        out["definition"] = definition
    return out


async def _describe_object(args: dict, object_type: str) -> dict:
    ctx = get_context()
    include = bool(args.get("include_definition", False))
    obj = await object_service.describe_object(
        args["schema"],
        args["name"],
        object_type,
        ctx.cfg,
        policy=ctx.policy,
    )
    obj = _attach_hash_and_bytes(obj)
    return _project_describe_object(obj, include, ctx.cfg.max_response_bytes)


async def _describe_object_tool(args: dict) -> dict:
    return await _describe_object(args, args["type"])
