import json
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from mcp.types import Resource, ResourceTemplate
from pydantic import AnyUrl

from ...infrastructure.cache import revalidation
from ...services import metadata_service, object_service, semantic_service
from ..app import app, get_context


@app.list_resources()
async def list_resources() -> list[Resource]:
    # Only resources without a tool equivalent are listed here.
    # `get_tables` tool supersedes the old semantic://schema/tables resource.
    return [
        Resource(
            uri=AnyUrl("semantic://summary/database"),
            name="Database summary",
            mimeType="application/json",
        ),
    ]


@app.list_resource_templates()
async def list_resource_templates() -> list[ResourceTemplate]:
    return [
        ResourceTemplate(
            uriTemplate="semantic://schema/tables/{qualified}{?source}",
            name="Table metadata",
            mimeType="application/json",
        ),
        ResourceTemplate(
            uriTemplate="semantic://analysis/classification/{qualified}{?source}",
            name="Table classification",
            mimeType="application/json",
        ),
        ResourceTemplate(
            uriTemplate="semantic://summary/table/{qualified}{?source}",
            name="AI-ready table summary (join-focused)",
            mimeType="application/json",
        ),
        ResourceTemplate(
            uriTemplate="semantic://summary/object/{type}/{qualified}{?source}",
            name="AI-ready object summary (reads/writes/depends)",
            mimeType="application/json",
        ),
        ResourceTemplate(
            uriTemplate="semantic://bundle/joining/{qualified}{?source}",
            name="Joining bundle for a single table",
            mimeType="application/json",
        ),
    ]


def _split_qualified(qualified: str, uri: str) -> tuple[str, str]:
    from ...policy.scope import split_object_name

    if get_context().cfg.database_type == "oracle":
        from ...policy.intents.oracle_analyzer import oracle_scope_entry

        ref = oracle_scope_entry(unquote(qualified))
        parts = (ref.schema, ref.name)
    else:
        parts = split_object_name(unquote(qualified))
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid resource URI '{uri}': expected schema.table format")
    return parts[0], parts[1]


@app.read_resource()
async def read_resource(uri: AnyUrl) -> str:
    from ..source_context import bind_source

    parsed = urlsplit(str(uri))
    query = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
    if set(query) - {"source"} or len(query.get("source", [])) > 1:
        raise ValueError("Invalid resource source selector")
    async with bind_source(get_context(), query.get("source", ["local"])[0]):
        return await _read_resource_bound(AnyUrl(urlunsplit(parsed._replace(query=""))))


async def _read_resource_bound(uri: AnyUrl) -> str:
    payload = await _read_resource_payload(uri)
    cfg = get_context().cfg
    data = json.loads(payload)
    if cfg.cache_enabled:
        await revalidation.maybe_revalidate(cfg)
        try:
            freshness = await revalidation.cache_freshness(cfg)
        except Exception:  # noqa: BLE001 (cache diagnostics must not fail operations)
            freshness = {"freshness_unknown": True}
        if isinstance(data, dict):
            data["cache"] = revalidation.merge_cache_metadata(cfg, data.get("cache", {}), freshness)
        else:
            data = {"data": data, "cache": revalidation.merge_cache_metadata(cfg, {}, freshness)}
    if cfg.connection_name or cfg.database_type == "oracle":
        from ..source_context import source_metadata

        return json.dumps(
            {
                "source": source_metadata(cfg),
                "data": data,
            },
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
    return json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))


async def _read_resource_payload(uri: AnyUrl) -> str:
    ctx = get_context()
    cp = ctx.cfg.cache_path
    db = ctx.cfg.database
    s = str(uri)

    if s == "semantic://schema/tables":
        return json.dumps(await metadata_service.list_tables(cp, db, policy=ctx.policy), default=str)

    if s == "semantic://summary/database":
        return json.dumps(
            await metadata_service.database_summary(cp, db, policy=ctx.policy),
            default=str,
        )

    if s.startswith("semantic://schema/tables/"):
        qualified = s[len("semantic://schema/tables/") :]
        schema, table = _split_qualified(qualified, s)
        return json.dumps(
            await metadata_service.describe_table(cp, db, schema, table, policy=ctx.policy),
            default=str,
        )

    if s.startswith("semantic://analysis/classification/"):
        qualified = s[len("semantic://analysis/classification/") :]
        schema, table = _split_qualified(qualified, s)
        return json.dumps(
            await semantic_service.classify_table(cp, db, schema, table, policy=ctx.policy),
            default=str,
        )

    if s.startswith("semantic://analysis/dependencies/"):
        qualified = s[len("semantic://analysis/dependencies/") :]
        parts = qualified.split("/")
        if len(parts) != 2:
            raise ValueError(f"Invalid resource URI '{s}': expected <type>/<schema>.<name>")
        obj_type = unquote(parts[0]).upper()
        schema, name = _split_qualified(parts[1], s)
        return json.dumps(
            await object_service.describe_object(schema, name, obj_type, ctx.cfg, policy=ctx.policy),
            default=str,
        )

    if s.startswith("semantic://summary/table/"):
        qualified = s[len("semantic://summary/table/") :]
        schema, table = _split_qualified(qualified, s)
        summary = await semantic_service.summarize_for_joining(
            cp,
            db,
            schema,
            table,
            policy=ctx.policy,
        )
        return json.dumps(summary or {}, default=str)

    if s.startswith("semantic://summary/object/"):
        rest = s[len("semantic://summary/object/") :]
        parts = rest.split("/", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid resource URI '{s}': expected <type>/<schema>.<name>")
        obj_type = unquote(parts[0]).upper()
        schema, name = _split_qualified(parts[1], s)
        obj = await object_service.describe_object(
            schema,
            name,
            obj_type,
            ctx.cfg,
            policy=ctx.policy,
        )
        payload = {
            "object": f"{schema}.{name}",
            "type": obj_type,
            "status": obj.get("status") if obj else None,
            "reads": list((obj or {}).get("read_tables", []) or []),
            "writes": list((obj or {}).get("write_tables", []) or []),
            "depends_on": list((obj or {}).get("dependencies", []) or []),
            **{
                key: obj[key]
                for key in (
                    "dependencies_omitted",
                    "definition_withheld",
                    "definition_omission_reason",
                    "io_completeness",
                    "dependency_completeness",
                    "resolution_chain",
                    "resolved_target",
                )
                if obj and key in obj
            },
        }
        return json.dumps(payload, default=str)

    if s.startswith("semantic://bundle/joining/"):
        qualified = s[len("semantic://bundle/joining/") :]
        schema, table = _split_qualified(qualified, s)
        bundle = await ctx.workflow.bundle_context_for_next_step(
            [{"kind": "table", "schema": schema, "table": table}],
            goal="joining",
        )
        return json.dumps(bundle, default=str)

    raise ValueError(f"Unknown resource URI: {s}")
