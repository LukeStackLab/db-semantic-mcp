"""Shared, conservative object scope checks for SQL and metadata.

Allow entries retain case because the server may use a case-sensitive collation.
Deny entries use casefold: denying extra case variants cannot broaden access.
Arguments contain decoded names; policy entries use TSQL identifier syntax.
"""
from dataclasses import dataclass
from functools import lru_cache

import sqlglot
from sqlglot import exp

from .models import PolicyScope


@dataclass(frozen=True)
class ObjectReference:
    database: str
    schema: str
    name: str

    @property
    def display_name(self) -> str:
        components = [self.database, self.schema, self.name]
        return ".".join("[" + part.replace("]", "]]") + "]" for part in components if part)


def table_reference(table: exp.Table) -> ObjectReference:
    """Decode one local object; do not infer a remote server or default schema."""
    if not isinstance(table.this, exp.Identifier) or len(table.parts) > 3:
        raise ValueError("Remote, dynamic and function sources are not supported")
    if table.this.args.get("temporary") or table.this.args.get("global"):
        raise ValueError("Temporary object scope cannot be verified")
    if not table.name or table.name.startswith(("#", "@")):
        raise ValueError("Object scope cannot be verified")
    if table.catalog and not table.db:
        raise ValueError("Database-qualified objects require an explicit schema")
    return ObjectReference(table.catalog, table.db, table.name)


@lru_cache(maxsize=1024)
def parse_scope_entry(value: str) -> ObjectReference:
    try:
        table = sqlglot.parse_one(value, read="tsql", into=exp.Table,
                                  error_level=sqlglot.ErrorLevel.RAISE)
        if any(value for key, value in table.args.items() if key not in {"this", "db", "catalog"}):
            raise ValueError("Invalid scope identifier")
        return table_reference(table)
    except Exception:
        raise ValueError("Invalid policy scope identifier") from None


def split_object_name(value: str) -> tuple[str, ...]:
    """Split strict one/two/three-part identifiers, preserving quoted dots."""
    ref = parse_scope_entry(value)
    if ref.database:
        return ref.database, ref.schema, ref.name
    if ref.schema:
        return ref.schema, ref.name
    return (ref.name,)


def database_allowed(scope: PolicyScope, database: str) -> bool:
    return not scope.allowed_databases or database in scope.allowed_databases


def object_allowed(scope: PolicyScope, database: str, schema: str, name: str) -> bool:
    if not name or not database_allowed(scope, database):
        return False
    if scope.allowed_schemas and schema not in scope.allowed_schemas:
        return False
    try:
        denied = [parse_scope_entry(entry) for entry in scope.denied_tables]
        allowed = [parse_scope_entry(entry) for entry in scope.allowed_tables]
    except ValueError:
        # Ignoring an invalid deny entry would silently remove a restriction.
        return False

    for ref in denied:
        if ref.name.casefold() != name.casefold():
            continue
        if ref.schema and schema and ref.schema.casefold() != schema.casefold():
            continue
        if ref.database and database and ref.database.casefold() != database.casefold():
            continue
        # An omitted component might resolve to the denied object at runtime.
        return False

    if not allowed:
        return True
    return any(
        ref.name == name
        and (not ref.schema or ref.schema == schema)
        and (not ref.database or ref.database == database)
        for ref in allowed
    )
