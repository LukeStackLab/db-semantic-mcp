"""Default regex-based analyzer — wraps the existing ``analyze_sql``."""

from __future__ import annotations

from ..analyzer import SqlIntent, analyze_sql


class RegexIntentAnalyzer:
    name = "regex"

    def analyze(self, sql: str) -> SqlIntent:
        return analyze_sql(sql)
