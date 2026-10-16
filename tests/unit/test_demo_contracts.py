"""Controller guards are tested without a Docker daemon or real credentials."""

import importlib.util
import json
import socket
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import demo_fixtures  # noqa: E402
import demo_support as demo  # noqa: E402


def test_private_state_permissions_and_stable_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "RUNTIME", tmp_path / "private")
    state = demo.load(create=True)
    first = (demo.RUNTIME / ".env").read_bytes()
    assert demo.load(create=True) == state
    assert (demo.RUNTIME / ".env").read_bytes() == first
    assert demo.RUNTIME.stat().st_mode & 0o777 == 0o700
    for name in (".env", "state.json"):
        assert (demo.RUNTIME / name).stat().st_mode & 0o777 == 0o600
    assert state["token"].encode() in first


def test_missing_state_refuses_existing_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "RUNTIME", tmp_path)
    (tmp_path / "unknown").write_text("user data")
    with pytest.raises(demo.DemoError, match="nonempty"):
        demo.load(create=True)


@pytest.mark.parametrize("kind", ["container", "volume", "network"])
def test_unknown_resource_ownership_refused(kind, monkeypatch):
    name = next(n for k, n in demo.resources() if k == kind)
    monkeypatch.setattr(demo, "inspect", lambda k, n: {} if n == name else None)
    with pytest.raises(demo.DemoError, match="unowned"):
        demo.verify_ownership({"token": "owned", "remote": False})


def test_matching_project_without_matching_token_refused(monkeypatch):
    monkeypatch.setattr(
        demo,
        "inspect",
        lambda *_: {"Config": {"Labels": {"com.docker.compose.project": demo.PROJECT, demo.LABEL: "other"}}},
    )
    with pytest.raises(demo.DemoError, match="unowned"):
        demo.verify_ownership({"token": "owned", "remote": False})


def test_extra_compose_resource_refused(monkeypatch):
    monkeypatch.setattr(demo, "inspect", lambda *_: None)
    monkeypatch.setattr(demo, "run", lambda *_: "unrecognized-container\n")
    with pytest.raises(demo.DemoError, match="Unexpected"):
        demo.verify_ownership({"token": "owned", "remote": False})


def test_bound_port_refuses_up(monkeypatch):
    monkeypatch.setattr(demo, "inspect", lambda *_: None)
    monkeypatch.setattr(demo, "run", lambda *_: "")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        monkeypatch.setitem(demo.SERVICES, "sql", sock.getsockname()[1])
        with pytest.raises(demo.DemoError, match="occupied"):
            demo.verify_ownership({"token": "owned", "remote": False}, ports=True)


def test_repeated_initialization_never_calls_bootstrap(monkeypatch):
    state = {
        "remote": True,
        "initialized": ["sql", "oracle", "remote", "links", "profile-sql", "profile-oracle", "profile-oracle-remote"],
    }
    monkeypatch.setattr(demo_fixtures, "environment", lambda: {})

    def unexpected(*_args, **_kwargs):
        pytest.fail("Completed fixture must not be written again")

    for name in ("initialize_sql", "initialize_oracle", "initialize_links", "write_profile", "save"):
        monkeypatch.setattr(demo_fixtures, name, unexpected)
    demo_fixtures.initialize(state)


def test_subprocess_failure_does_not_print_credentials(monkeypatch):
    import subprocess

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 1, stdout="secret password", stderr="secret password"),
    )
    with pytest.raises(demo.DemoError) as exc:
        demo.run(["test-command"], input="secret password")
    assert "secret password" not in str(exc.value)


def test_smoke_keeps_explicit_prefixed_password_and_wallet(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "installed_smoke", SCRIPTS.parent / "tests/integration/check_installed.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from db_semantic_mcp.profiles import ConnectionProfile, profile_bytes

    profile = ConnectionProfile(
        name="env_test",
        database_type="oracle",
        database="FREEPDB1",
        server="localhost",
        user="u",
        encrypt=False,
        oracle={
            "service_name": "FREEPDB1",
            "wallet_password_env": "SEMANTIC_MCP_TEST_WALLET",
            "wallet_location": str(tmp_path),
        },
        credential={"storage": "env", "key": "SEMANTIC_MCP_TEST_PASSWORD"},
    )
    path = tmp_path / "connection.json"
    path.write_bytes(profile_bytes(profile))
    monkeypatch.setenv("SEMANTIC_MCP_TEST_PASSWORD", "password")
    monkeypatch.setenv("SEMANTIC_MCP_TEST_WALLET", "wallet")
    monkeypatch.setenv("SEMANTIC_MCP_SERVER", "unrelated-host")
    env = module.child_environment(path)
    assert env["SEMANTIC_MCP_TEST_PASSWORD"] == "password"
    assert env["SEMANTIC_MCP_TEST_WALLET"] == "wallet"
    assert "SEMANTIC_MCP_SERVER" not in env
    assert json.loads(path.read_text())["credential"]["key"] == "SEMANTIC_MCP_TEST_PASSWORD"


def test_down_stops_without_removing_data(monkeypatch):
    import demo as controller

    monkeypatch.setattr(controller, "bind_local_docker", lambda: None)

    state = {"token": "owned", "remote": True, "initialized": []}
    monkeypatch.setattr(controller, "load", lambda **_: state)
    monkeypatch.setattr(controller, "verify_ownership", lambda *a, **kw: {})
    calls = []
    monkeypatch.setattr(controller, "compose", lambda *args: calls.append(args))
    monkeypatch.setattr(controller, "run", lambda *a, **kw: pytest.fail("stop must not remove resources"))
    monkeypatch.setattr(sys, "argv", ["demo.py", "down"])
    controller.main()
    assert calls == [(state, "stop", "--timeout", "30")]


def test_purge_removes_only_verified_resources(tmp_path, monkeypatch):
    import demo as controller

    monkeypatch.setattr(controller, "bind_local_docker", lambda: None)

    state = {"token": "owned", "remote": True, "initialized": []}
    owned = {f"{demo.PROJECT}-sql": {}, f"{demo.PROJECT}-sql-data": {}}
    monkeypatch.setattr(controller, "load", lambda **_: state)
    monkeypatch.setattr(controller, "verify_ownership", lambda *a, **kw: owned)
    monkeypatch.setattr(controller, "RUNTIME", tmp_path)
    monkeypatch.setattr(controller, "save", lambda _: None)
    calls = []
    monkeypatch.setattr(controller, "run", lambda args: calls.append(args))
    monkeypatch.setattr(sys, "argv", ["demo.py", "down", "--purge"])
    controller.main()
    assert calls == [
        ["docker", "container", "rm", "-f", f"{demo.PROJECT}-sql"],
        ["docker", "volume", "rm", f"{demo.PROJECT}-sql-data"],
    ]
    assert state["initialized"] == [] and state["remote"] is False


def test_init_missing_data_volume_is_refused(monkeypatch):
    import demo as controller

    monkeypatch.setattr(controller, "bind_local_docker", lambda: None)

    monkeypatch.setattr(controller, "load", lambda **_: {"token": "owned", "remote": False, "initialized": ["sql"]})
    monkeypatch.setattr(controller, "verify_ownership", lambda *a, **kw: {})
    monkeypatch.setattr(controller, "initialize", lambda _: pytest.fail("missing data must not be recreated"))
    monkeypatch.setattr(sys, "argv", ["demo.py", "init"])
    with pytest.raises(demo.DemoError, match="missing"):
        controller.main()


@pytest.mark.parametrize("problem", ["absent", "wrong-port", "wrong-volume"])
def test_bootstrap_refuses_unknown_endpoint_or_data(problem):
    state = {"remote": False}
    found = {}
    for service in ("sql", "oracle"):
        internal = "1433/tcp" if service == "sql" else "1521/tcp"
        found[f"{demo.PROJECT}-{service}"] = {
            "State": {"Running": True},
            "NetworkSettings": {
                "Ports": {internal: [{"HostIp": "127.0.0.1", "HostPort": str(demo.SERVICES[service])}]}
            },
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": f"{demo.PROJECT}-{service}-data",
                    "Destination": "/var/opt/mssql" if service == "sql" else "/opt/oracle/oradata",
                }
            ],
        }
    sql = found[f"{demo.PROJECT}-sql"]
    if problem == "absent":
        found.clear()
    elif problem == "wrong-port":
        sql["NetworkSettings"]["Ports"]["1433/tcp"][0]["HostPort"] = "1433"
    else:
        sql["Mounts"][0]["Name"] = "user-data"
    with pytest.raises(demo.DemoError):
        demo.verify_ready(state, found)


@pytest.fixture
def local_docker_socket():
    # macOS sockaddr_un paths are shorter than pytest's default temp paths.
    with tempfile.TemporaryDirectory(prefix="dmcp-", dir="/tmp") as directory:
        with socket.socket(socket.AF_UNIX) as sock:
            path = Path(directory).resolve() / "docker.sock"
            sock.bind(str(path))
            yield "unix://" + str(path)


@pytest.mark.parametrize(
    "variables,selected_local",
    [
        ({}, True),
        ({"DOCKER_CONTEXT": "desktop-linux"}, True),
        ({"DOCKER_CONTEXT": "remote"}, False),
        ({"DOCKER_HOST": "tcp://192.0.2.1:2375"}, False),
        ({"DOCKER_CONTEXT": "desktop-linux", "DOCKER_HOST": "tcp://192.0.2.1:2375"}, False),
        ({"DOCKER_CONTEXT": "remote", "DOCKER_HOST": "LOCAL"}, True),
    ],
)
def test_daemon_selection_uses_cli_effective_endpoint(monkeypatch, local_docker_socket, variables, selected_local):
    # Docker 29.8.2 chooses DOCKER_HOST before DOCKER_CONTEXT; resolution is
    # delegated to CLI so another version's precedence is respected as well.
    for key in ("DOCKER_CONTEXT", "DOCKER_HOST"):
        monkeypatch.delenv(key, raising=False)
    for key, value in variables.items():
        monkeypatch.setenv(key, local_docker_socket if value == "LOCAL" else value)
    monkeypatch.setattr(demo, "_DOCKER_ENDPOINT", None)

    def cli(args, *, env):
        assert args == ["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"]
        for key, value in variables.items():
            assert env[key] == (local_docker_socket if value == "LOCAL" else value)
        return json.dumps(local_docker_socket if selected_local else "ssh://remote.example")

    monkeypatch.setattr(demo, "_run_process", cli)
    if selected_local:
        demo.bind_local_docker()
        assert demo._DOCKER_ENDPOINT == local_docker_socket
    else:
        with pytest.raises(demo.DemoError, match="local Unix"):
            demo.bind_local_docker()
        assert demo._DOCKER_ENDPOINT is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "tcp://127.0.0.1:2375",
        "ssh://remote.example",
        "npipe:////./pipe/docker_engine",
        "unix://remote/var/run/docker.sock",
        "unix:///missing/docker.sock",
        "not a URL",
    ],
)
def test_unproven_daemon_rejected(monkeypatch, endpoint):
    monkeypatch.setattr(demo, "_DOCKER_ENDPOINT", "unix:///previous.sock")
    monkeypatch.setattr(demo, "_run_process", lambda *a, **kw: json.dumps(endpoint))
    with pytest.raises(demo.DemoError, match="local Unix"):
        demo.bind_local_docker()
    assert demo._DOCKER_ENDPOINT is None


@pytest.mark.parametrize("command", ["up", "init", "check", "down", "mcp", "doctor"])
def test_remote_daemon_rejected_before_state_or_any_mutation(monkeypatch, tmp_path, command):
    import demo as controller

    calls = []
    monkeypatch.setattr(demo, "_DOCKER_ENDPOINT", None)
    monkeypatch.setattr(demo, "RUNTIME", tmp_path / "must-not-exist")

    def metadata_only(args, **kw):
        calls.append(args)
        return json.dumps("tcp://192.0.2.1:2375")

    monkeypatch.setattr(demo, "_run_process", metadata_only)

    def unexpected(*a, **kw):
        pytest.fail("Rejected daemon must not load/create state, inspect resources, mutate Docker or bootstrap SQL")

    for name in ("load", "verify_ownership", "compose", "initialize", "run"):
        monkeypatch.setattr(controller, name, unexpected)
    monkeypatch.setattr(sys, "argv", ["demo.py", command])
    with pytest.raises(demo.DemoError, match="local Unix"):
        controller.main()
    assert calls == [["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"]]
    assert not demo.RUNTIME.exists()


def test_all_docker_commands_stay_pinned_after_environment_changes(monkeypatch, local_docker_socket):
    monkeypatch.setattr(demo, "_DOCKER_ENDPOINT", local_docker_socket)
    calls = []

    def capture(args, **kw):
        calls.append((args, kw["env"]))
        return ""

    monkeypatch.setattr(demo, "_run_process", capture)
    overrides = {
        "DOCKER_HOST": "tcp://192.0.2.1:2375",
        "DOCKER_CONTEXT": "remote",
        "DOCKER_TLS_VERIFY": "1",
        "DOCKER_TLS": "1",
        "DOCKER_CERT_PATH": "/remote/certs",
    }
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    demo.run(["docker", "container", "ls"])
    demo.run(["docker", "compose", "up", "-d"], env={**overrides, "DEMO_VALUE": "kept"})
    for args, env in calls:
        assert args[:3] == ["docker", "--host", local_docker_socket]
        assert all(key not in env for key in overrides)
    assert calls[1][1]["DEMO_VALUE"] == "kept"


def test_resource_commands_require_prior_locality_guard(monkeypatch):
    monkeypatch.setattr(demo, "_DOCKER_ENDPOINT", None)
    monkeypatch.setattr(demo, "_run_process", lambda *a, **kw: pytest.fail("No Docker resource access before guard"))
    with pytest.raises(demo.DemoError, match="locality"):
        demo.run(["docker", "container", "rm", "-f", "anything"])
