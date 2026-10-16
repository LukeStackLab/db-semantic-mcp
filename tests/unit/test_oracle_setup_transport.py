import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from db_semantic_mcp.config import OracleOptions
from db_semantic_mcp.infrastructure.backends.oracle_transport import connection_params
from db_semantic_mcp.setup import oracle_checks, oracle_wizard, wizard
from db_semantic_mcp.setup.files import SetupError
from tests.unit.test_oracle_backend import oracle_config


class Params:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def parse_connect_string(self, dsn):
        if dsn == "mixed":
            self.protocol = ["tcps", "tcp"]
        elif dsn == "tcp":
            self.protocol = "tcp"
        else:
            self.protocol = "tcps"
            self.ssl_server_dn_match = False

    def set(self, **kwargs):
        self.__dict__.update(kwargs)


@pytest.mark.parametrize("mode", ["thin", "thick"])
def test_transport_enforces_all_addresses_and_overrides_dsn_dn_disable(mode):
    driver = SimpleNamespace(ConnectParams=Params)
    cfg = oracle_config(encrypt=True, oracle={"dsn": "tcps", "driver_mode": mode})
    result = connection_params(driver, cfg)
    assert result.protocol == "tcps" and result.ssl_server_dn_match and result.thick_mode_dsn_passthrough is False
    for dsn in ["mixed", "tcp"]:
        cfg.oracle = OracleOptions(dsn=dsn, driver_mode=mode)
        with pytest.raises(ValueError, match="must match encrypt"):
            connection_params(driver, cfg)
    cfg.encrypt = False
    cfg.oracle = OracleOptions(dsn="tcps", driver_mode=mode)
    with pytest.raises(ValueError):
        connection_params(driver, cfg)


@pytest.mark.parametrize(
    "dsn",
    [
        "user/password@host/db",
        "tcps://user:password@host/db",
        "(DESCRIPTION=(PASSWORD=SECRET))",
        "tcps://host/db?wallet_password=SECRET",
    ],
)
def test_config_rejects_dsn_embedded_credentials(dsn):
    with pytest.raises(ValueError, match="must not contain credentials"):
        OracleOptions(dsn=dsn)


def test_each_wizard_oracle_probe_has_fresh_process_and_secret_only_stdin(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"visible_tables":1,"driver_mode":"test"}')

    monkeypatch.setattr(oracle_checks.subprocess, "run", run)
    for mode in ["thin", "thick", "thin"]:
        cfg = oracle_config(oracle={"service_name": "db", "driver_mode": mode})
        oracle_checks.check_isolated(cfg)
    assert len(calls) == 3
    assert all(
        "-I" in args and "secret" not in str(args) and "secret" in kw["input"] and kw["capture_output"]
        for args, kw in calls
    )
    assert [json.loads(kw["input"])["oracle"]["driver_mode"] for _, kw in calls] == ["thin", "thick", "thin"]


def test_doctor_errors_never_echo_driver_output(monkeypatch):
    monkeypatch.setattr(
        oracle_checks.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stdout="SECRET invalid json", stderr="PASSWORD=SECRET"),
    )
    with pytest.raises(SetupError) as exc:
        oracle_checks.check_isolated(oracle_config())
    assert "SECRET" not in str(exc.value)


def test_doctor_reports_actual_version_mode_transport_and_source_failures(monkeypatch):
    cfg = oracle_config(sources={"denied": {"db_link": "DENIED"}})
    conn = MagicMock()
    conn.thin = True
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(oracle_checks, "open_connection", lambda cfg: context)
    seen = []

    def fetch(conn, sql):
        seen.append(sql)
        if "product_component_version" in sql:
            return [("Oracle AI Database 26ai Free", "23.26.3.0.0")]
        if "NETWORK_PROTOCOL" in sql:
            return [("tcp",)]
        if "SERVICE_NAME" in sql:
            return [("FREEPDB1",)]
        if "all_tables" in sql:
            return [(3,)]
        if "all_db_links" in sql:
            return [("APP", "DENIED")]
        raise AssertionError(sql)

    monkeypatch.setattr(oracle_checks, "_fetch", fetch)
    r = oracle_checks.check_oracle(cfg)
    assert r["version"] == "23.26.3.0.0" and r["driver_mode"] == "thin" and r["transport"] == "tcp"
    assert r["sources"]["denied"]["status"] == "unavailable"
    assert all(sql.startswith("SELECT") for sql in seen)


def test_oracle_wizard_options_and_provider_stays_fixed(tmp_path):
    from db_semantic_mcp.profiles import ConnectionProfile

    previous = ConnectionProfile(
        name="ora",
        database_type="oracle",
        server="host",
        database="db",
        user="reader",
        encrypt=False,
        oracle={"service_name": "db"},
    )
    answers = iter(["thin", "", "service", "host", "", "db", "n", "reader"])
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return next(answers)

    result, password = wizard._gather_connection("ora", previous, tmp_path, ask, lambda _: "secret", readonly=True)
    assert result.database_type == "oracle" and result.oracle.service_name == "db" and not result.encrypt
    assert not any("資料庫類型" in prompt for prompt in prompts)


def test_source_selection_requires_explicit_scope_and_consent():
    from db_semantic_mcp.profiles import ConnectionProfile

    profile = ConnectionProfile(
        name="ora",
        database_type="oracle",
        server="host",
        database="db",
        user="reader",
        encrypt=False,
        oracle={"service_name": "db"},
    )
    answers = iter(["y", "1", "sales", "APP", "y", "n"])
    updated, scopes = oracle_wizard.select_sources(
        profile, [{"owner": "APP", "db_link": "SALES.EXAMPLE"}], lambda _: next(answers), lambda _: None
    )
    assert updated.sources["sales"].readonly
    assert (
        scopes["sales"].allowed_schemas == ["APP"]
        and scopes["sales"].operations.select
        and not scopes["sales"].operations.update
    )
    answers = iter(["y", "1", "sales", "APP", "n", "n"])
    updated, scopes = oracle_wizard.select_sources(
        profile, [{"owner": "APP", "db_link": "SALES.EXAMPLE"}], lambda _: next(answers), lambda _: None
    )
    assert not updated.sources and not scopes


def test_new_oracle_wizard_preflight_does_not_require_unwritten_fixed_policy(tmp_path, monkeypatch):
    from db_semantic_mcp.profiles import ConnectionProfile
    from db_semantic_mcp.services.policy_service import PolicyService

    profile = ConnectionProfile(
        name="new",
        database_type="oracle",
        server="host",
        database="service",
        user="reader",
        encrypt=False,
        oracle={"service_name": "service"},
    )
    monkeypatch.setattr(wizard, "_gather_connection", lambda *a, **kw: (profile, "secret"))

    def check(cfg):
        assert cfg.connection_name is None
        PolicyService(cfg).load()
        return {"visible_tables": 1}

    monkeypatch.setattr(wizard, "check_connection", check)
    result = wizard._collect_connection("new", None, tmp_path, lambda _: "", lambda _: "secret", lambda _: None, None)
    assert result[0] is profile
