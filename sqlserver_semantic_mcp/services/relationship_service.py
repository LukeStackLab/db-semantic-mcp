"""Constraint-aware FK relationships and bounded shortest join alternatives."""

import json
from collections import deque
from copy import deepcopy

import aiosqlite

from ..infrastructure.cache.structural import read_schema_version

_GRAPH_CACHE: dict[tuple[str, str, str], dict[tuple[str, str], list[dict]]] = {}
_MAX_HOPS = 20
_MAX_CANDIDATES = 20


def _json_index_columns(value) -> list[str] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, list) and all(isinstance(c, str) for c in parsed):
        return parsed
    return None


def parse_index_columns(value) -> list[str]:
    """Read escaped JSON names while retaining old cache display support."""
    parsed = _json_index_columns(value)
    if parsed is not None:
        return parsed
    return value.split(",") if isinstance(value, str) and value else []


def group_foreign_keys(rows: list[dict]) -> list[dict]:
    """Group catalog column rows by constraint, without guessing missing keys.

    Inputs use SQLite column names. Output endpoints retain the cache names;
    each ordered column_pairs item uses from_column/to_column/ordinal.
    Rows without constraint id/name get a positional identity and
    metadata_complete=False.
    """
    groups: dict[tuple, list[dict]] = {}
    ordered = sorted(rows, key=lambda row: json.dumps(dict(row), sort_keys=True, default=str))
    for position, row in enumerate(ordered):
        row = dict(row)
        endpoints = (row["schema_name"], row["table_name"], row["ref_schema"], row["ref_table"])
        if row.get("constraint_id") is not None:
            identity = ("id", row["constraint_id"])
        elif row.get("constraint_name"):
            identity = ("name", row["constraint_name"])
        else:
            identity = ("legacy", position)
        groups.setdefault((*endpoints, *identity), []).append(row)

    result = []
    for members in groups.values():
        members.sort(
            key=lambda r: (
                r.get("constraint_column_id") if isinstance(r.get("constraint_column_id"), int) else 0,
                r["column_name"],
                r["ref_column"],
            )
        )
        first = members[0]
        ordinals = [r.get("constraint_column_id") for r in members]
        complete = (
            first.get("constraint_id") is not None
            and bool(first.get("constraint_name"))
            and all(type(value) is int for value in ordinals)
            and ordinals == list(range(1, len(members) + 1))
            and all(
                all(r.get(key) == first.get(key) for key in ("constraint_name", "is_disabled", "is_not_trusted"))
                for r in members
            )
        )
        group = {key: first[key] for key in ("schema_name", "table_name", "ref_schema", "ref_table")}
        group.update(
            {
                "constraint_id": first.get("constraint_id"),
                "constraint_name": first.get("constraint_name"),
                "metadata_complete": bool(complete),
                "is_disabled": first.get("is_disabled"),
                "is_not_trusted": first.get("is_not_trusted"),
                "column_pairs": [
                    {
                        "from_column": r["column_name"],
                        "to_column": r["ref_column"],
                        "ordinal": r.get("constraint_column_id"),
                    }
                    for r in members
                ],
            }
        )
        if len(members) == 1:
            group["column_name"] = first["column_name"]
            group["ref_column"] = first["ref_column"]
        result.append(group)
    return result


def _reverse_edge(edge: dict) -> dict:
    reverse = {
        **edge,
        "from_schema": edge["to_schema"],
        "from_table": edge["to_table"],
        "to_schema": edge["from_schema"],
        "to_table": edge["from_table"],
        "direction": "inbound" if edge["direction"] == "outbound" else "outbound",
        "type": {"many_to_one": "one_to_many", "one_to_many": "many_to_one"}.get(edge["type"], edge["type"]),
        "column_pairs": [
            {"from_column": pair["to_column"], "to_column": pair["from_column"], "ordinal": pair["ordinal"]}
            for pair in edge["column_pairs"]
        ],
    }
    cardinality = edge["cardinality"]
    reverse["cardinality"] = {
        "from_min": cardinality["to_min"],
        "from_max": cardinality["to_max"],
        "to_min": cardinality["from_min"],
        "to_max": cardinality["from_max"],
    }
    if len(reverse["column_pairs"]) == 1:
        reverse["from_column"] = reverse["column_pairs"][0]["from_column"]
        reverse["to_column"] = reverse["column_pairs"][0]["to_column"]
    return reverse


async def _read_fk_sources(db_path: str, database: str):
    """Read FK rows, column nullability and unique indexes in one pass."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM sc_foreign_keys WHERE database_name=?", (database,))
        groups = group_foreign_keys([dict(r) for r in await cur.fetchall()])
        cur = await db.execute(
            "SELECT schema_name, table_name, column_name, is_nullable FROM sc_columns WHERE database_name=?",
            (database,),
        )
        nullable = {
            (r["schema_name"], r["table_name"], r["column_name"]): bool(r["is_nullable"]) for r in await cur.fetchall()
        }
        cur = await db.execute("SELECT * FROM sc_indexes WHERE database_name=?", (database,))
        unique_keys: dict[tuple[str, str], list[set[str]]] = {}
        for row in await cur.fetchall():
            # Old comma strings and unknown index state cannot prove uniqueness.
            names = _json_index_columns(row["columns"])
            if row["is_unique"] and names and row["has_filter"] == 0 and row["is_disabled"] == 0:
                unique_keys.setdefault((row["schema_name"], row["table_name"]), []).append(set(names))
    return groups, nullable, unique_keys


def _fk_nullability(src: tuple[str, str], columns: set[str], nullable: dict, complete: bool) -> bool | None:
    nulls = [nullable.get((*src, name)) for name in columns]
    if any(value is True for value in nulls):
        return True
    if nulls and all(value is False for value in nulls):
        return False if complete else None
    return None


def _fk_cardinality(
    unique: bool, target_unique: bool, complete: bool, enforced: bool, is_nullable: bool | None
) -> dict:
    return {
        "from_min": 0,
        "from_max": (1 if unique else "many") if complete else None,
        "to_min": 1 if enforced and is_nullable is False else 0,
        "to_max": 1 if complete and target_unique else None,
    }


def _fk_edge(group: dict, nullable: dict, unique_keys: dict) -> dict:
    src = (group["schema_name"], group["table_name"])
    dst = (group["ref_schema"], group["ref_table"])
    columns = {pair["from_column"] for pair in group["column_pairs"]}
    unique = any(key <= columns for key in unique_keys.get(src, []))
    referenced_columns = {pair["to_column"] for pair in group["column_pairs"]}
    target_unique = any(key <= referenced_columns for key in unique_keys.get(dst, []))
    complete = group["metadata_complete"]
    is_nullable = _fk_nullability(src, columns, nullable, complete)
    enforced = complete and target_unique and group["is_disabled"] == 0 and group["is_not_trusted"] == 0
    edge = {
        "from_schema": src[0],
        "from_table": src[1],
        "to_schema": dst[0],
        "to_table": dst[1],
        "constraint_id": group["constraint_id"],
        "constraint_name": group["constraint_name"],
        "metadata_complete": group["metadata_complete"],
        "column_pairs": group["column_pairs"],
        "direction": "outbound",
        "type": ("one_to_one" if unique else "many_to_one") if complete and target_unique else "unknown",
        "nullable": is_nullable,
        "is_disabled": group["is_disabled"],
        "is_not_trusted": group["is_not_trusted"],
        "cardinality": _fk_cardinality(unique, target_unique, complete, enforced, is_nullable),
    }
    if len(edge["column_pairs"]) == 1:
        edge["from_column"] = edge["column_pairs"][0]["from_column"]
        edge["to_column"] = edge["column_pairs"][0]["to_column"]
    return edge


async def _load_fk_graph(db_path: str, database: str) -> dict[tuple[str, str], list[dict]]:
    ver = await read_schema_version(db_path, database)
    structural_hash = ver["structural_hash"] if ver else ""
    cache_key = (db_path, database, structural_hash)
    cached = _GRAPH_CACHE.get(cache_key)
    if cached is not None:
        return cached

    groups, nullable, unique_keys = await _read_fk_sources(db_path, database)

    graph: dict[tuple[str, str], list[dict]] = {}
    for group in groups:
        edge = _fk_edge(group, nullable, unique_keys)
        src = (edge["from_schema"], edge["from_table"])
        dst = (edge["to_schema"], edge["to_table"])
        graph.setdefault(src, []).append(edge)
        graph.setdefault(dst, []).append(_reverse_edge(edge))

    for key in list(_GRAPH_CACHE):
        if key[:2] == (db_path, database) and key != cache_key:
            _GRAPH_CACHE.pop(key, None)
    _GRAPH_CACHE[cache_key] = graph
    return graph


def _allowed(policy, database: str, node: tuple[str, str]) -> bool:
    return policy is None or policy.object_allowed(node[0], node[1], database)


def _scope_graph(graph: dict, policy, database: str) -> dict:
    if policy is None:
        return graph
    return {
        node: [edge for edge in edges if _allowed(policy, database, (edge["to_schema"], edge["to_table"]))]
        for node, edges in graph.items()
        if _allowed(policy, database, node)
    }


async def get_table_relationships(
    db_path: str,
    database: str,
    schema: str,
    table: str,
    *,
    policy=None,
) -> list[dict]:
    graph = _scope_graph(await _load_fk_graph(db_path, database), policy, database)
    results = []
    for edge in graph.get((schema, table), []):
        if edge["direction"] == "outbound":
            results.append(deepcopy(edge))
        else:
            # Keep the existing list convention: FK owner remains from_table;
            # direction/type describe the requested table.
            incoming = _reverse_edge(edge)
            incoming["direction"] = "inbound"
            incoming["type"] = edge["type"]
            results.append(deepcopy(incoming))
    return results


def _bounded_integer(value: int, name: str, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")


def _bfs_predecessors(graph: dict, start: tuple[str, str], target: tuple[str, str], max_hops: int) -> tuple[dict, dict]:
    distance = {start: 0}
    predecessors: dict[tuple[str, str], list[tuple[tuple[str, str], dict]]] = {}
    queue = deque([start])
    target_depth = 0 if start == target else None
    while queue:
        node = queue.popleft()
        depth = distance[node]
        if depth >= max_hops or (target_depth is not None and depth >= target_depth):
            continue
        for edge in graph.get(node, []):
            nxt = (edge["to_schema"], edge["to_table"])
            if nxt not in distance:
                distance[nxt] = depth + 1
                queue.append(nxt)
            if distance[nxt] == depth + 1:
                predecessors.setdefault(nxt, []).append((node, edge))
            if nxt == target:
                target_depth = depth + 1
    return distance, predecessors


def _reconstruct_candidate_paths(
    predecessors: dict, start: tuple[str, str], target: tuple[str, str], max_candidates: int
) -> list[list[dict]]:
    paths = []
    stack = [(target, [])]
    while stack and len(paths) <= max_candidates:
        node, reverse_path = stack.pop()
        if node == start:
            paths.append(list(reversed(reverse_path)))
            continue
        for previous, edge in reversed(predecessors.get(node, [])):
            stack.append((previous, reverse_path + [edge]))
    return paths


async def find_join_candidates(
    db_path: str,
    database: str,
    from_schema: str,
    from_table: str,
    to_schema: str,
    to_table: str,
    max_hops: int = 5,
    max_candidates: int = 5,
    policy=None,
) -> dict:
    """Return bounded shortest-path alternatives without selecting a FK role."""
    _bounded_integer(max_hops, "max_hops", _MAX_HOPS)
    _bounded_integer(max_candidates, "max_candidates", _MAX_CANDIDATES)
    start, target = (from_schema, from_table), (to_schema, to_table)
    not_found = {
        "found": False,
        "ambiguous": False,
        "path": [],
        "candidates": [],
        "truncated": False,
        "metadata_complete": False,
        "next_action": "broaden_or_pick_different_start",
    }
    if not _allowed(policy, database, start) or not _allowed(policy, database, target):
        return not_found
    graph = _scope_graph(await _load_fk_graph(db_path, database), policy, database)
    distance, predecessors = _bfs_predecessors(graph, start, target, max_hops)
    if target not in distance:
        return not_found

    paths = _reconstruct_candidate_paths(predecessors, start, target, max_candidates)
    truncated = len(paths) > max_candidates
    ambiguous = len(paths) > 1
    candidates = [
        {
            "path": deepcopy(path),
            "hops": len(path),
            "metadata_complete": all(edge["metadata_complete"] for edge in path),
        }
        for path in paths[:max_candidates]
    ]
    complete = bool(candidates) and all(c["metadata_complete"] for c in candidates)
    return {
        "found": True,
        "ambiguous": ambiguous,
        "path": candidates[0]["path"] if not ambiguous else [],
        "candidates": candidates,
        "truncated": truncated,
        "metadata_complete": complete,
        "next_action": (
            "select_join_candidate" if ambiguous else "review_join" if complete else "refresh_relationship_metadata"
        ),
    }


async def find_join_path(
    db_path: str,
    database: str,
    from_schema: str,
    from_table: str,
    to_schema: str,
    to_table: str,
    max_hops: int = 5,
    *,
    policy=None,
) -> list[dict] | None:
    """Compatibility wrapper: return a path only when the role is unambiguous."""
    result = await find_join_candidates(
        db_path,
        database,
        from_schema,
        from_table,
        to_schema,
        to_table,
        max_hops=max_hops,
        policy=policy,
    )
    return result["path"] if result["found"] and not result["ambiguous"] else None


async def get_dependency_chain(
    db_path: str,
    database: str,
    schema: str,
    table: str,
    max_depth: int = 10,
    *,
    schemas: list[str] | None = None,
    policy=None,
) -> list[dict]:
    _bounded_integer(max_depth, "max_depth", _MAX_HOPS)
    graph = _scope_graph(await _load_fk_graph(db_path, database), policy, database)
    start = (schema, table)
    if not _allowed(policy, database, start):
        return []
    visited = {start: 0}
    queue = deque([start])
    allowed = set(schemas) if schemas else None
    while queue:
        node = queue.popleft()
        depth = visited[node]
        if depth >= max_depth:
            continue
        for edge in graph.get(node, []):
            nxt = (edge["to_schema"], edge["to_table"])
            if nxt not in visited and (allowed is None or nxt[0] in allowed):
                visited[nxt] = depth + 1
                queue.append(nxt)
    return [{"schema_name": s, "table_name": t, "depth": d} for (s, t), d in visited.items() if (s, t) != start]
