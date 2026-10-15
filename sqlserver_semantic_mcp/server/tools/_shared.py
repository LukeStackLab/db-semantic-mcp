"""Shared helpers for tool argument normalization."""

from __future__ import annotations


def normalize_schema_filter(raw) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw] if raw else None
    if isinstance(raw, list):
        vals = [s for s in raw if isinstance(s, str) and s]
        return vals or None
    return None
