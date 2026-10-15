"""Catalog contracts retain native types and trustworthy relationship evidence."""

import json
from unittest.mock import MagicMock

import pytest

from db_semantic_mcp.config import Config
from db_semantic_mcp.infrastructure.backends import oracle_catalog as catalog
from db_semantic_mcp.infrastructure.backends.base import UnsupportedMetadataError
from db_semantic_mcp.infrastructure.cache.structural import StructuralSnapshot


def cfg(**kwargs):
    return Config.from_fixed(database_type="oracle", server="s", oracle={"service_name": "db"}, **kwargs)


def snapshot_results():
    return [
        [("APP", "ORDERS")],
        [
            ("APP", "ORDERS", "AMOUNT", "NUMBER", 22, "N", None, 1, 19, 4, 0, None),
            ("APP", "ORDERS", "NAME", "VARCHAR2", 80, "Y", None, 2, None, None, 20, "C"),
            ("APP", "ORDERS", "TS", "TIMESTAMP(6) WITH TIME ZONE", 13, "Y", None, 3, None, 6, 0, None),
        ],
        [("APP", "ORDERS", "ID")],
        [
            ("APP", "ORDERS", "A", "APP", "CUSTOMERS", "A", "APP.FK", "FK", 1, 0, 0),
            ("APP", "ORDERS", "B", "APP", "CUSTOMERS", "B", "APP.FK", "FK", 2, 0, 0),
        ],
        [
            ("APP", "ORDERS", "PK", "UNIQUE", "VALID", "NORMAL", "A", 1, "P", "ENABLED", "VALIDATED", "NOT DEFERRABLE"),
            ("APP", "ORDERS", "PK", "UNIQUE", "VALID", "NORMAL", "B", 2, "P", "ENABLED", "VALIDATED", "NOT DEFERRABLE"),
            ("APP", "ORDERS", "FX", "UNIQUE", "VALID", "FUNCTION-BASED NORMAL", "SYS_NC1", 1, None, None, None, None),
            ("APP", "ORDERS", "DEFER", "UNIQUE", "VALID", "NORMAL", "A", 1, "U", "ENABLED", "VALIDATED", "DEFERRABLE"),
        ],
        [("APP", "ORDERS", "TABLE"), ("APP", "P", "PACKAGE"), ("APP", "P", "PACKAGE BODY"), ("APP", "S", "SYNONYM")],
        [("APP", "ORDERS", None, "orders")],
    ]


def test_snapshot_tuple_contracts_native_types_and_conservative_indexes(monkeypatch):
    results = iter(snapshot_results())
    calls = []

    def fetch(conn, sql, params=None):
        calls.append((sql, params))
        return next(results)

    monkeypatch.setattr(catalog, "_fetch", fetch)
    snap = catalog.fetch_snapshot(cfg(), MagicMock())
    assert all(len(r) == 12 for r in snap.columns)
    assert [r[3] for r in snap.columns] == ["NUMBER(19,4)", "VARCHAR2(20 CHAR)", "TIMESTAMP(6) WITH TIME ZONE"]
    assert all(len(r) == 11 for r in snap.foreign_keys)
    assert [r[8] for r in snap.foreign_keys] == [1, 2]
    assert all(len(r) == 8 for r in snap.indexes)
    pk, functional, deferred = snap.indexes
    assert json.loads(pk[5]) == ["A", "B"] and pk[3:5] == (1, 1)
    assert functional[3] == 0 and functional[6:] == (1, 1)
    assert deferred[3] == 0 and deferred[7] == 1
    assert snap.comments[0][2] == ""
    assert all("DBA_" not in sql for sql, _ in calls)
    assert "c.deferrable='NOT DEFERRABLE'" in calls[2][0]


@pytest.mark.parametrize(
    "values,expected",
    [
        (("NUMBER", None, None, 0, None), "NUMBER"),
        (("NUMBER", None, 4, 0, None), "NUMBER(*,4)"),
        (("NUMBER", 8, -2, 0, None), "NUMBER(8,-2)"),
        (("VARCHAR2", None, None, 30, "B"), "VARCHAR2(30 BYTE)"),
        (("NCHAR", None, None, 10, "C"), "NCHAR(10)"),
        (("FLOAT", 126, None, 0, None), "FLOAT(126)"),
    ],
)
def test_native_type(values, expected):
    assert catalog.native_type(*values) == expected


def test_link_is_bound_validated_catalog_confirmed_and_private_precedence(monkeypatch):
    conn = MagicMock()
    config = cfg(sources={"erp": {"db_link": "erp_1"}}).for_source("erp")
    seen = []

    def fetch(conn, sql, params=None):
        if "user_users@" in sql:
            return [("PRIVATE_APP",)]
        seen.append((sql, params))
        return [("PUBLIC", "ERP_1.EXAMPLE", "PUBLIC_APP", "host"), ("APP", "ERP_1.EXAMPLE", "PRIVATE_APP", "host")]

    monkeypatch.setattr(catalog, "_fetch", fetch)
    route = catalog.resolve_db_link(config, conn)
    assert route["username"] == "PRIVATE_APP"
    assert catalog.catalog_suffix(config, conn) == "@ERP_1.EXAMPLE"
    assert seen[0][1] == {"link": "ERP_1", "prefix": "ERP\\_1.%"}
    assert "ERP_1" not in seen[0][0]


@pytest.mark.parametrize(
    "rows", [[], [("APP", "X.A", "U", "h"), ("APP", "X.B", "U", "h")], [("APP", "X;DROP", "U", "h")]]
)
def test_link_missing_ambiguous_or_unsafe_fails_closed(monkeypatch, rows):
    monkeypatch.setattr(catalog, "_fetch", lambda *args: rows)
    with pytest.raises(ValueError):
        catalog.resolve_db_link(cfg(sources={"x": {"db_link": "X"}}).for_source("x"), MagicMock())


def test_connected_user_link_principal_is_probed(monkeypatch):
    def fetch(conn, sql, params=None):
        if "user_users@" in sql:
            return [("REMOTE_APP",)]
        return [("APP", "X", None, "host")]

    monkeypatch.setattr(catalog, "_fetch", fetch)
    route = catalog.resolve_db_link(cfg(sources={"x": {"db_link": "X"}}).for_source("x"), MagicMock())
    assert route["username"] is None and route["remote_user"] == "REMOTE_APP"


@pytest.mark.parametrize("principals", [[], [("A",), ("B",)], [(None,)]])
def test_remote_principal_must_be_proven(monkeypatch, principals):
    monkeypatch.setattr(
        catalog,
        "_fetch",
        lambda conn, sql, params=None: principals if "user_users@" in sql else [("APP", "X", None, "host")],
    )
    with pytest.raises(UnsupportedMetadataError, match="principal"):
        catalog.resolve_db_link(cfg(sources={"x": {"db_link": "X"}}).for_source("x"), MagicMock())


def test_definition_values_are_bound(monkeypatch):
    from contextlib import nullcontext

    monkeypatch.setattr(catalog, "_connection", lambda *args: nullcontext(MagicMock()))
    calls = []

    def fetch(conn, sql, params):
        calls.append((sql, params))
        return [("CREATE PACKAGE ",), ("test AS END;",)]

    monkeypatch.setattr(catalog, "_fetch", fetch)
    definition = catalog.get_object_definition(cfg(), 'APP";bad', "Pkg", "PACKAGE")
    assert definition == "CREATE PACKAGE test AS END;"
    assert calls[0][1] == {"owner": 'APP";bad', "name": "Pkg", "type": "PACKAGE"}
    assert 'APP";bad' not in calls[0][0]


def test_fingerprint_tracks_comments_constraints_definitions_and_is_stable(monkeypatch):
    from db_semantic_mcp.infrastructure.cache import probe

    monkeypatch.setattr(probe, "filter_fingerprints", lambda cfg, fps: fps)
    snap = StructuralSnapshot([("APP", "T")], [], [], [], [], [("APP", "P", "PACKAGE")], [("APP", "T", "", "first")])
    monkeypatch.setattr(catalog, "fetch_snapshot", lambda *args: snap)
    state = {"constraint": "ENABLED", "definition": "PACKAGE P AS END;"}

    def fetch(conn, sql, params=None):
        if "all_source" in sql:
            return [("APP", "P", "PACKAGE", 1, state["definition"])]
        if "all_constraints" in sql:
            return [("APP", "T", "PK", "P", state["constraint"])]
        return []

    monkeypatch.setattr(catalog, "_fetch", fetch)
    first = catalog.fetch_fingerprints(cfg(), MagicMock())
    assert first == catalog.fetch_fingerprints(cfg(), MagicMock())
    snap.comments[0] = ("APP", "T", "", "second")
    second = catalog.fetch_fingerprints(cfg(), MagicMock())
    assert first.tables != second.tables
    state["constraint"] = "DISABLED"
    third = catalog.fetch_fingerprints(cfg(), MagicMock())
    assert second.tables != third.tables
    state["definition"] = "PACKAGE P AS PROCEDURE NEW_P; END;"
    assert third.objects != catalog.fetch_fingerprints(cfg(), MagicMock()).objects


def test_inventory_scope_pushes_schema_binds_and_explains_system_default(monkeypatch):
    from types import SimpleNamespace

    from db_semantic_mcp.services import policy_service

    policy = SimpleNamespace(scope=SimpleNamespace(allowed_schemas=["SYS", '"Quoted Schema"']))
    monkeypatch.setattr(
        policy_service, "PolicyService", lambda cfg: SimpleNamespace(effective_scope=lambda: policy.scope)
    )
    owners, params = catalog._inventory_scope(cfg(), "@LINK")
    assert owners == ":schema_0,:schema_1"
    assert params == {"schema_0": "SYS", "schema_1": "Quoted Schema"}
    policy.scope.allowed_schemas = []
    owners, params = catalog._inventory_scope(cfg(), "@LINK")
    assert "all_users@LINK" in owners and "oracle_maintained='N'" in owners and params == {}
