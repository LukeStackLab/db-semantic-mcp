"""Whole-batch keyword guard for select-only policy profiles.

``analyze_sql`` classifies a batch by its first keyword and splits statements
on ``;`` only. T-SQL needs no ``;``, so ``SELECT 1 DELETE dbo.X``,
``WITH c AS (...) UPDATE ...`` and ``SELECT * INTO dbo.T ...`` all come back
as SELECT. When the active profile allows nothing but SELECT, this guard scans
the whole batch for write / transaction-control keywords instead.
"""

import re

from .models import PolicyProfile

_WRITE_KEYWORDS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "MERGE",
    "TRUNCATE",
    "CREATE",
    "ALTER",
    "DROP",
    "INTO",
    "EXEC",
    "EXECUTE",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "SAVE",
    "GRANT",
    "REVOKE",
    "DENY",
    "BACKUP",
    "RESTORE",
    "DBCC",
    "KILL",
    "SHUTDOWN",
    "CHECKPOINT",
    "RECONFIGURE",
    "BULK",
    "OPENROWSET",
    "OPENDATASOURCE",
    "OPENQUERY",
    "WRITETEXT",
    "UPDATETEXT",
    "RECEIVE",
    "CONVERSATION",
)

_WRITE_PAT = re.compile(
    r"\b(?:" + "|".join(_WRITE_KEYWORDS) + r")\b"
    r"|\bNEXT\s+VALUE\s+FOR\b"
    r"|\b(?:ENABLE|DISABLE)\s+TRIGGER\b",
    re.IGNORECASE,
)


def mask_literals(sql: str) -> str:
    """Blank out string literals, quoted/bracketed identifiers and comments.

    Single left-to-right pass, so a ``'--'`` literal cannot hide the rest of
    the line from the scan. Block comments end at the first ``*/``: SQL Server
    nests them, so this masks at most what the server treats as comment and
    errs towards rejecting. Unterminated tokens mask to the end; the server
    rejects such a batch at parse time anyway.
    """
    out: list[str] = []

    def blank(start: int, end: int) -> str:
        # Length-preserving, so offsets in the masked text still point at the
        # caller's original line and column.
        return "".join("\n" if c == "\n" else " " for c in sql[start:end])

    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            stop = n if end == -1 else end
            out.append(blank(i, stop))
            i = stop
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            stop = n if end == -1 else end + 2
            out.append(blank(i, stop))
            i = stop
        elif ch in "'\"[":
            close = "]" if ch == "[" else ch
            j = i + 1
            while j < n:
                if sql[j] == close:
                    if j + 1 < n and sql[j + 1] == close:  # doubled = escaped
                        j += 2
                        continue
                    break
                j += 1
            stop = min(j + 1, n)
            out.append(blank(i, stop))
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def find_write_keywords(sql: str) -> list[str]:
    """Distinct write/control keywords in ``sql`` outside literals/comments."""
    return keywords_in_masked(mask_literals(sql))


def keywords_in_masked(masked: str) -> list[str]:
    """Distinct write/control keywords in already-masked text."""
    seen: list[str] = []
    for m in _WRITE_PAT.finditer(masked):
        word = re.sub(r"\s+", " ", m.group(0)).upper()
        if word not in seen:
            seen.append(word)
    return seen


def find_write_keyword_hits(sql: str) -> list[dict]:
    """Every write/control keyword outside literals/comments, with its position.

    Lets a denial point at the exact token instead of only naming the keyword.
    Positions are 1-based line and column in the caller's SQL.
    """
    return keyword_hits_in_masked(mask_literals(sql))


def keyword_hits_in_masked(masked: str) -> list[dict]:
    """Positioned write/control keyword hits in already-masked text."""
    hits = []
    for m in _WRITE_PAT.finditer(masked):
        line = masked.count("\n", 0, m.start()) + 1
        column = m.start() - (masked.rfind("\n", 0, m.start()) + 1) + 1
        hits.append(
            {
                "keyword": re.sub(r"\s+", " ", m.group(0)).upper(),
                "line": line,
                "column": column,
            }
        )
    return hits


def is_select_only(policy: PolicyProfile) -> bool:
    ops = policy.operations.model_dump()
    return bool(ops.get("select")) and not any(v for k, v in ops.items() if k != "select")
