"""Oracle ALL_* metadata. Values are bound; only catalog-confirmed DB links are interpolated."""

import hashlib
import json
import re
from collections import defaultdict
from contextlib import contextmanager
from decimal import Decimal

from ...config import Config
from .base import UnsupportedMetadataError

_LINK = re.compile(r"[A-Za-z][A-Za-z0-9_$#]*(?:\.[A-Za-z0-9_$#]+)*\Z")


@contextmanager
def _connection(cfg, conn=None):
    if conn is not None:
        yield conn
    else:
        from ..connection import open_connection

        with open_connection(cfg) as owned:
            yield owned


def _fetch(conn, sql, params=None):
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params or {})
        return [
            tuple(int(v) if isinstance(v, Decimal) and v == int(v) else v for v in row) for row in cursor.fetchall()
        ]
    finally:
        cursor.close()


def resolve_db_link(cfg: Config, conn) -> dict | None:
    """Resolve configured full/short link with private-before-public precedence.

    This verifies routing, not policy. USER_USERS is evaluated on the remote
    dictionary; USER / SYS_CONTEXT in a distributed SELECT can be evaluated locally.
    """
    if cfg.source_id == "local":
        if cfg.source_dblink:
            raise ValueError("Local source cannot route over a DB link")
        return None
    route = cfg.sources.get(cfg.source_id)
    if route is None or route.db_link != cfg.source_dblink or not _LINK.fullmatch(route.db_link):
        raise ValueError("Source does not match a configured DB link")
    rows = _fetch(
        conn,
        "SELECT owner, db_link, username, host FROM sys.all_db_links "
        "WHERE owner IN (USER, 'PUBLIC') AND (db_link = :link OR db_link LIKE :prefix ESCAPE '\\')",
        {"link": route.db_link, "prefix": route.db_link.replace("_", "\\_") + ".%"},
    )
    exact = [r for r in rows if r[1].upper() == route.db_link]
    rows = exact or rows
    names = {r[1] for r in rows}
    if len(names) != 1:
        raise ValueError("Configured DB link is missing or ambiguous in the session catalog")
    private = [r for r in rows if r[0] != "PUBLIC"]
    selected = (private or rows)[0]
    if not _LINK.fullmatch(selected[1]):
        raise ValueError("Catalog DB link has an unsupported identifier")
    try:
        principals = _fetch(conn, f"SELECT username FROM sys.user_users@{selected[1]}")
    except Exception:  # noqa: BLE001 (never expose remote driver authentication details)
        raise UnsupportedMetadataError("Cannot establish remote DB link principal") from None
    if len(principals) != 1 or len(principals[0]) != 1 or not isinstance(principals[0][0], str) or not principals[0][0]:
        raise UnsupportedMetadataError("Cannot establish a unique remote DB link principal")
    route = dict(zip(("owner", "db_link", "username", "host"), selected, strict=True))
    route["remote_user"] = principals[0][0]
    return route


def catalog_suffix(cfg, conn):
    link = resolve_db_link(cfg, conn)
    return "@" + link["db_link"] if link else ""


def current_schema(cfg: Config, conn=None) -> str:
    with _connection(cfg, conn) as connection:
        link = resolve_db_link(cfg, connection)
        if link:
            # Default remote object owner. Local SYS_CONTEXT does not identify the remote session.
            return link["remote_user"]
        return _fetch(connection, "SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM sys.dual")[0][0]


def native_type(data_type, precision, scale, char_length, char_used):
    """Preserve Oracle spellings and precision, including unconstrained NUMBER scale."""
    if data_type == "NUMBER":
        if precision is not None:
            return f"NUMBER({precision},{scale or 0})"
        if scale is not None:
            return f"NUMBER(*,{scale})"
    if data_type in {"VARCHAR2", "CHAR"} and char_length is not None:
        return f"{data_type}({char_length} {'CHAR' if char_used == 'C' else 'BYTE'})"
    if data_type in {"NVARCHAR2", "NCHAR"} and char_length is not None:
        return f"{data_type}({char_length})"
    if data_type == "FLOAT" and precision is not None:
        return f"FLOAT({precision})"
    return data_type


def _inventory_scope(cfg, suffix):
    from ...services.policy_service import PolicyService

    scope = PolicyService(cfg).effective_scope()
    if scope.allowed_schemas:
        from ...policy.intents.oracle_analyzer import oracle_identifier

        # PUBLIC is an alias namespace, not a request to scan every system
        # synonym. Public candidates below are bounded by their target owners.
        selected = [oracle_identifier(value) for value in scope.allowed_schemas]
        selected = [value for value in selected if value != "PUBLIC"]
        params = {f"schema_{i}": value for i, value in enumerate(selected)}
        owners = (
            ",".join(":" + key for key in params)
            if params
            else f"SELECT username FROM sys.all_users{suffix} WHERE oracle_maintained='N'"
        )
    else:
        params = {}
        owners = f"SELECT username FROM sys.all_users{suffix} WHERE oracle_maintained='N'"
    return owners, params


def _public_candidates(cfg, owners, params):
    """Bound PUBLIC aliases to app owners or explicitly enabled remote routes.

    Only a candidate filter, never authorization. Do not probe/warm every remote
    source while enumerating the local inventory; resolution proves each used
    route and target live later.
    """
    from ...services.policy_service import PolicyService

    policy = PolicyService(cfg).current_policy()
    clauses = [f"table_owner IN ({owners})"]
    bindings = dict(params)
    if cfg.source_id == "local" and policy.operations.select:
        for index, (name, route) in enumerate(cfg.sources.items()):
            scope = policy.scope.sources.get(name)
            if scope is None or not scope.operations.select:
                continue
            key = f"public_link_{index}"
            bindings[key] = route.db_link
            bindings[key + "_prefix"] = route.db_link.replace("_", "\\_") + ".%"
            clauses.append(f"(db_link=:{key} OR db_link LIKE :{key}_prefix ESCAPE '\\')")
    return "(" + " OR ".join(clauses) + ")", bindings


def fetch_snapshot(cfg: Config, conn=None):
    from ..cache.structural import StructuralSnapshot

    with _connection(cfg, conn) as connection:
        s = catalog_suffix(cfg, connection)
        owners, params = _inventory_scope(cfg, s)
        tables = _fetch(
            connection,
            f"SELECT owner, table_name FROM sys.all_tables{s} WHERE owner IN ({owners}) ORDER BY owner, table_name",
            params,
        )
        raw_columns = _fetch(
            connection,
            f"SELECT owner, table_name, column_name, data_type, data_length, nullable, data_default, "
            f"column_id, data_precision, data_scale, char_length, char_used FROM sys.all_tab_columns{s} "
            f"WHERE owner IN ({owners}) ORDER BY owner, table_name, column_id",
            params,
        )
        columns = []
        for (
            owner,
            table,
            name,
            typ,
            length,
            nullable,
            default,
            ordinal,
            precision,
            scale,
            chars,
            semantics,
        ) in raw_columns:
            columns.append(
                (
                    owner,
                    table,
                    name,
                    native_type(typ, precision, scale, chars, semantics),
                    length,
                    int(nullable == "Y"),
                    default,
                    ordinal,
                    precision,
                    scale,
                    scale if typ.startswith("TIMESTAMP") else None,
                    None,
                )
            )
        primary_keys = _fetch(
            connection,
            f"SELECT c.owner, c.table_name, k.column_name FROM sys.all_constraints{s} c "
            f"JOIN sys.all_cons_columns{s} k ON k.owner=c.owner AND k.constraint_name=c.constraint_name "
            "WHERE c.constraint_type='P' AND c.status='ENABLED' AND c.validated='VALIDATED' "
            f"AND c.deferrable='NOT DEFERRABLE' AND c.owner IN ({owners}) ORDER BY c.owner,c.table_name,k.position",
            params,
        )
        foreign_keys = _fetch(
            connection,
            f"SELECT c.owner,c.table_name,k.column_name,r.owner,r.table_name,rk.column_name, "
            "c.owner || '.' || c.constraint_name,c.constraint_name,k.position, "
            "CASE WHEN c.status='ENABLED' THEN 0 ELSE 1 END, "
            "CASE WHEN c.validated='VALIDATED' AND c.deferrable='NOT DEFERRABLE' "
            "AND r.status='ENABLED' AND r.validated='VALIDATED' AND r.deferrable='NOT DEFERRABLE' THEN 0 ELSE 1 END "
            f"FROM sys.all_constraints{s} c JOIN sys.all_cons_columns{s} k "
            "ON k.owner=c.owner AND k.constraint_name=c.constraint_name "
            f"JOIN sys.all_constraints{s} r ON r.owner=c.r_owner AND r.constraint_name=c.r_constraint_name "
            f"JOIN sys.all_cons_columns{s} rk ON rk.owner=r.owner AND rk.constraint_name=r.constraint_name "
            "AND rk.position=k.position WHERE c.constraint_type='R' "
            f"AND c.owner IN ({owners}) ORDER BY c.owner,c.table_name,c.constraint_name,k.position",
            params,
        )
        raw_indexes = _fetch(
            connection,
            "SELECT i.table_owner,i.table_name,i.index_name,i.uniqueness,i.status,i.index_type,"
            "k.column_name,k.column_position,c.constraint_type,c.status,c.validated,c.deferrable "
            f"FROM sys.all_indexes{s} i JOIN sys.all_ind_columns{s} k ON k.index_owner=i.owner AND k.index_name=i.index_name "
            f"LEFT JOIN sys.all_constraints{s} c ON c.index_owner=i.owner AND c.index_name=i.index_name "
            f"AND c.constraint_type IN ('P','U') WHERE i.table_owner IN ({owners}) ORDER BY i.table_owner,i.table_name,i.index_name,k.column_position",
            params,
        )
        grouped = defaultdict(list)
        for row in raw_indexes:
            grouped[row[:3]].append(row)
        indexes = []
        for key, rows in grouped.items():
            _, _, _, unique, status, kind, _, _, constraint, enabled, validated, deferrable = rows[0]
            functional = kind not in {"NORMAL", "NORMAL/REV"}
            trusted = (
                status == "VALID"
                and not functional
                and (
                    constraint is None
                    or (enabled == "ENABLED" and validated == "VALIDATED" and deferrable == "NOT DEFERRABLE")
                )
            )
            names = [r[6] for r in sorted(rows, key=lambda r: r[7])]
            indexes.append(
                (
                    *key,
                    int(unique == "UNIQUE" and trusted),
                    int(constraint == "P" and trusted),
                    json.dumps(names),
                    int(functional),
                    int(not trusted),
                )
            )
        public_where, public_params = _public_candidates(cfg, owners, params)
        objects = _fetch(
            connection,
            f"SELECT DISTINCT owner,object_name,object_type FROM sys.all_objects{s} "
            "WHERE object_type IN ('TABLE','VIEW','MATERIALIZED VIEW','PROCEDURE','FUNCTION','PACKAGE','PACKAGE BODY','SYNONYM') "
            f"AND owner IN ({owners}) UNION SELECT owner,synonym_name,'SYNONYM' FROM sys.all_synonyms{s} "
            f"WHERE owner='PUBLIC' AND {public_where} ORDER BY 1,2,3",
            public_params,
        )
        comments = _fetch(
            connection,
            f"SELECT owner,table_name,CAST(NULL AS VARCHAR2(128)),comments FROM sys.all_tab_comments{s} WHERE comments IS NOT NULL AND owner IN ({owners}) "
            f"UNION ALL SELECT owner,table_name,column_name,comments FROM sys.all_col_comments{s} WHERE comments IS NOT NULL AND owner IN ({owners})",
            params,
        )
        comments = [(o, t, c or "", text) for o, t, c, text in comments]
    return StructuralSnapshot(tables, columns, primary_keys, foreign_keys, indexes, objects, comments)


def get_object_definition(
    cfg: Config, schema: str, name: str, object_type: str | None = None, *, conn=None
) -> str | None:
    with _connection(cfg, conn) as conn:
        s = catalog_suffix(cfg, conn)
        params = {"owner": schema, "name": name}
        if object_type is None:
            rows = _fetch(
                conn, f"SELECT object_type FROM sys.all_objects{s} WHERE owner=:owner AND object_name=:name", params
            )
            types = {r[0] for r in rows}
            if len(types) > 1:
                raise ValueError("Object type is required for an ambiguous Oracle object")
            object_type = next(iter(types), None)
        if object_type in {"VIEW", "MATERIALIZED VIEW"}:
            # LONG must be selected directly, without local joins/functions over a distributed query.
            catalog, field, key = (
                ("all_views", "text", "view_name") if object_type == "VIEW" else ("all_mviews", "query", "mview_name")
            )
            rows = _fetch(conn, f"SELECT {field} FROM sys.{catalog}{s} WHERE owner=:owner AND {key}=:name", params)
            return rows[0][0] if rows else None
        if object_type in {"PROCEDURE", "FUNCTION", "PACKAGE", "PACKAGE BODY", "TYPE", "TYPE BODY", "TRIGGER"}:
            params["type"] = object_type
            rows = _fetch(
                conn,
                f"SELECT text FROM sys.all_source{s} WHERE owner=:owner AND name=:name AND type=:type ORDER BY line",
                params,
            )
            return "".join(r[0] or "" for r in rows) if rows else None
        return None


def get_object_dependencies(cfg: Config, schema: str, name: str, *, object_type=None, conn=None) -> list[tuple]:
    with _connection(cfg, conn) as conn:
        s = catalog_suffix(cfg, conn)
        return _fetch(
            conn,
            f"SELECT referenced_owner,referenced_name,referenced_type,referenced_link_name FROM sys.all_dependencies{s} "
            "WHERE owner=:owner AND name=:name "
            + ("AND type=:type " if object_type else "")
            + "ORDER BY referenced_owner,referenced_name,referenced_type",
            {"owner": schema, "name": name, **({"type": object_type} if object_type else {})},
        )


def _hash(value):
    # Catalog row order is not guaranteed without ORDER BY, including dependency/comment unions.
    def canonical(item):
        if isinstance(item, dict):
            return {k: canonical(v) for k, v in item.items()}
        if isinstance(item, list):
            return sorted((canonical(v) for v in item), key=lambda v: json.dumps(v, default=str))
        return item

    return hashlib.sha256(json.dumps(canonical(value), sort_keys=True, default=str).encode()).hexdigest()


def fetch_fingerprints(cfg: Config, conn=None):
    from ..cache.probe import SchemaFingerprints, filter_fingerprints

    with _connection(cfg, conn) as connection:
        snapshot = fetch_snapshot(cfg, connection)
        s = catalog_suffix(cfg, connection)
        owners, params = _inventory_scope(cfg, s)
        definitions = defaultdict(list)
        for owner, name, kind, line, text in _fetch(
            connection,
            f"SELECT owner,name,type,line,text FROM sys.all_source{s} WHERE owner IN ({owners}) ORDER BY owner,name,type,line",
            params,
        ):
            definitions[(owner, name, kind)].append((line, text))
        for owner, name, text in _fetch(
            connection, f"SELECT owner,view_name,text FROM sys.all_views{s} WHERE owner IN ({owners})", params
        ):
            definitions[(owner, name, "VIEW")].append(text)
        for owner, name, text in _fetch(
            connection, f"SELECT owner,mview_name,query FROM sys.all_mviews{s} WHERE owner IN ({owners})", params
        ):
            definitions[(owner, name, "MATERIALIZED VIEW")].append(text)
        public_where, public_params = _public_candidates(cfg, owners, params)
        synonyms = _fetch(
            connection,
            f"SELECT owner,synonym_name,table_owner,table_name,db_link FROM sys.all_synonyms{s} "
            f"WHERE owner IN ({owners}) OR (owner='PUBLIC' AND {public_where})",
            public_params,
        )
        for owner, name, target_owner, target_name, link in synonyms:
            definitions[(owner, name, "SYNONYM")].append((target_owner, target_name, link))
        raw_constraints = _fetch(
            connection,
            f"SELECT owner,table_name,constraint_name,constraint_type,status,validated,deferrable,deferred,rely,search_condition "
            f"FROM sys.all_constraints{s} WHERE owner IN ({owners}) ORDER BY owner,table_name,constraint_name",
            params,
        )
        dependencies = _fetch(
            connection,
            f"SELECT owner,name,type,referenced_owner,referenced_name,referenced_type,referenced_link_name FROM sys.all_dependencies{s} WHERE owner IN ({owners})",
            params,
        )
        raw_expressions = _fetch(
            connection,
            f"SELECT table_owner,table_name,index_name,column_position,column_expression FROM sys.all_ind_expressions{s} "
            f"WHERE table_owner IN ({owners})",
            params,
        )
        object_states = _fetch(
            connection,
            f"SELECT owner,object_name,object_type,status,last_ddl_time FROM sys.all_objects{s} WHERE owner IN ({owners})",
            params,
        )
        table_parts = {key: defaultdict(list) for key in snapshot.tables}
        object_parts = {key: {"definition": definitions[key]} for key in snapshot.objects}
        for row in object_states:
            if row[:3] in object_parts:
                object_parts[row[:3]]["state"] = row[3:]
        for row in raw_expressions:
            if row[:2] in table_parts:
                table_parts[row[:2]]["index_expressions"].append(row)
        for kind in ("columns", "primary_keys", "foreign_keys", "indexes", "comments"):
            for row in getattr(snapshot, kind):
                key = row[:2]
                if key in table_parts:
                    table_parts[key][kind].append(row)
                for obj_type in ("VIEW", "MATERIALIZED VIEW"):
                    if (*key, obj_type) in object_parts:
                        object_parts[(*key, obj_type)].setdefault(kind, []).append(row)
        for row in raw_constraints:
            if row[:2] in table_parts:
                table_parts[row[:2]]["constraints"].append(row)
        dependents = defaultdict(list)
        for row in dependencies:
            if row[:3] in object_parts:
                object_parts[row[:3]].setdefault("dependencies", []).append(row[3:])
            if row[3:5] in table_parts and not row[6]:
                dependents[row[3:5]].append(row[:2])
        fps = SchemaFingerprints(
            tables={key: _hash(value) for key, value in table_parts.items()},
            objects={key: _hash(value) for key, value in object_parts.items()},
            table_dependents=dict(dependents),
        )
        return filter_fingerprints(cfg, fps)
