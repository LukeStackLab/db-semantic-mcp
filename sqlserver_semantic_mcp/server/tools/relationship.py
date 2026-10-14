from mcp.types import Tool

from ...services import relationship_service, semantic_service
from ..app import get_context, register_tool


_CLASSIFICATION_PENALTY = {"bridge": 0.25, "audit": 0.2, "lookup": 0.1}


def register() -> None:
    register_tool(
        Tool(
            name="get_table_relationships",
            description="List inbound + outbound FK relationships for a table.",
            inputSchema={
                "type": "object",
                "properties": {
                    "schema": {"type": "string"},
                    "table":  {"type": "string"},
                },
                "required": ["schema", "table"],
            },
        ),
        _rels,
    )
    register_tool(
        Tool(
            name="find_join_path",
            description=(
                "Find bounded shortest FK join alternatives with complete column "
                "pairs. Parallel roles return ambiguous=true and require a choice. "
                "Set score=true for heuristic ranking; scores never authorize execution."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "from_schema": {"type": "string"},
                    "from_table":  {"type": "string"},
                    "to_schema":   {"type": "string"},
                    "to_table":    {"type": "string"},
                    "max_hops":    {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                    "max_candidates": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                    "score": {"type": "boolean", "default": False,
                              "description": "true = also rank the path "
                                             "(penalises extra/bridge/audit/"
                                             "lookup hops)."},
                },
                "required": ["from_schema", "from_table", "to_schema", "to_table"],
            },
        ),
        _path,
    )
    register_tool(
        Tool(
            name="get_dependency_chain",
            description=(
                "List all tables reachable from a given table via FKs. "
                "schemas param limits the BFS frontier to allowed schemas "
                "(start table always included)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "schema":    {"type": "string"},
                    "table":     {"type": "string"},
                    "max_depth": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
                    "schemas":   {"oneOf": [{"type": "string"},
                                            {"type": "array",
                                             "items": {"type": "string"}}]},
                },
                "required": ["schema", "table"],
            },
        ),
        _chain,
    )


async def _rels(args: dict) -> list[dict]:
    ctx = get_context()
    ctx.policy.authorize_object(args["schema"], args["table"], ctx.cfg.mssql_database)
    return await relationship_service.get_table_relationships(
        ctx.cfg.cache_path, ctx.cfg.mssql_database,
        args["schema"], args["table"], policy=ctx.policy,
    )


async def _path(args: dict) -> dict:
    ctx = get_context()
    for prefix in ("from", "to"):
        ctx.policy.authorize_object(args[f"{prefix}_schema"], args[f"{prefix}_table"], ctx.cfg.mssql_database)
    result = await relationship_service.find_join_candidates(
        ctx.cfg.cache_path, ctx.cfg.mssql_database,
        args["from_schema"], args["from_table"],
        args["to_schema"], args["to_table"],
        max_hops=args.get("max_hops", 5),
        max_candidates=args.get("max_candidates", 5), policy=ctx.policy,
    )
    if not args.get("score"):
        return result

    if not result["found"]:
        return {**result, "confidence": 0.0, "hops": 0, "penalties": [],
                "recommended_tool": "discover_relevant_tables"}

    for candidate in result["candidates"]:
        candidate.update(await _score_path(ctx, candidate["path"]))
    result["confidence_kind"] = "heuristic"
    if not result["ambiguous"]:
        candidate = result["candidates"][0]
        for key in ("confidence", "hops", "penalties"):
            result[key] = candidate[key]
    return result


async def _score_path(ctx, path: list[dict]) -> dict:
    hops = len(path)
    score = 1.0 - (0.15 * max(hops - 1, 0))
    penalties: list[dict] = []
    for edge in path:
        schema, table = edge.get("to_schema"), edge.get("to_table")
        if not schema or not table:
            continue
        cls = await semantic_service.classify_table(
            ctx.cfg.cache_path, ctx.cfg.mssql_database, schema, table,
            policy=ctx.policy)
        pen = _CLASSIFICATION_PENALTY.get(cls.get("type"), 0.0)
        if pen:
            score -= pen
            penalties.append({"at": f"{schema}.{table}",
                              "classification": cls.get("type"), "penalty": pen})
    score = max(0.0, min(1.0, score))
    return {"confidence": round(score, 3), "hops": hops, "penalties": penalties}


async def _chain(args: dict) -> list[dict]:
    ctx = get_context()
    ctx.policy.authorize_object(args["schema"], args["table"], ctx.cfg.mssql_database)
    raw = args.get("schemas")
    if isinstance(raw, str):
        schemas = [raw] if raw else None
    elif isinstance(raw, list):
        schemas = [s for s in raw if isinstance(s, str) and s] or None
    else:
        schemas = None
    return await relationship_service.get_dependency_chain(
        ctx.cfg.cache_path, ctx.cfg.mssql_database,
        args["schema"], args["table"],
        max_depth=args.get("max_depth", 10),
        schemas=schemas,
        policy=ctx.policy,
    )
