import hashlib

from mcp.types import Tool

from ...services import object_service
from ..app import get_context, register_tool


def register() -> None:
    register_tool(
        Tool(
            name="describe_object",
            description="Describe native objects, including Oracle PACKAGE/BODY and SYNONYM: authorized dependencies, alias paths and available definitions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "schema": {"type": "string"},
                    "name": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": [
                            "VIEW",
                            "MATERIALIZED VIEW",
                            "PROCEDURE",
                            "FUNCTION",
                            "PACKAGE",
                            "PACKAGE BODY",
                            "SYNONYM",
                        ],
                    },
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
    if obj.get("query_reference"):
        from ...services.oracle_metadata import qualified

        out["object"] = qualified(schema, name)
    if obj.get("status") == "error":
        out["error_message"] = obj.get("error_message")
    for key in (
        "definition_withheld",
        "definition_omission_reason",
        "dependencies_omitted",
        "dependency_completeness",
        "io_completeness",
        "chain",
        "target",
        "canonical_source",
        "query_reference",
        "resolution_chain",
        "resolved_target",
    ):
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
    if ctx.cfg.database_type == "oracle" and obj.get("status") != "error":
        from ...services.oracle_metadata import query_reference

        target = obj.get("resolved_target")
        if args["schema"] == "PUBLIC" and ctx.cfg.source_id != "local" and target:
            obj["query_reference"] = query_reference(
                ctx.cfg.for_source(target["source"]), target["schema"], target["name"]
            )
        else:
            obj["query_reference"] = query_reference(ctx.cfg, args["schema"], args["name"])
        obj["canonical_source"] = obj.get("resolved_target", {}).get("source", ctx.cfg.source_id)
    obj = _attach_hash_and_bytes(obj)
    return _project_describe_object(obj, include, ctx.cfg.max_response_bytes)


async def _describe_object_tool(args: dict) -> dict:
    return await _describe_object(args, args["type"])
