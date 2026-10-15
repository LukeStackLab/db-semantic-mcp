"""Unit tests for find_join_path's score flag (folds in score_join_candidate)."""

import pytest


def _path_result(*, ambiguous=False, complete=True):
    path = [{"to_schema": "dbo", "to_table": "Bridge", "metadata_complete": complete}]
    candidates = [{"path": path, "metadata_complete": complete, "hops": 1}]
    if ambiguous:
        candidates.append({"path": path, "metadata_complete": complete, "hops": 1})
    return {
        "found": True,
        "path": [] if ambiguous else path,
        "candidates": candidates,
        "ambiguous": ambiguous,
        "metadata_complete": complete,
        "truncated": False,
        "next_action": "select_join_candidate"
        if ambiguous
        else "review_join"
        if complete
        else "refresh_relationship_metadata",
    }


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_SERVER", "x")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_DATABASE", "testdb")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_USER", "u")
    monkeypatch.setenv("SEMANTIC_MCP_MSSQL_PASSWORD", "p")
    monkeypatch.setenv("SEMANTIC_MCP_CACHE_PATH", str(tmp_path / "t.db"))
    from sqlserver_semantic_mcp.config import reset_config

    reset_config()
    from sqlserver_semantic_mcp.server.app import reset_context

    reset_context()


@pytest.mark.asyncio
async def test_find_join_path_score_false_skips_classify(monkeypatch, env):
    from sqlserver_semantic_mcp.server.tools import relationship

    async def fake_path(*a, **k):
        return _path_result()

    called = {"n": 0}

    async def fake_classify(*a, **k):
        called["n"] += 1
        return {"type": "bridge"}

    monkeypatch.setattr(relationship.relationship_service, "find_join_candidates", fake_path)
    monkeypatch.setattr(
        "sqlserver_semantic_mcp.services.semantic_service.classify_table",
        fake_classify,
    )

    out = await relationship._path({"from_schema": "dbo", "from_table": "A", "to_schema": "dbo", "to_table": "B"})
    assert out == _path_result()
    assert called["n"] == 0  # perf check: score=false must not run per-hop classify


@pytest.mark.asyncio
async def test_find_join_path_score_true_returns_confidence(monkeypatch, env):
    from sqlserver_semantic_mcp.server.tools import relationship

    async def fake_path(*a, **k):
        return _path_result()

    async def fake_classify(*a, **k):
        return {"type": "bridge"}

    monkeypatch.setattr(relationship.relationship_service, "find_join_candidates", fake_path)
    monkeypatch.setattr(
        "sqlserver_semantic_mcp.services.semantic_service.classify_table",
        fake_classify,
    )

    out = await relationship._path(
        {"from_schema": "dbo", "from_table": "A", "to_schema": "dbo", "to_table": "B", "score": True}
    )
    assert out["found"] is True
    assert "confidence" in out and out["confidence"] < 1.0  # bridge hop penalises
    assert out["penalties"]
    assert out["next_action"] != "execute"
    assert out.get("recommended_tool") != "plan_or_execute_query"


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous,complete", [(True, True), (False, False), (False, True)])
async def test_score_never_recommends_execution(monkeypatch, env, ambiguous, complete):
    from sqlserver_semantic_mcp.server.tools import relationship

    async def fake_path(*args, **kwargs):
        return _path_result(ambiguous=ambiguous, complete=complete)

    async def fake_classify(*args, **kwargs):
        return {"type": "dimension"}

    monkeypatch.setattr(relationship.relationship_service, "find_join_candidates", fake_path)
    monkeypatch.setattr(relationship.semantic_service, "classify_table", fake_classify)
    result = await relationship._path(
        {"from_schema": "dbo", "from_table": "A", "to_schema": "dbo", "to_table": "B", "score": True}
    )
    assert result["next_action"] != "execute"
    assert result.get("recommended_tool") != "plan_or_execute_query"
    assert result["ambiguous"] is ambiguous


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler,args",
    [
        ("_rels", {"schema": "dbo", "table": "Denied"}),
        ("_chain", {"schema": "dbo", "table": "Denied"}),
        ("_path", {"from_schema": "dbo", "from_table": "Allowed", "to_schema": "dbo", "to_table": "Denied"}),
    ],
)
async def test_relationship_tools_authorize_explicit_objects(monkeypatch, handler, args):
    from types import SimpleNamespace

    from sqlserver_semantic_mcp.server.tools import relationship

    class Policy:
        def authorize_object(self, schema, name, database=""):
            if name == "Denied":
                raise PermissionError("denied")

    monkeypatch.setattr(
        relationship,
        "get_context",
        lambda: SimpleNamespace(
            cfg=SimpleNamespace(mssql_database="db"),
            policy=Policy(),
        ),
    )
    with pytest.raises(PermissionError, match="denied"):
        await getattr(relationship, handler)(args)
