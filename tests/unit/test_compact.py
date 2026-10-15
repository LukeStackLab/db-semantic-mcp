import json

from sqlserver_semantic_mcp.server.compact import compact


def test_r1_drops_none_empty_false():
    assert compact({"a": 1, "b": None, "c": [], "d": {}, "e": False}) == {"a": 1}


def test_r1_keeps_zero_and_empty_string():
    assert compact({"count": 0, "label": ""}) == {"count": 0, "label": ""}


def test_r1_whitelist_preserves_is_nullable_false():
    assert compact({"is_nullable": False, "default_value": False}) == {"is_nullable": False}


def test_unknown_transaction_keeps_retry_prohibition():
    result = compact({"executed": False, "transaction_state": "unknown", "automatic_retry_allowed": False})
    assert result["automatic_retry_allowed"] is False
    assert result["executed"] is False


def test_r2_merges_schema_and_table():
    assert compact({"schema_name": "dbo", "table_name": "Users", "type": "USER_TABLE"}) == {
        "table": "dbo.Users",
        "type": "USER_TABLE",
    }


def test_r3_merges_schema_and_object():
    assert compact({"schema": "dbo", "object_name": "vw_x", "object_type": "VIEW", "depends_on": []}) == {
        "object": "dbo.vw_x",
        "type": "VIEW",
    }


def test_r2_guard_skips_when_empty():
    # Empty strings are preserved by R1 (only None/[]/{}/False are stripped),
    # and the merge guard blocks R2, so both identifier keys remain intact.
    assert compact({"schema_name": "dbo", "table_name": "", "other": 1}) == {
        "schema_name": "dbo",
        "table_name": "",
        "other": 1,
    }


def test_r2_guard_skips_when_none():
    assert compact({"schema_name": "dbo", "table_name": None, "other": 1}) == {
        "schema_name": "dbo",
        "other": 1,
    }


def test_recursion_into_lists_of_dicts():
    got = compact(
        [
            {"schema_name": "dbo", "table_name": "A", "description": None},
            {"schema_name": "dbo", "table_name": "B", "description": "x"},
        ]
    )
    assert got == [
        {"table": "dbo.A"},
        {"table": "dbo.B", "description": "x"},
    ]


def test_recursion_into_nested_dicts():
    got = compact(
        {
            "outer": {
                "schema_name": "dbo",
                "table_name": "X",
                "columns": [
                    {"name": "Id", "is_nullable": False, "description": None},
                ],
            },
        }
    )
    assert got == {
        "outer": {
            "table": "dbo.X",
            "columns": [
                {"name": "Id", "is_nullable": False},
            ],
        },
    }


def test_key_order_preserved_on_merge():
    out = compact({"schema_name": "dbo", "table_name": "Users", "type": "USER_TABLE"})
    assert list(out.keys()) == ["table", "type"]


def test_golden_size_describe_table_reduces_at_least_30pct():
    before = {
        "schema_name": "dbo",
        "table_name": "Users",
        "type": "USER_TABLE",
        "description": None,
        "columns": [
            {"column_name": "Id", "data_type": "int", "is_nullable": False, "default_value": None, "description": None},
            {
                "column_name": "Email",
                "data_type": "nvarchar(255)",
                "is_nullable": False,
                "default_value": None,
                "description": "login email",
            },
        ],
        "primary_keys": ["Id"],
        "foreign_keys": [],
        "indexes": [],
    }
    before_bytes = len(json.dumps(before, indent=2))
    after_bytes = len(json.dumps(compact(before), separators=(",", ":")))
    assert after_bytes < 0.7 * before_bytes, f"expected ≥30% reduction; got before={before_bytes} after={after_bytes}"
