import json
import pytest
from sqlserver_semantic_mcp.policy.loader import (
    load_policy_from_file, builtin_readonly,
)


def test_builtin_readonly_is_safe():
    p = builtin_readonly()
    assert p.operations.select is True
    assert p.operations.update is False
    assert p.operations.delete is False
    assert p.operations.drop is False


def test_load_policy_file(tmp_path):
    pf = tmp_path / "policy.json"
    pf.write_text(json.dumps({
        "active_profile": "rw",
        "profiles": {
            "rw": {
                "profile_name": "rw",
                "operations": {"select": True, "update": True},
                "constraints": {"max_rows_returned": 500},
            }
        }
    }))
    profile = load_policy_from_file(str(pf), profile_override=None)
    assert profile.profile_name == "rw"
    assert profile.operations.update is True
    assert profile.constraints.max_rows_returned == 500


def test_profile_override(tmp_path):
    pf = tmp_path / "policy.json"
    pf.write_text(json.dumps({
        "active_profile": "a",
        "profiles": {
            "a": {"profile_name": "a"},
            "b": {"profile_name": "b", "operations": {"select": False}},
        }
    }))
    profile = load_policy_from_file(str(pf), profile_override="b")
    assert profile.operations.select is False


def test_unknown_profile_raises(tmp_path):
    pf = tmp_path / "policy.json"
    pf.write_text(json.dumps({
        "active_profile": "nope",
        "profiles": {"a": {"profile_name": "a"}}
    }))
    with pytest.raises(ValueError, match="not found"):
        load_policy_from_file(str(pf), profile_override=None)


def test_missing_file_returns_readonly():
    p = load_policy_from_file(None, profile_override=None)
    assert p.profile_name == "readonly"


def test_explicit_missing_file_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="policy"):
        load_policy_from_file(str(tmp_path / "missing.json"), profile_override=None)


def test_unreadable_policy_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="policy"):
        load_policy_from_file(str(tmp_path), profile_override=None)


def test_unknown_scope_fields_fail_closed(tmp_path):
    pf = tmp_path / "typo.json"
    pf.write_text(json.dumps({"profiles": {"readonly": {
        "profile_name": "readonly", "scope": {"denied_table": ["Secrets"]},
    }}}))
    with pytest.raises(ValueError, match="policy"):
        load_policy_from_file(str(pf), profile_override=None)
