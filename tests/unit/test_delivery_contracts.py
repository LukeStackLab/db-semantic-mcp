"""Public delivery boundaries: compact provenance and actionable lazy imports."""

import json
from types import SimpleNamespace

import pytest

from db_semantic_mcp.config import Config
from db_semantic_mcp.infrastructure.backends.oracle import MissingOracleDriverError, OracleBackend
from db_semantic_mcp.server.source_context import source_metadata


@pytest.mark.parametrize("mode", ["thin", "thick"])
def test_source_budget_retains_precision_and_routing(mode):
    cfg = Config.from_fixed(
        database_type="oracle",
        server="example",
        oracle={"service_name": "demo", "driver_mode": mode},
        sources={"remote": {"db_link": "EXAMPLE.LINK"}},
    )
    full = OracleBackend().capabilities(cfg)
    local = source_metadata(cfg)
    assert len(json.dumps(local).encode()) < 300
    assert local["capabilities"]["native_timestamp_max_precision"] == (9 if mode == "thick" else 6)
    assert local["capabilities"]["native_timestamp_timezone"] is (mode == "thick")
    assert local["capabilities"]["remote_sources"] is True
    assert "query_timeout_semantics" not in local["capabilities"]
    assert len(json.dumps(local).encode()) < len(json.dumps({**local, "capabilities": full}).encode()) / 2
    remote = source_metadata(cfg.for_source("remote"))
    assert remote["db_link"] == "EXAMPLE.LINK"
    assert "EXAMPLE.LINK" in remote["query_hint"]


@pytest.mark.asyncio
async def test_policy_keeps_full_capability_guidance(monkeypatch):
    from db_semantic_mcp.server.tools import policy
    from db_semantic_mcp.services.policy_service import PolicyService

    cfg = Config.from_fixed(database_type="oracle", server="example", oracle={"service_name": "demo"})
    monkeypatch.setattr(policy, "get_context", lambda: SimpleNamespace(cfg=cfg, policy=PolicyService(cfg)))
    out = await policy._get_policy({})
    assert "per network round trip" in out["capabilities"]["query_timeout_semantics"]
    assert "TO_CHAR" in out["capabilities"]["timestamp_workaround"]
    assert "ORACLE_MAINTAINED" in out["capabilities"]["inventory_scope"]


def test_driver_missing_reaches_doctor_boundary(monkeypatch):
    from db_semantic_mcp.infrastructure.backends import oracle
    from db_semantic_mcp.setup import oracle_checks
    from db_semantic_mcp.setup.files import SetupError

    def missing(_):
        raise ImportError("sensitive driver detail")

    monkeypatch.setattr(oracle.importlib, "import_module", missing)
    cfg = Config.from_fixed(database_type="oracle", server="example", oracle={"service_name": "demo"})
    with pytest.raises(MissingOracleDriverError, match=r"uv pip install '\.\[oracle\]'"):
        oracle._driver()
    with pytest.raises(SetupError, match="Oracle driver is missing") as err:
        oracle_checks.check_isolated(cfg)
    assert "sensitive" not in str(err.value)


def test_version_works_without_configuration(capsys):
    from db_semantic_mcp.main import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == "db-semantic-mcp 0.8.0\n"


def test_config_repr_does_not_include_password():
    cfg = Config.from_fixed(server="test", database="db", password="private-password-marker")
    assert "private-password-marker" not in repr(cfg)
    assert cfg.password == "private-password-marker"
