import json

from mcp.types import Resource, ResourceTemplate
from pydantic import AnyUrl

from ...services import metadata_service, semantic_service, object_service
from ..app import app, get_context


@app.list_resources()
async def list_resources() -> list[Resource]:
    # Only resources without a tool equivalent are listed here.
    # `get_tables` tool supersedes the old semantic://schema/tables resource.
    return [
        Resource(
            uri=AnyUrl("semantic://summary/database"),
            name="Database summary", mimeType="application/json",
        ),
    ]


@app.list_resource_templates()
async def list_resource_templates() -> list[ResourceTemplate]:
    return [
        ResourceTemplate(
            uriTemplate="semantic://schema/tables/{qualified}",
            name="Table metadata", mimeType="application/json",
        ),
        ResourceTemplate(
            uriTemplate="semantic://analysis/classification/{qualified}",
            name="Table classification", mimeType="application/json",
        ),
        ResourceTemplate(
            uriTemplate="semantic://summary/table/{qualified}",
            name="AI-ready table summary (join-focused)",
            mimeType="application/json",
        ),
        ResourceTemplate(
            uriTemplate="semantic://summary/object/{type}/{qualified}",
            name="AI-ready object summary (reads/writes/depends)",
            mimeType="application/json",
        ),
        ResourceTemplate(
            uriTemplate="semantic://bundle/joining/{qualified}",
            name="Joining bundle for a single table",
            mimeType="application/json",
        ),
    ]


def _split_qualified(qualified: str, uri: str) -> tuple[str, str]:
    parts = qualified.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"Invalid resource URI '{uri}': expected schema.table format"
        )
    return parts[0], parts[1]


@app.read_resource()
async def read_resource(uri: AnyUrl) -> str:
    payload = await _read_resource_payload(uri)
    cfg = get_context().cfg
    if cfg.connection_name:
        return json.dumps({
            "source": {"connection": cfg.connection_name, "database": cfg.mssql_database},
            "data": json.loads(payload),
        }, ensure_ascii=False, default=str, separators=(",", ":"))
    return payload


async def _read_resource_payload(uri: AnyUrl) -> str:
    ctx = get_context()
    cp = ctx.cfg.cache_path
    db = ctx.cfg.mssql_database
    s = str(uri)

    if s == "semantic://schema/tables":
        return json.dumps(await metadata_service.list_tables(cp, db), default=str)

    if s == "semantic://summary/database":
        return json.dumps(
            await metadata_service.database_summary(cp, db), default=str,
        )

    if s.startswith("semantic://schema/tables/"):
        qualified = s[len("semantic://schema/tables/"):]
        schema, table = _split_qualified(qualified, s)
        return json.dumps(
            await metadata_service.describe_table(cp, db, schema, table),
            default=str,
        )

    if s.startswith("semantic://analysis/classification/"):
        qualified = s[len("semantic://analysis/classification/"):]
        schema, table = _split_qualified(qualified, s)
        return json.dumps(
            await semantic_service.classify_table(cp, db, schema, table),
            default=str,
        )

    if s.startswith("semantic://analysis/dependencies/"):
        qualified = s[len("semantic://analysis/dependencies/"):]
        parts = qualified.split("/")
        if len(parts) != 2:
            raise ValueError(
                f"Invalid resource URI '{s}': expected <type>/<schema>.<name>"
            )
        obj_type = parts[0].upper()
        schema, name = _split_qualified(parts[1], s)
        return json.dumps(
            await object_service.describe_object(schema, name, obj_type, ctx.cfg),
            default=str,
        )

    if s.startswith("semantic://summary/table/"):
        qualified = s[len("semantic://summary/table/"):]
        schema, table = _split_qualified(qualified, s)
        summary = await semantic_service.summarize_for_joining(
            cp, db, schema, table,
        )
        return json.dumps(summary or {}, default=str)

    if s.startswith("semantic://summary/object/"):
        rest = s[len("semantic://summary/object/"):]
        parts = rest.split("/", 1)
        if len(parts) != 2:
            raise ValueError(
                f"Invalid resource URI '{s}': expected <type>/<schema>.<name>"
            )
        obj_type = parts[0].upper()
        schema, name = _split_qualified(parts[1], s)
        obj = await object_service.describe_object(
            schema, name, obj_type, ctx.cfg,
        )
        payload = {
            "object": f"{schema}.{name}",
            "type": obj_type,
            "status": obj.get("status") if obj else None,
            "reads": list((obj or {}).get("read_tables", []) or []),
            "writes": list((obj or {}).get("write_tables", []) or []),
            "depends_on": list((obj or {}).get("dependencies", []) or []),
        }
        return json.dumps(payload, default=str)

    if s.startswith("semantic://bundle/joining/"):
        qualified = s[len("semantic://bundle/joining/"):]
        schema, table = _split_qualified(qualified, s)
        bundle = await ctx.workflow.bundle_context_for_next_step(
            [{"kind": "table", "schema": schema, "table": table}],
            goal="joining",
        )
        return json.dumps(bundle, default=str)

    raise ValueError(f"Unknown resource URI: {s}")
