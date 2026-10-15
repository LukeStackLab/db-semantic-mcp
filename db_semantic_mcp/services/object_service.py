import logging
import re
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from ..config import Config, get_config
from ..infrastructure.cache.semantic import (
    get_object_definition,
    upsert_object_definition,
)
from ..infrastructure.cache.structural import read_schema_version
from ..infrastructure.connection import fetch_all, fetch_one
from ..infrastructure.queries.object_queries import (
    GET_OBJECT_DEFINITION,
    GET_OBJECT_DEPENDENCIES,
)
from ..infrastructure.workers import run_db_work
from ..policy.analyzer import (
    _IDENT,
    _strip_comments,
)
from ..policy.intents.ast_analyzer import (
    _METADATA_FUNCTIONS,
    _check_opaque_sources,
    _StrictTSQL,
)
from ..policy.scope import split_object_name, table_reference
from .policy_service import PolicyService

logger = logging.getLogger(__name__)


_WRITE_PATTERNS = [
    rf"\bUPDATE\s+({_IDENT})",
    rf"\bINSERT\s+INTO\s+({_IDENT})",
    rf"\bDELETE\s+FROM\s+({_IDENT})",
    rf"\bMERGE\s+(?:INTO\s+)?({_IDENT})",
    rf"\bTRUNCATE\s+TABLE\s+({_IDENT})",
]


def split_read_write(sql: str) -> tuple[list[str], list[str]]:
    """Split a SQL body (e.g. a PROCEDURE definition) into (read_tables, write_tables).

    Regex-based. Scans the entire SQL for write-operation patterns (UPDATE/INSERT/
    DELETE/MERGE/TRUNCATE TABLE) and for read-source patterns (FROM/JOIN).
    Write targets are excluded from reads even if they also appear as FROM aliases
    in the same statement (write-intent wins).

    Known limitations: CTE names may appear as reads; dynamic SQL is invisible.
    Returns ([], []) on empty input.
    """
    if not sql or not sql.strip():
        return [], []

    clean = _strip_comments(sql)

    writes: list[str] = []
    for pat in _WRITE_PATTERNS:
        writes.extend(re.findall(pat, clean, re.IGNORECASE))

    # Read sources = FROM / JOIN, excluding DELETE FROM target
    # Strip DELETE FROM fragments so they don't double-count
    read_scan = re.sub(
        rf"\bDELETE\s+FROM\s+{_IDENT}",
        "",
        clean,
        flags=re.IGNORECASE,
    )
    reads: list[str] = []
    reads.extend(re.findall(rf"\bFROM\s+({_IDENT})", read_scan, re.IGNORECASE))
    reads.extend(re.findall(rf"\bJOIN\s+({_IDENT})", read_scan, re.IGNORECASE))

    # Dedup preserving order
    def _dedup(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for t in items:
            k = t.lower()
            if k not in seen:
                seen.add(k)
                out.append(t)
        return out

    writes_d = _dedup(writes)
    # Reads: dedup, then remove any table that is also in writes (write-intent wins)
    write_keys = {w.lower() for w in writes_d}
    reads_d = [r for r in _dedup(reads) if r.lower() not in write_keys]

    return reads_d, writes_d


def _augment_read_write(obj: dict) -> dict:
    """Add read_tables/write_tables derived from the cached definition."""
    if not obj:
        return obj
    definition = obj.get("definition")
    if isinstance(definition, str) and definition:
        try:
            reads, writes = split_read_write(definition)
        except Exception:  # noqa: BLE001 (controlled error replacement)
            logger.exception("split_read_write failed; falling back")
            reads, writes = obj.get("dependencies", []) or [], []
        out = dict(obj)
        out["read_tables"] = reads
        out["write_tables"] = writes
        # affected_tables aliases write_tables for older consumers.
        out["affected_tables"] = writes
        return out
    return obj


async def describe_object(
    schema: str,
    object_name: str,
    object_type: str,
    cfg: Config | None = None,
    policy: PolicyService | None = None,
) -> dict:
    cfg = cfg or get_config()
    if cfg.database_type == "oracle":
        return await run_db_work(cfg, _describe_oracle, cfg, schema, object_name, object_type, policy)
    db = cfg.database
    if policy is not None:
        policy.authorize_object(schema, object_name, database=db)
    ver = await read_schema_version(cfg.cache_path, db)
    object_hash = ver["object_hash"] if ver else ""

    cached = await get_object_definition(
        cfg.cache_path,
        db,
        schema,
        object_name,
        object_type,
    )
    if cached and cached["status"] in {"ready", "restricted"} and cached.get("object_hash") == object_hash:
        if cached["status"] == "restricted":
            cached.update(
                definition_withheld=True,
                dependencies_omitted=True,
                definition_omission_reason="Definition contains restricted or unresolved object references",
            )
        scoped = _apply_scope(_augment_read_write(cached), policy, db, schema, object_name)
        if policy is not None and scoped.get("definition_withheld") and cached["status"] != "restricted":
            await upsert_object_definition(
                cfg.cache_path,
                db,
                schema,
                object_name,
                object_type,
                object_hash=object_hash,
                status="restricted",
                definition=None,
                dependencies=scoped.get("dependencies", []),
                affected_tables=scoped.get("affected_tables", []),
            )
            scoped["status"] = "restricted"
        return scoped

    qualified = f"{schema}.{object_name}"
    try:
        definition, dep_rows = await run_db_work(cfg, _fetch_object, cfg, qualified)
        dependencies = [f"{r[0]}.{r[1]}" for r in dep_rows if r[0]]
        affected = [f"{r[0]}.{r[1]}" for r in dep_rows if r[2] and "TABLE" in str(r[2]).upper()]
        scoped = _apply_scope(
            _augment_read_write(
                {
                    "database_name": db,
                    "schema": schema,
                    "object_name": object_name,
                    "object_type": object_type,
                    "object_hash": object_hash,
                    "status": "ready",
                    "definition": definition,
                    "dependencies": dependencies,
                    "affected_tables": affected,
                }
            ),
            policy,
            db,
            schema,
            object_name,
        )
        status = "restricted" if scoped.get("definition_withheld") else "ready"
        scoped["status"] = status
        await upsert_object_definition(
            cfg.cache_path,
            db,
            schema,
            object_name,
            object_type,
            object_hash=object_hash,
            status=status,
            definition=scoped.get("definition"),
            dependencies=scoped.get("dependencies", []),
            affected_tables=scoped.get("affected_tables", []),
        )
        return scoped
    except Exception as e:  # noqa: BLE001 (security boundary – never echo driver errors)
        message = str(e)
        if cfg.password:
            message = message.replace(cfg.password, "***")
        logger.error("describe_object failed: %s", message)
        await upsert_object_definition(
            cfg.cache_path,
            db,
            schema,
            object_name,
            object_type,
            object_hash=object_hash,
            status="error",
            error_message=message,
        )
        return {"status": "error", "error_message": "Unable to load object metadata" if policy else message}


def _fetch_object(cfg: Config, qualified: str) -> tuple[str | None, list]:
    """Both complete sync connection lifecycles run in the same DB worker."""
    row = fetch_one(cfg, GET_OBJECT_DEFINITION, (qualified,))
    definition = row[0] if row and row[0] else None
    return definition, fetch_all(cfg, GET_OBJECT_DEPENDENCIES, (qualified,))


def _reference_allowed(reference: str, policy: PolicyService, database: str) -> bool:
    try:
        parts = split_object_name(reference)
        name = parts[-1]
        schema = parts[-2] if len(parts) >= 2 else ""
        return policy.object_allowed(schema, name, database=parts[0] if len(parts) == 3 else database)
    except Exception:  # noqa: BLE001 (controlled error replacement)
        return False


def _module_target(statement):
    module_target = None
    if isinstance(statement, exp.Create) and str(statement.args.get("kind", "")).upper() in {
        "VIEW",
        "PROCEDURE",
        "FUNCTION",
    }:
        module_target = statement.this
        while isinstance(module_target, (exp.StoredProcedure, exp.UserDefinedFunction, exp.Schema)):
            module_target = module_target.this
    return module_target


def _has_forbidden_command(statement) -> bool:
    _check_opaque_sources(statement)
    return any(isinstance(node, (exp.Command, exp.Execute)) for node in statement.walk())


def _has_disallowed_metadata_functions(statement, policy: PolicyService) -> bool:
    metadata_functions = {
        function.name.upper() if isinstance(function, exp.Anonymous) else function.sql_name().upper()
        for function in statement.find_all(exp.Func)
    } & _METADATA_FUNCTIONS
    if metadata_functions:
        scope = policy.current_policy().scope
        return any((scope.allowed_databases, scope.allowed_schemas, scope.allowed_tables, scope.denied_tables))
    return False


def _has_disallowed_qualified_functions(statement, policy: PolicyService, database: str) -> bool:
    for function in statement.find_all(exp.Dot):
        if not isinstance(function.expression, exp.Func):
            continue
        parts = [function.expression]
        qualifier = function.this
        while isinstance(qualifier, exp.Dot):
            parts.insert(0, qualifier.expression)
            qualifier = qualifier.this
        parts.insert(0, qualifier)
        if len(parts) not in (2, 3) or any(not isinstance(part, exp.Identifier) for part in parts[:-1]):
            return True
        function_db = parts[0].name if len(parts) == 3 else database
        if not policy.object_allowed(parts[-2].name, parts[-1].name, database=function_db):
            return True
    return False


def _has_disallowed_tables(
    statement, policy: PolicyService, database: str, schema: str, name: str, module_target: Any
) -> bool:
    cte_refs = set()
    for scope in traverse_scope(statement):
        for node, source in scope.selected_sources.values():
            if isinstance(node, exp.Table) and isinstance(source, Scope):
                cte_refs.add(id(node))
    for table in statement.find_all(exp.Table):
        reference = table_reference(table)
        if id(table) in cte_refs:
            continue
        # Only this root module's own target was authorized at entry.
        # A nested CREATE or a same-named target in another DB is a
        # separate physical object and still requires a scope check.
        if (
            table is module_target
            and reference.name == name
            and reference.schema in {"", schema}
            and (reference.database or database) == database
        ):
            continue
        if not policy.object_allowed(reference.schema, reference.name, database=reference.database or database):
            return True
    return False


def _statement_allowed(statement, policy: PolicyService, database: str, schema: str, name: str) -> bool:
    if _has_forbidden_command(statement):
        return False
    if _has_disallowed_metadata_functions(statement, policy):
        return False
    if _has_disallowed_qualified_functions(statement, policy, database):
        return False
    module_target = _module_target(statement)
    if _has_disallowed_tables(statement, policy, database, schema, name, module_target):
        return False
    return True


def _definition_allowed(
    definition: str,
    policy: PolicyService,
    database: str,
    schema: str,
    name: str,
) -> bool:
    """Withhold opaque definitions when their physical scope cannot be proven."""
    try:
        statements = _StrictTSQL().parse(definition, error_level=sqlglot.ErrorLevel.RAISE)
        for statement in statements:
            if statement is None:
                continue
            if not _statement_allowed(statement, policy, database, schema, name):
                return False
        return True
    except Exception:  # noqa: BLE001 (controlled error replacement)
        return False


def _apply_scope(
    obj: dict,
    policy: PolicyService | None,
    database: str,
    schema: str,
    name: str,
) -> dict:
    if policy is None:
        return obj
    out = dict(obj)
    hidden = False
    for key in ("dependencies", "read_tables", "write_tables", "affected_tables"):
        values = obj.get(key) or []
        filtered = [value for value in values if _reference_allowed(value, policy, database)]
        hidden |= len(filtered) != len(values)
        out[key] = filtered
    definition = obj.get("definition")
    if definition and (hidden or not _definition_allowed(definition, policy, database, schema, name)):
        out["definition"] = None
        out["definition_withheld"] = True
        out["definition_omission_reason"] = "Definition contains restricted or unresolved object references"
        # Regex-derived names can be incomplete for three/four-part references.
        # Once a body is withheld, retain only catalog-confirmed allowed names.
        confirmed = {value.casefold() for value in out.get("dependencies", [])}
        for key in ("read_tables", "write_tables", "affected_tables"):
            values = out[key]
            out[key] = [value for value in values if value.casefold() in confirmed]
            hidden |= len(out[key]) != len(values)
    if hidden:
        out["dependencies_omitted"] = True
    return out


def _describe_oracle(cfg, schema, name, object_type, policy):
    """Live Oracle catalog definitions; PL/SQL is never fed to the TSQL parser."""
    from ..infrastructure.backends import oracle_catalog as catalog
    from ..infrastructure.backends.oracle_resolution import OracleResolver, resolve_object
    from ..infrastructure.connection import open_connection
    from ..policy.scope import ObjectReference, oracle_object_allowed

    policy = policy or PolicyService(cfg)
    kind = object_type.upper()
    if kind not in {"VIEW", "MATERIALIZED VIEW", "PROCEDURE", "FUNCTION", "PACKAGE", "PACKAGE BODY", "SYNONYM"}:
        return {"status": "error", "error_message": "Unsupported Oracle object type"}
    try:
        with open_connection(cfg.for_source("local")) as conn:
            resolver = OracleResolver(cfg, conn, policy.current_policy())
            source_cfg = resolver.source_config(cfg.source_id)
            scope = (
                policy.current_policy().scope
                if cfg.source_id == "local"
                else policy.current_policy().scope.sources[cfg.source_id]
            )
            if not oracle_object_allowed(scope, cfg.database, schema, name):
                raise PermissionError("Oracle object is denied by source scope")
            suffix = resolver._suffix(cfg.source_id)
            exists = catalog._fetch(
                conn,
                f"SELECT object_type FROM all_objects{suffix} "
                "WHERE owner=:owner AND object_name=:name AND object_type=:type",
                {"owner": schema, "name": name, "type": kind},
            )
            if not exists:
                raise ValueError("Object is missing or inaccessible")
            if kind == "SYNONYM":
                resolved = resolve_object(cfg, conn, policy.current_policy(), schema, name, source=cfg.source_id)
                return {
                    "status": "ready",
                    "database_name": cfg.database,
                    "source": cfg.source_id,
                    "schema": schema,
                    "object_name": name,
                    "object_type": kind,
                    "definition": None,
                    "resolution_chain": resolved["chain"],
                    "resolved_target": resolved["target"],
                    "dependencies": resolved["chain"][1:],
                    "dependency_completeness": "resolved_path",
                    "read_tables": [],
                    "write_tables": [],
                    "affected_tables": [],
                    "io_completeness": "unknown",
                }
            # Do not load opaque source text merely to discard it. Only VIEW
            # definitions can pass the source/function/column proof below.
            opaque_definition = kind != "VIEW"
            definition = (
                None if opaque_definition else catalog.get_object_definition(source_cfg, schema, name, kind, conn=conn)
            )
            dependencies = catalog.get_object_dependencies(source_cfg, schema, name, object_type=kind, conn=conn)
            visible = []
            hidden = False
            for owner, dep_name, dep_type, link in dependencies:
                try:
                    dep_source = (
                        resolver.source_for_link(link, "SELECT") if link and cfg.source_id == "local" else cfg.source_id
                    )
                    if link and cfg.source_id != "local":
                        raise PermissionError("Chained remote dependency")
                    dep_scope = (
                        policy.current_policy().scope
                        if dep_source == "local"
                        else policy.current_policy().scope.sources[dep_source]
                    )
                    if not oracle_object_allowed(dep_scope, cfg.database, owner or "", dep_name):
                        raise PermissionError("Dependency scope")
                    if dep_type in {"TABLE", "VIEW", "MATERIALIZED VIEW", "SYNONYM"}:
                        resolver.resolve(
                            ObjectReference("", owner or "", dep_name, source=dep_source, dialect="oracle")
                        )
                    visible.append({"source": dep_source, "schema": owner, "name": dep_name, "object_type": dep_type})
                except Exception:  # noqa: BLE001 (fail closed without driver or SQL disclosure)
                    hidden = True
            reads = []
            io_complete = False
            if kind == "VIEW":
                try:
                    resolver.resolve(ObjectReference("", schema, name, source=cfg.source_id, dialect="oracle"))
                    reads = [
                        {"source": r.source, "schema": r.schema, "name": r.name}
                        for r in resolver.result.references
                        if resolver.result.object_types.get(r) in {"TABLE", "MATERIALIZED VIEW"}
                    ]
                    io_complete = True
                except Exception:  # noqa: BLE001 (fail closed without driver or SQL disclosure)
                    hidden = True
            # Remote authorization is closed-world even when local scope is
            # unrestricted. Static dependencies cannot prove an opaque module
            # has no dynamic/unconfigured remote references. Only VIEW has the
            # complete source/function/column proof above; materialized-view
            # definitions do not yet have an equivalent proof.
            withheld = hidden or opaque_definition
            return {
                "status": "restricted" if withheld else "ready",
                "database_name": cfg.database,
                "source": cfg.source_id,
                "schema": schema,
                "object_name": name,
                "object_type": kind,
                "definition": None if withheld else definition,
                "dependencies": visible,
                "dependency_completeness": "static_catalog_only",
                "dependencies_omitted": hidden,
                "read_tables": reads if not withheld else [],
                "write_tables": [],
                "affected_tables": [],
                "io_completeness": "complete_select_sources" if io_complete and not withheld else "unknown",
                "definition_withheld": withheld,
                **(
                    {
                        "definition_omission_reason": (
                            "Opaque Oracle definitions are withheld: dynamic or transitive sources cannot be proven "
                            "under closed-world source authorization"
                            if opaque_definition
                            else "Transitive or dynamic object scope cannot be proven"
                        )
                    }
                    if withheld
                    else {}
                ),
            }
    except Exception:  # noqa: BLE001 (fail closed without driver or SQL disclosure)
        return {"status": "error", "error_message": "Unable to load authorized Oracle object metadata"}
