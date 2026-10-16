"""Live proof for the supported Oracle scalar-table read subset.

Metadata is read on the execution connection before caller parse/execute. Reject
an entire target when implicit evaluation cannot be proven safe, including hidden
columns and access paths. This deliberately does not parse PL/SQL expressions.
"""

import re

from . import oracle_catalog as catalog

_SCALAR = re.compile(
    r"(?:NUMBER|FLOAT|BINARY_FLOAT|BINARY_DOUBLE|VARCHAR2|NVARCHAR2|CHAR|NCHAR|"
    r"DATE|RAW|LONG|LONG RAW|ROWID|UROWID|CLOB|NCLOB|BLOB|"
    r"TIMESTAMP(?:\(\d+\))?(?: WITH (?:LOCAL )?TIME ZONE)?|"
    r"INTERVAL YEAR(?:\(\d+\))? TO MONTH|INTERVAL DAY(?:\(\d+\))? TO SECOND(?:\(\d+\))?)\Z"
)


def verified_columns(conn, owner, name, suffix, object_type):
    """Return visible columns only after all evidence is present and safe.

    ALL_TAB_COLS includes system-hidden virtual columns (function indexes).
    ALL_* visibility is tied to the same principal and resolved remote route.
    Catalog failure propagates to the existing controlled authorization denial.
    """
    params = {"owner": owner, "name": name}
    if object_type == "MATERIALIZED VIEW":
        computation = catalog._fetch(
            conn,
            f"SELECT on_query_computation FROM sys.all_mviews{suffix} WHERE owner=:owner AND mview_name=:name",
            params,
        )
        if computation != [("N",)]:
            raise PermissionError("Oracle materialized-view query computation is unproven")
    rows = catalog._fetch(
        conn,
        f"SELECT column_name,data_type,data_type_owner,virtual_column,hidden_column "
        f"FROM sys.all_tab_cols{suffix} WHERE owner=:owner AND table_name=:name",
        params,
    )
    if not rows:
        raise PermissionError("Oracle scalar-column evidence is unavailable")
    columns = {}
    for row in rows:
        if len(row) != 5:
            raise PermissionError("Incomplete Oracle scalar-column evidence")
        column, datatype, type_owner, virtual, hidden = row
        if (
            type_owner is not None
            or virtual != "NO"
            or hidden not in {"YES", "NO"}
            or not _SCALAR.fullmatch(datatype or "")
        ):
            raise PermissionError("Oracle target has unsupported implicit column evaluation")
        if hidden == "NO":
            columns[column] = "UNKNOWN"
    if not columns:
        raise PermissionError("Oracle visible-column evidence is unavailable")
    reject_policy_functions(conn, owner, name, suffix)
    if object_type != "VIEW":
        for view in ("all_external_tables", "all_object_tables"):
            if catalog._fetch(
                conn, f"SELECT table_name FROM sys.{view}{suffix} WHERE owner=:owner AND table_name=:name", params
            ):
                raise PermissionError("Oracle non-scalar table access is unsupported")
        indexes = catalog._fetch(
            conn,
            f"SELECT index_type FROM sys.all_indexes{suffix} WHERE table_owner=:owner AND table_name=:name",
            params,
        )
        if any(
            len(row) != 1 or row[0] not in {"NORMAL", "NORMAL/REV", "BITMAP", "IOT - TOP", "LOB", "CLUSTER"}
            for row in indexes
        ):
            raise PermissionError("Oracle callable or unknown index access is unsupported")
    return columns


def reject_policy_functions(conn, owner, name, suffix):
    # Oracle policies can attach to synonym aliases as well as tables/views.
    if catalog._fetch(
        conn,
        f"SELECT policy_name FROM sys.all_policies{suffix} WHERE object_owner=:owner AND object_name=:name",
        {"owner": owner, "name": name},
    ):
        raise PermissionError("Oracle policy-function targets are unsupported")
