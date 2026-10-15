"""Analyzer contract for SQL intent detection."""

from __future__ import annotations

from typing import Protocol

from ..analyzer import SqlIntent


class IntentAnalyzer(Protocol):
    """Analyze a SQL string and return a :class:`SqlIntent`.

    Implementations must not raise for malformed SQL; they should return
    an ``UNKNOWN`` intent with ``is_sql_like=False`` instead, so the
    workflow router can send the request down the discovery path.
    """

    def analyze(self, sql: str) -> SqlIntent: ...
