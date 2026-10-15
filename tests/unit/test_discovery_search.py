import pytest

from sqlserver_semantic_mcp.config import Config
from sqlserver_semantic_mcp.workflows.discovery_flow import _score, _tokenize, discover_relevant_tables


def test_discovery_terms_are_unique_and_qualified_match_ranks_first():
    table = {"schema_name": "sales", "table_name": "Orders"}
    assert _tokenize("orders orders orders") == ["orders"]
    assert _score(table, ["orders"])[0] == _score(table, ["orders"] * 3)[0]
    assert _score(table, ["sales.orders"])[0] > _score(table, ["orders"])[0]


@pytest.mark.asyncio
async def test_chinese_alias_description_and_policy_search(tmp_path, monkeypatch):
    from sqlserver_semantic_mcp.workflows import discovery_flow

    cfg = Config.model_construct(
        mssql_server="fake",
        mssql_database="db",
        cache_path=str(tmp_path / "fake"),
        discovery_aliases={"供應商": ["vendor"]},
    )

    async def documents(_cfg):
        return [
            {"schema_name": "sales", "table_name": "Orders"},
            {"schema_name": "hr", "table_name": "Orders"},
            {"schema_name": "sales", "table_name": "T42", "description": "供應商 vendor"},
        ]

    monkeypatch.setattr(discovery_flow, "_documents", documents)

    class Policy:
        def object_allowed(self, schema, name, database):
            return schema == "sales"

    result = await discover_relevant_tables("查詢訂單", cfg=cfg, policy=Policy())
    assert [row["table"] for row in result["data"]["candidates"]] == ["sales.Orders"]
    result = await discover_relevant_tables("供應商", cfg=cfg, policy=Policy())
    assert result["data"]["candidates"][0]["table"] == "sales.T42"
    assert any("description" in reason for reason in result["data"]["candidates"][0]["why"])


@pytest.mark.asyncio
async def test_qualified_exact_match_wins_when_scores_saturate_and_limit_truncates(tmp_path, monkeypatch):
    from sqlserver_semantic_mcp.workflows import discovery_flow

    cfg = Config.model_construct(mssql_server="fake", mssql_database="db", cache_path=str(tmp_path / "fake"))
    docs = [
        {"schema_name": "aaa", "table_name": "CustomerStatus", "description": "total amount"},
        {"schema_name": "sales", "table_name": "Orders"},
    ]

    async def documents(_cfg):
        return docs

    monkeypatch.setattr(discovery_flow, "_documents", documents)

    class Policy:
        def object_allowed(self, *args):
            return True

    goal = "sales.Orders customer status total amount"
    assert [_score(doc, _tokenize(goal))[0] for doc in docs] == [1.0, 1.0]
    result = await discover_relevant_tables(goal, cfg=cfg, policy=Policy(), limit=1)
    assert result["data"]["candidates"][0]["table"] == "sales.Orders"
    assert result["data"]["truncated"] is True
