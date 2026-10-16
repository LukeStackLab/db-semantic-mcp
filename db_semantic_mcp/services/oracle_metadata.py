"""Live Oracle alias proof and bounded, targeted table/view metadata."""

import aiosqlite

from ..infrastructure.backends.oracle_catalog import _fetch, catalog_suffix, native_type
from ..infrastructure.backends.oracle_resolution import resolve_object
from ..infrastructure.connection import open_connection
from ..infrastructure.workers import run_db_work


def quote(value):
    return '"' + value.replace('"', '""') + '"'


def qualified(schema, name):
    return quote(schema) + "." + quote(name)


def query_reference(cfg, schema, name):
    if schema == "PUBLIC" and cfg.source_id == "local":
        return quote(name)
    link = cfg._resolved_dblink or cfg.source_dblink
    return qualified(schema, name) + ("@" + link if link else "")


def _proof(cfg, policy, conn, schema, name):
    resolved = resolve_object(
        cfg.for_source("local", policy=policy.current_policy()),
        conn,
        policy.current_policy(),
        schema,
        name,
        source=cfg.source_id,
    )
    canonical = resolved["canonical_config"]
    target = resolved["target"]
    return resolved, {
        "chain": resolved["chain"],
        "target": target,
        "canonical_source": target["source"],
        "query_reference": query_reference(cfg, schema, name)
        if schema != "PUBLIC" or cfg.source_id == "local"
        else query_reference(canonical, target["schema"], target["name"]),
    }


def _resolve_candidates(policy, candidates, limit):
    cfg = policy._cfg
    result = []
    with open_connection(cfg.for_source("local", policy=policy.current_policy())) as conn:
        for row in candidates:
            try:
                _, proof = _proof(cfg, policy, conn, row["schema_name"], row["table_name"])
            except (PermissionError, ValueError):
                continue
            result.append({**row, **proof})
            if len(result) >= limit:
                break
    return result


async def list_tables(db_path, database, *, schemas=None, keyword=None, limit=None, policy):
    cfg = policy._cfg
    cap = min(limit or cfg.max_list_items, cfg.max_list_items)
    sql = "SELECT schema_name,object_name AS table_name,object_type FROM sc_objects WHERE database_name=? AND object_type IN ('TABLE','VIEW','MATERIALIZED VIEW','SYNONYM')"
    params = [database]
    if schemas:
        sql += " AND schema_name IN (" + ",".join("?" for _ in schemas) + ")"
        params.extend(schemas)
    if keyword:
        sql += " AND instr(lower(schema_name || '.' || object_name),lower(?)) > 0"
        params.append(keyword)
    sql += " ORDER BY schema_name,object_name,object_type"
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        candidates = []
        async for row in cur:
            if policy.object_allowed(row["schema_name"], row["table_name"], database):
                candidates.append(dict(row))
                if len(candidates) >= cfg.max_list_items:
                    break
    if not candidates:
        return []
    try:
        return await run_db_work(cfg, _resolve_candidates, policy, candidates, cap)
    except (PermissionError, ValueError):
        raise
    except Exception:  # noqa: BLE001 (redact driver/credential failures)
        raise ValueError("Oracle metadata source is unavailable; check catalog permissions") from None


def _describe(policy, schema, table, columns):
    cfg = policy._cfg
    with open_connection(cfg.for_source("local", policy=policy.current_policy())) as conn:
        resolved, proof = _proof(cfg, policy, conn, schema, table)
        target, canonical = resolved["target"], resolved["canonical_config"]
        s = catalog_suffix(canonical, conn)
        params = {"owner": target["schema"], "name": target["name"]}
        raw = _fetch(
            conn,
            f"SELECT column_name,data_type,data_length,nullable,data_default,column_id,data_precision,data_scale,char_length,char_used "
            f"FROM sys.all_tab_columns{s} WHERE owner=:owner AND table_name=:name ORDER BY column_id",
            params,
        )
        comments = dict(
            _fetch(
                conn,
                f"SELECT column_name,comments FROM sys.all_col_comments{s} WHERE owner=:owner AND table_name=:name",
                params,
            )
        )
        all_columns = []
        for name, typ, length, nullable, default, ordinal, precision, scale, chars, semantics in raw:
            all_columns.append(
                dict(
                    column_name=name,
                    data_type=native_type(typ, precision, scale, chars, semantics),
                    sql_type=native_type(typ, precision, scale, chars, semantics),
                    max_length=length,
                    is_nullable=nullable == "Y",
                    default_value=default,
                    ordinal_position=ordinal,
                    numeric_precision=precision,
                    numeric_scale=scale,
                    datetime_precision=scale if typ.startswith("TIMESTAMP") else None,
                    description=comments.get(name),
                )
            )
        if columns and set(columns) - {c["column_name"] for c in all_columns}:
            raise ValueError("Unknown column(s)")
        pk = [
            r[0]
            for r in _fetch(
                conn,
                f"SELECT k.column_name FROM sys.all_constraints{s} c JOIN sys.all_cons_columns{s} k ON k.owner=c.owner AND k.constraint_name=c.constraint_name "
                "WHERE c.owner=:owner AND c.table_name=:name AND c.constraint_type='P' AND c.status='ENABLED' AND c.validated='VALIDATED' AND c.deferrable='NOT DEFERRABLE' ORDER BY k.position",
                params,
            )
        ]
        description = _fetch(
            conn, f"SELECT comments FROM sys.all_tab_comments{s} WHERE owner=:owner AND table_name=:name", params
        )
        raw_fks = _fetch(
            conn,
            f"SELECT k.column_name,r.owner,r.table_name,rk.column_name,c.constraint_name,k.position,c.status,c.validated,c.deferrable,r.status,r.validated,r.deferrable "
            f"FROM sys.all_constraints{s} c JOIN sys.all_cons_columns{s} k ON k.owner=c.owner AND k.constraint_name=c.constraint_name "
            f"JOIN sys.all_constraints{s} r ON r.owner=c.r_owner AND r.constraint_name=c.r_constraint_name "
            f"JOIN sys.all_cons_columns{s} rk ON rk.owner=r.owner AND rk.constraint_name=r.constraint_name AND rk.position=k.position "
            "WHERE c.owner=:owner AND c.table_name=:name AND c.constraint_type='R' ORDER BY c.constraint_name,k.position",
            params,
        )
        from .policy_service import PolicyService

        target_policy = PolicyService(canonical)
        fks = [
            dict(
                schema_name=target["schema"],
                table_name=target["name"],
                column_name=r[0],
                ref_schema=r[1],
                ref_table=r[2],
                ref_column=r[3],
                constraint_id=target["schema"] + "." + r[4],
                constraint_name=r[4],
                constraint_column_id=r[5],
                is_disabled=int(r[6] != "ENABLED"),
                is_not_trusted=int(r[7:] != ("VALIDATED", "NOT DEFERRABLE", "ENABLED", "VALIDATED", "NOT DEFERRABLE")),
            )
            for r in raw_fks
            if target_policy.object_allowed(r[1], r[2])
        ]
        raw_indexes = _fetch(
            conn,
            f"SELECT i.index_name,i.uniqueness,i.status,i.index_type,k.column_name,k.column_position,c.constraint_type,c.status,c.validated,c.deferrable "
            f"FROM sys.all_indexes{s} i JOIN sys.all_ind_columns{s} k ON k.index_owner=i.owner AND k.index_name=i.index_name "
            f"LEFT JOIN sys.all_constraints{s} c ON c.index_owner=i.owner AND c.index_name=i.index_name AND c.constraint_type IN ('P','U') "
            "WHERE i.table_owner=:owner AND i.table_name=:name ORDER BY i.index_name,k.column_position",
            params,
        )
        indexes = {}
        for name, unique, status, kind, col, _ordinal, constraint, enabled, validated, deferrable in raw_indexes:
            trusted = (
                status == "VALID"
                and kind in {"NORMAL", "NORMAL/REV"}
                and (
                    constraint is None or (enabled, validated, deferrable) == ("ENABLED", "VALIDATED", "NOT DEFERRABLE")
                )
            )
            idx = indexes.setdefault(
                name,
                dict(
                    index_name=name,
                    is_unique=unique == "UNIQUE" and trusted,
                    is_primary_key=constraint == "P" and trusted,
                    columns=[],
                    has_filter=int(kind not in {"NORMAL", "NORMAL/REV"}),
                    is_disabled=int(not trusted),
                ),
            )
            idx["columns"].append(col)
    from .relationship_service import group_foreign_keys

    joins = group_foreign_keys(fks)
    if columns is not None:
        joins = [fk for fk in joins if any(pair["from_column"] in columns for pair in fk["column_pairs"])]
    required = {pair["from_column"] for fk in joins for pair in fk["column_pairs"]}
    return dict(
        schema_name=schema,
        table_name=table,
        columns=[c for c in all_columns if columns is None or c["column_name"] in columns],
        column_count=len(all_columns),
        columns_omitted=columns is not None and len(columns) < len(all_columns),
        primary_key=pk,
        foreign_keys=fks,
        indexes=list(indexes.values()),
        description=description[0][0] if description else None,
        classification={"type": "unknown"},
        join_constraints=joins,
        required_join_columns=[column for column in all_columns if column["column_name"] in required],
        cache={"hit": False, "live_verified": True},
        **proof,
    )


async def describe_table(schema, table, *, columns=None, policy):
    try:
        return await run_db_work(policy._cfg, _describe, policy, schema, table, columns)
    except (PermissionError, ValueError):
        raise
    except Exception:  # noqa: BLE001 (redact driver/credential failures)
        raise ValueError("Oracle table metadata is unavailable; check source and catalog permissions") from None


async def search_documents(db_path, database, *, schemas=None, policy):
    rows = await list_tables(db_path, database, schemas=schemas, policy=policy)
    async with aiosqlite.connect(db_path) as db:
        for row in rows:
            cur = await db.execute(
                "SELECT column_name FROM sc_columns WHERE database_name=? AND schema_name=? AND table_name=? ORDER BY ordinal_position",
                (database, row["schema_name"], row["table_name"]),
            )
            row["column_names"] = [r[0] for r in await cur.fetchall()]
            row.update(description="", column_descriptions=[])
    return rows
