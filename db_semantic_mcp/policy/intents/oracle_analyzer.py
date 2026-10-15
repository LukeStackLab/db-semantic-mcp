"""Strict Oracle analysis. Normalized text is evidence only, never executable SQL.

Oracle q literals and DB link suffixes are lexed before SQLGlot: the stock
parser confuses dotted links with catalog qualification and quoted links with
aliases. Every substituted link must belong to exactly one physical table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.dialects.oracle import Oracle

from ...domain.enums import RiskLevel, SqlOperation
from ..analyzer import SqlIntent, _compute_risk
from ..scope import ObjectReference
from .ast_analyzer import _root_has_top


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    start: int
    end: int


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*")
_LINK_PART = re.compile(r"[A-Za-z0-9_$#]+")


def lex_oracle(sql: str) -> list[Token]:
    """Scan Oracle strings/comments/identifiers without permissive fallbacks."""
    tokens = []
    i = 0
    while i < len(sql):
        start = i
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = len(sql) if end < 0 else end
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                raise ValueError("Unterminated Oracle comment")
            if sql.startswith("/*+", i):
                raise ValueError("Oracle optimizer hints are not supported")
            i = end + 2
            continue
        q = re.match(r"(?i)(?:n?q)'", sql[i:])
        if q:
            pos = i + q.end()
            if pos >= len(sql) or sql[pos].isspace() or sql[pos] == "'":
                raise ValueError("Invalid Oracle alternative quote")
            closer = {"[": "]", "(": ")", "{": "}", "<": ">"}.get(sql[pos], sql[pos])
            end = sql.find(closer + "'", pos + 1)
            if end < 0:
                raise ValueError("Unterminated Oracle alternative quote")
            text = "'" + sql[pos + 1 : end].replace("'", "''") + "'"
            i = end + 2
            tokens.append(Token("literal", text, start, i))
            continue
        if ch in "'\"" or ch in "nN" and sql[i : i + 2].upper() == "N'":
            if ch in "nN":
                i += 1
                ch = "'"
            i += 1
            while i < len(sql):
                if sql[i] == ch:
                    if sql[i : i + 2] == ch * 2:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            else:
                raise ValueError("Unterminated Oracle quoted token")
            tokens.append(Token("identifier" if ch == '"' else "literal", sql[start:i], start, i))
            continue
        match = _WORD.match(sql, i)
        if match:
            i = match.end()
            tokens.append(Token("word", match.group(), start, i))
            continue
        number = re.match(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?", sql[i:])
        if number:
            i += number.end()
            tokens.append(Token("number", number.group(), start, i))
            continue
        if sql[i : i + 2] in {">=", "<=", "<>", "!=", "||", ":=", "**", "=>"}:
            tokens.append(Token("symbol", sql[i : i + 2], i, i + 2))
            i += 2
            continue
        if ch in "`[]\\":
            raise ValueError("Unsupported Oracle token")
        tokens.append(Token("symbol", ch, i, i + 1))
        i += 1
    return tokens


def mask_oracle_literals(sql: str) -> str:
    masked = ["\n" if c == "\n" else " " for c in sql]
    for token in lex_oracle(sql):
        if token.kind not in {"literal", "identifier"}:
            masked[token.start : token.end] = sql[token.start : token.end]
    return "".join(masked)


def oracle_identifier(value: str) -> str:
    tokens = lex_oracle(value)
    if len(tokens) != 1 or tokens[0].kind not in {"word", "identifier"}:
        raise ValueError("Invalid Oracle identifier")
    token = tokens[0]
    return token.text[1:-1].replace('""', '"') if token.kind == "identifier" else token.text.upper()


def oracle_scope_entry(value: str) -> ObjectReference:
    tokens = lex_oracle(value)
    if len(tokens) not in {1, 3} or len(tokens) == 3 and tokens[1].text != ".":
        raise ValueError("Oracle scope entries require name or schema.name")
    parts = [oracle_identifier(t.text) for t in tokens[::2]]
    return ObjectReference("", parts[-2] if len(parts) == 2 else "", parts[-1], dialect="oracle")


class _StrictOracle(Oracle):
    class Parser(Oracle.Parser):
        def _warn_unsupported(self):
            raise ValueError("Unsupported Oracle syntax")


# Canonical SQLGlot names and anonymous Oracle builtins. No package, network,
# XML evaluation, metadata, user-defined or remote functions are permitted.
_PURE_FUNCTIONS = set(
    """ABS ACOS ASIN ATAN ATAN2 CEIL CEILING COS COSH EXP FLOOR LN LOG
MOD POWER ROUND SIGN SIN SINH SQRT TAN TANH TRUNC TRUNCATE GREATEST LEAST
COALESCE NVL NVL2 NULLIF DECODE IF CASE COUNT SUM AVG MIN MAX STDDEV STDDEV_POP
STDDEV_SAMP VARIANCE VAR_POP VAR_SAMP LENGTH CHAR_LENGTH LENGTHB LOWER UPPER
INITCAP SUBSTR SUBSTRING INSTR REPLACE TRANSLATE TRIM LTRIM RTRIM LPAD RPAD
CONCAT CONCAT_WS ASCII CHR REVERSE TO_CHAR TO_DATE TO_NUMBER TO_TIMESTAMP
TO_TIMESTAMP_TZ CAST TRY_CAST EXTRACT DATE_TRUNC TIMESTAMP_TRUNC ADD_MONTHS
MONTHS_BETWEEN LAST_DAY NEXT_DAY CURRENT_DATE CURRENT_TIMESTAMP LOCALTIMESTAMP
SYSDATE SYSTIMESTAMP ROW_NUMBER RANK DENSE_RANK LAG LEAD FIRST_VALUE LAST_VALUE
NTH_VALUE NTILE CUME_DIST PERCENT_RANK RATIO_TO_REPORT LISTAGG GROUP_CONCAT
REGEXP_LIKE REGEXP_REPLACE REGEXP_SUBSTR REGEXP_INSTR REGEXP_COUNT
TIME_TO_STR STR_TO_DATE STR_TO_TIME TO_DECIMAL DATE_ADD DATE_DIFF""".split()
)
# Explicit structural allowlist rejects new/opaque parser extensions by default.
_STRUCTURES = set(
    """Select Union Intersect Except Subquery Insert Update Schema Values
Table Identifier Column Star Literal Null Boolean From Join Where Group Having Order
Ordered Limit LimitOptions Offset Fetch Distinct Alias TableAlias With CTE Paren Tuple EQ NEQ GT
GTE LT LTE And Or Not Add Sub Mul Div Mod Neg Pow DPipe Like ILike Escape Between In
Is Exists Any All Case If Window WindowSpec Filter WithinGroup DataType DataTypeParam
Interval Var Cast TryCast When NullSafeEQ NullSafeNEQ Placeholder Parameter
Anonymous""".split()
)
_CONTROL = set(
    """DELETE MERGE TRUNCATE CREATE ALTER DROP INTO EXEC EXECUTE BEGIN COMMIT
ROLLBACK SAVEPOINT GRANT REVOKE DECLARE CALL RETURN RETURNING LOCK MODEL MATCH_RECOGNIZE
PIVOT UNPIVOT CONNECT START SAMPLE VERSIONS FLASHBACK NEXTVAL CURRVAL""".split()
)


# Native Oracle spellings are checked separately from canonical AST names.
# SQLGlot knows other dialects' DATE_ADD / CONCAT_WS / SUBSTRING etc. Those
# spellings can be user functions in Oracle and must never inherit builtin trust.
_NATIVE_FUNCTIONS = set(
    """ABS ACOS ASIN ATAN ATAN2 CEIL COS COSH EXP FLOOR LN LOG
MOD POWER ROUND SIGN SIN SINH SQRT TAN TANH TRUNC GREATEST LEAST COALESCE NVL NVL2
NULLIF DECODE COUNT SUM AVG MIN MAX STDDEV STDDEV_POP STDDEV_SAMP VARIANCE VAR_POP
VAR_SAMP LENGTH LENGTHB LOWER UPPER INITCAP SUBSTR INSTR REPLACE TRANSLATE TRIM
LTRIM RTRIM LPAD RPAD CONCAT ASCII CHR TO_CHAR TO_DATE TO_NUMBER TO_TIMESTAMP
TO_TIMESTAMP_TZ CAST EXTRACT ADD_MONTHS MONTHS_BETWEEN LAST_DAY NEXT_DAY
CURRENT_DATE CURRENT_TIMESTAMP LOCALTIMESTAMP SYSDATE SYSTIMESTAMP ROW_NUMBER
RANK DENSE_RANK LAG LEAD FIRST_VALUE LAST_VALUE NTH_VALUE NTILE CUME_DIST
PERCENT_RANK RATIO_TO_REPORT LISTAGG REGEXP_LIKE REGEXP_REPLACE REGEXP_SUBSTR
REGEXP_INSTR REGEXP_COUNT""".split()
)
_CALL_GRAMMAR = set(
    """SELECT FROM JOIN ON WHERE HAVING AND OR NOT IN EXISTS AS
OVER VALUES WITH BY WHEN THEN ELSE UNION INTERSECT MINUS DISTINCT ALL
NUMBER VARCHAR2 NVARCHAR2 CHAR NCHAR RAW FLOAT TIMESTAMP INTERVAL""".split()
)


def _check_native_calls(normalized, root):
    allowed_positions = set()
    for cte in root.find_all(exp.CTE):
        alias = cte.args.get("alias")
        if alias and isinstance(alias.this, exp.Identifier):
            allowed_positions.add(alias.this.meta.get("start"))
    if isinstance(root, exp.Insert) and isinstance(root.this, exp.Schema):
        allowed_positions.add(root.this.this.this.meta.get("start"))
    tokens = lex_oracle(normalized)
    for current, following in zip(tokens, tokens[1:], strict=False):
        if following.text != "(" or current.kind not in {"word", "identifier"}:
            continue
        if current.start in allowed_positions:
            continue
        if current.kind == "identifier" or current.text.upper() not in _NATIVE_FUNCTIONS | _CALL_GRAMMAR:
            raise ValueError("Oracle callable name is not a native pure builtin")


def parse_oracle(sql: str) -> tuple[exp.Expression, list[ObjectReference]]:
    tokens = lex_oracle(sql)
    if not tokens:
        raise ValueError("No Oracle statement was found")
    # Single trailing terminator is harmless; multiple roots never authorized.
    if tokens[-1].text == ";":
        tokens.pop()
    if any(t.text == ";" for t in tokens):
        raise ValueError("Oracle accepts one SQL statement per call")
    remote = {}
    parts = [t.text for t in tokens]
    for index, token in enumerate(tokens):
        if token.text != "@":
            continue
        if index == 0 or tokens[index - 1].kind not in {"word", "identifier"}:
            raise ValueError("Unproven Oracle DB link source")
        # A link is one contiguous Oracle identifier, including its dots.
        pos = token.end
        m = re.match(r"[A-Za-z][A-Za-z0-9_$#]*(?:\.[A-Za-z0-9_$#]+)*", sql[pos:])
        if not m:
            raise ValueError("Invalid Oracle DB link")
        end = pos + m.end()
        consumed = [j for j in range(index + 1, len(tokens)) if tokens[j].start < end]
        if not consumed or tokens[consumed[-1]].end != end:
            raise ValueError("Invalid Oracle DB link boundary")
        marker = f"__DBMCP_LINK_{index}"
        if any(marker in t.text.upper() for t in tokens):
            raise ValueError("Reserved analysis identifier")
        remote[marker] = (oracle_identifier(tokens[index - 1].text), m.group().upper())
        parts[index - 1] = '"' + marker + '"'
        parts[index] = ""
        for j in consumed:
            parts[j] = ""
    normalized = " ".join(parts)
    root_list = _StrictOracle().parse(normalized, error_level=sqlglot.ErrorLevel.RAISE)
    if len(root_list) != 1 or root_list[0] is None:
        raise ValueError("Oracle accepts one SQL statement per call")
    root = root_list[0]
    _check_native_calls(normalized, root)
    if type(root) not in {exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery, exp.Insert, exp.Update}:
        raise ValueError("Oracle supports SELECT and simple INSERT VALUES / UPDATE WHERE only")
    for node in root.walk():
        if isinstance(node, exp.Identifier):
            if not node.args.get("quoted"):
                node.set("this", node.name.upper())
                if node.name in _CONTROL and not (node.name == "INTO" and isinstance(node.parent, exp.Insert)):
                    raise ValueError("Unquoted Oracle control keyword is not an identifier")
        if isinstance(node, exp.DataType) and node.this == exp.DataType.Type.USERDEFINED:
            raise ValueError("Oracle user-defined types cannot be authorized")
        if isinstance(node, exp.Column) and node.name.upper() in {"NEXTVAL", "CURRVAL"}:
            raise ValueError("Oracle sequence access is unsupported")
        if isinstance(node, exp.Dot):
            raise ValueError("Qualified or remote functions cannot be authorized")
        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).upper()
            if name not in _PURE_FUNCTIONS:
                raise ValueError("Oracle function is not in the pure builtin allowlist")
        elif type(node).__name__ not in _STRUCTURES:
            raise ValueError("Unsupported Oracle SQL construct")
    refs = []
    consumed_remote = set()

    def visit(node, ctes):
        local = set(ctes)
        with_clause = node.args.get("with_")
        if with_clause:
            declared = set()
            for cte in with_clause.expressions:
                if not cte.alias or cte.alias in declared:
                    raise ValueError("Ambiguous Oracle CTE")
                declared.add(cte.alias)
                # Recursive Oracle CTEs may refer to themselves.
                local.add(cte.alias)
                visit(cte.this, local)
        if isinstance(node, exp.Table):
            if not isinstance(node.this, exp.Identifier) or node.catalog:
                raise ValueError("Oracle object must be name or schema.name")
            if any(v for k, v in node.args.items() if k not in {"this", "db", "catalog", "alias"}):
                raise ValueError("Unsupported Oracle table source")
            name, link = remote.get(node.name, (node.name, ""))
            if node.name in remote:
                consumed_remote.add(node.name)
            if node.db or link or name not in local:
                reference = ObjectReference("", node.db, name, db_link=link, dialect="oracle")
                node.meta["oracle_reference"] = reference
                refs.append(reference)
                if link and not node.alias:
                    node.set("alias", exp.TableAlias(this=exp.Identifier(this=name, quoted=True)))
        for key, values in node.args.items():
            if key == "with_":
                continue
            for child in values if isinstance(values, list) else [values]:
                if isinstance(child, exp.Expression):
                    visit(child, local)

    visit(root, set())
    if consumed_remote != set(remote):
        raise ValueError("DB links may only address physical table sources")
    if isinstance(root, (exp.Insert, exp.Update)):
        simple = root.args.get("with_") is None and not any(isinstance(n, exp.Query) for n in root.walk())
        simple &= (
            isinstance(root.expression, exp.Values)
            if isinstance(root, exp.Insert)
            else (root.args.get("from_") is None and root.args.get("where") is not None)
        )
        if not simple or len(refs) != 1:
            raise ValueError("Oracle writes require simple INSERT VALUES or UPDATE WHERE")
    return root, list(dict.fromkeys(refs))


class OracleIntentAnalyzer:
    name = "oracle_ast"

    def analyze(self, sql: str) -> SqlIntent:
        try:
            root, refs = parse_oracle(sql)
            op = (
                SqlOperation.INSERT
                if isinstance(root, exp.Insert)
                else (SqlOperation.UPDATE if isinstance(root, exp.Update) else SqlOperation.SELECT)
            )
            return SqlIntent(
                primary_operation=op,
                has_where_clause=root.args.get("where") is not None,
                has_top_clause=_root_has_top(root),
                object_references=refs,
                affected_tables=[r.display_name for r in refs],
                required_operations=[op],
                is_simple_write=op != SqlOperation.SELECT,
                confidence=1.0,
                risk_level=_compute_risk(op, root.args.get("where") is not None, False),
                has_unqualified_tables=any(not r.schema for r in refs),
                contains_cte=root.find(exp.CTE) is not None,
            )
        except Exception:  # noqa: BLE001 (never echo parser SQL or driver details)
            return SqlIntent(
                primary_operation=SqlOperation.UNKNOWN,
                has_where_clause=False,
                has_top_clause=False,
                statement_count=0,
                risk_level=RiskLevel.HIGH,
                confidence=0.0,
                authorization_error="Oracle SQL could not be proven safe; use SELECT or simple INSERT VALUES / UPDATE WHERE",
            )
