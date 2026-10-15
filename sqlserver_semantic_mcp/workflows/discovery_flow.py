"""Deterministic local discovery over authorized metadata."""

from __future__ import annotations

import re

from ..config import Config, get_config
from ..infrastructure.cache.structural import read_schema_version
from ..services import metadata_service, semantic_service
from .contracts import ToolEnvelope

_STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "to",
    "and",
    "or",
    "for",
    "on",
    "in",
    "by",
    "with",
    "is",
    "are",
    "what",
    "which",
    "how",
    "show",
    "list",
    "me",
    "my",
    "our",
    "their",
    "please",
    "need",
    "want",
    "find",
}
_ALIASES = {
    "訂單": ["order", "orders"],
    "客戶": ["customer", "customers"],
    "使用者": ["user", "users"],
    "商品": ["product", "products"],
    "產品": ["product", "products"],
    "發票": ["invoice", "invoices"],
    "銷售": ["sales"],
    "金額": ["amount", "total"],
    "庫存": ["inventory", "stock"],
    "狀態": ["status", "state"],
}
_SEARCH_CACHE: dict[tuple, list[dict]] = {}


def _tokenize(goal: str) -> list[str]:
    parts = re.findall(r"[\w.]+", goal.casefold())
    return list(dict.fromkeys(token for token in parts if len(token) > 1 and token not in _STOPWORDS))


def _score(table: dict, tokens: list[str]) -> tuple[float, list[str]]:
    name = f"{table['schema_name']}.{table['table_name']}".casefold()
    bare = table["table_name"].casefold()
    columns = " ".join(table.get("column_names", [])).casefold()
    description = " ".join([table.get("description") or "", *table.get("column_descriptions", [])]).casefold()
    score, reasons = 0.0, []
    for token in dict.fromkeys(tokens):
        if token == name:
            score += 1.0
            reasons.append(f"qualified name: {token}")
        elif token == bare:
            score += 0.6
            reasons.append(f"table name: {token}")
        elif token in bare:
            score += 0.35
            reasons.append(f"table contains: {token}")
        elif token in name:
            score += 0.2
            reasons.append(f"schema/name contains: {token}")
        elif token in description:
            score += 0.3
            reasons.append(f"description: {token}")
        elif token in columns:
            score += 0.15
            reasons.append(f"column name: {token}")
    return min(score, 1.0), reasons[:5]


async def _documents(cfg: Config) -> list[dict]:
    version = await read_schema_version(cfg.cache_path, cfg.mssql_database)
    key = (
        cfg.cache_path,
        cfg.mssql_database,
        (version or {}).get("structural_hash"),
        (version or {}).get("comment_hash"),
    )
    if key not in _SEARCH_CACHE:
        # Cache the local snapshot, not an authorization decision. Scope is
        # checked on every call so policy changes cannot reuse a prior allow.
        documents = await metadata_service.search_documents(cfg.cache_path, cfg.mssql_database)
        for old in [item for item in _SEARCH_CACHE if item[:2] == key[:2]]:
            _SEARCH_CACHE.pop(old, None)
        _SEARCH_CACHE[key] = documents
    return _SEARCH_CACHE[key]


async def discover_relevant_tables(
    goal: str,
    *,
    schemas: list[str] | None = None,
    keyword: str | None = None,
    limit: int = 10,
    classify: bool = False,
    cfg: Config | None = None,
    policy=None,
) -> dict:
    cfg = cfg or get_config()
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("discovery limit must be between 1 and 100")
    if policy is None:
        from ..services.policy_service import PolicyService

        policy = PolicyService(cfg)
    tokens = _tokenize(goal)
    aliases = {**_ALIASES, **cfg.discovery_aliases}
    folded = goal.casefold()
    for alias, translations in aliases.items():
        if alias.casefold() in folded or alias.casefold() in tokens:
            tokens.extend(value.casefold() for value in translations if value)
            # Allow a business alias to match an existing Chinese description.
            tokens.append(alias.casefold())
    if keyword:
        tokens.extend(_tokenize(keyword))
    tokens = list(dict.fromkeys(tokens))
    tables = [
        doc
        for doc in await _documents(cfg)
        if (not schemas or doc["schema_name"] in schemas)
        and policy.object_allowed(doc["schema_name"], doc["table_name"], cfg.mssql_database)
    ]
    scored = []
    for table in tables:
        if keyword:
            searchable = " ".join(
                [
                    table["schema_name"],
                    table["table_name"],
                    table.get("description") or "",
                    *table.get("column_names", []),
                    *table.get("column_descriptions", []),
                ]
            ).casefold()
            if keyword.casefold() not in searchable:
                continue
        score, reasons = _score(table, tokens)
        if score <= 0:
            continue
        row = {"table": f"{table['schema_name']}.{table['table_name']}", "score": round(score, 3), "why": reasons}
        scored.append((row, table))
    scored.sort(key=lambda item: (item[0]["table"].casefold() not in tokens, -item[0]["score"], item[0]["table"]))
    top = scored[:limit]
    if classify:
        for row, table in top:
            cls = await semantic_service.classify_table(
                cfg.cache_path, cfg.mssql_database, table["schema_name"], table["table_name"], policy=policy
            )
            row["classification"] = cls.get("type")
            row["classification_confidence"] = cls.get("confidence")
    return ToolEnvelope(
        kind="discover_relevant_tables",
        next_action="describe_table" if top else "broaden_search",
        recommended_tool="describe_table" if top else "get_tables",
        data={
            "search_terms": tokens,
            "total_scanned": len(tables),
            "candidates": [row for row, _ in top],
            "truncated": len(scored) > limit,
            "score_kind": "heuristic",
        },
    ).to_dict()
