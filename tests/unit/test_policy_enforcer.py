from db_semantic_mcp.policy.analyzer import analyze_sql
from db_semantic_mcp.policy.enforcer import enforce
from db_semantic_mcp.policy.models import (
    PolicyConstraints,
    PolicyOperations,
    PolicyProfile,
    PolicyScope,
)


def _profile(**kwargs) -> PolicyProfile:
    return PolicyProfile(profile_name="t", **kwargs)


def test_select_allowed_by_default():
    p = _profile()
    r = enforce(analyze_sql("SELECT * FROM T"), p)
    assert r.allowed is True


def test_update_blocked_when_not_allowed():
    p = _profile(operations=PolicyOperations(select=True, update=False))
    r = enforce(analyze_sql("UPDATE T SET x=1 WHERE Id=1"), p)
    assert r.allowed is False
    assert "not allowed" in r.reason.lower()


def test_update_requires_where():
    p = _profile(
        operations=PolicyOperations(update=True),
        constraints=PolicyConstraints(require_where_for_update=True),
    )
    r = enforce(analyze_sql("UPDATE T SET x=1"), p)
    assert r.allowed is False
    assert "WHERE" in r.reason


def test_delete_requires_where():
    p = _profile(
        operations=PolicyOperations(delete=True),
        constraints=PolicyConstraints(require_where_for_delete=True),
    )
    r = enforce(analyze_sql("DELETE FROM T"), p)
    assert r.allowed is False


def test_multi_statement_blocked():
    p = _profile(
        constraints=PolicyConstraints(allow_multi_statement=False),
    )
    r = enforce(analyze_sql("SELECT 1; SELECT 2;"), p)
    assert r.allowed is False


def test_denied_table_blocks():
    p = _profile(
        scope=PolicyScope(denied_tables=["secrets"]),
    )
    r = enforce(analyze_sql("SELECT * FROM secrets"), p)
    assert r.allowed is False
    assert "denied" in r.reason.lower()


def test_allowed_table_whitelist():
    p = _profile(
        scope=PolicyScope(allowed_tables=["public"]),
    )
    r = enforce(analyze_sql("SELECT * FROM internal"), p)
    assert r.allowed is False


def test_allowed_database():
    p = _profile(
        scope=PolicyScope(allowed_databases=["prod"]),
    )
    r = enforce(analyze_sql("SELECT 1"), p, database="staging")
    assert r.allowed is False
