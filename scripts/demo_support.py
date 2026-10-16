"""Private state and strict Docker ownership for the local demo."""

import json
import os
import secrets
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".db-semantic-mcp-demo"
PROJECT = "db-semantic-demo"
LABEL = "io.db-semantic-mcp.demo-owner"
SERVICES = {"sql": 21433, "oracle": 21521, "remote": 21522}
_DOCKER_ENDPOINT = None


class DemoError(RuntimeError):
    pass


def _run_process(args, *, env=None, input=None, timeout=120):
    try:
        result = subprocess.run(args, env=env, input=input, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise DemoError(f"{args[0]} unavailable or timed out; no credentials were printed") from None
    if result.returncode:
        raise DemoError(
            f"{args[0]} operation failed (exit {result.returncode}); output withheld to protect credentials"
        )
    return result.stdout


def bind_local_docker():
    """Resolve with the installed CLI's precedence, then pin a local Unix socket."""
    global _DOCKER_ENDPOINT
    _DOCKER_ENDPOINT = None
    # Context metadata is local; no daemon access, state creation or mutation.
    output = _run_process(
        ["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"],
        env=dict(os.environ),
    )
    try:
        endpoint = urlsplit(json.loads(output))
        path = Path(endpoint.path)
        if (
            endpoint.scheme != "unix"
            or endpoint.netloc
            or endpoint.query
            or endpoint.fragment
            or not path.is_absolute()
            or not path.is_socket()
        ):
            raise ValueError
        # Resolve a Desktop socket symlink now; later context/env changes cannot reroute it.
        _DOCKER_ENDPOINT = "unix://" + str(path.resolve(strict=True))
    except (ValueError, TypeError, AttributeError, OSError):
        raise DemoError(
            "Local demo requires an existing local Unix Docker socket; remote, TCP and npipe endpoints are unsupported"
        ) from None


def run(args, *, env=None, input=None, timeout=120):
    if args[0] == "docker":
        if _DOCKER_ENDPOINT is None:
            raise DemoError("Docker locality must be verified before resource operations")
        # --host pins both Docker and Compose independently of currentContext.
        env = dict(os.environ if env is None else env)
        for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
            env.pop(key, None)
        args = ["docker", "--host", _DOCKER_ENDPOINT, *args[1:]]
    return _run_process(args, env=env, input=input, timeout=timeout)


def private_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise DemoError("Refusing symlink in private demo state")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(value)
    path.chmod(0o600)


def save(state):
    private_write(RUNTIME / "state.json", json.dumps(state, indent=2) + "\n")


def load(create=False):
    if RUNTIME.is_symlink():
        raise DemoError("Refusing symlink demo directory")
    path = RUNTIME / "state.json"
    if path.is_symlink() or (RUNTIME / ".env").is_symlink():
        raise DemoError("Refusing symlink private state or credentials")
    if path.exists():
        state = json.loads(path.read_text())
        if not isinstance(state.get("token"), str) or len(state["token"]) != 48:
            raise DemoError("Invalid demo ownership state")
        if environment().get("DEMO_OWNER_TOKEN") != state["token"]:
            raise DemoError("Private credential ownership token mismatch")
        return state
    if not create:
        raise DemoError("No demo state; run up first")
    if RUNTIME.exists() and any(RUNTIME.iterdir()):
        raise DemoError("Refusing nonempty demo directory without ownership state")
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    RUNTIME.chmod(0o700)
    state = {"token": secrets.token_hex(24), "initialized": [], "remote": False}
    values = {
        key: "Demo" + secrets.token_hex(20) + "a!"
        for key in ("SQL_ADMIN_PASSWORD", "ORACLE_ADMIN_PASSWORD", "DEMO_SQL_PASSWORD", "DEMO_ORACLE_PASSWORD")
    }
    values["DEMO_OWNER_TOKEN"] = state["token"]
    private_write(RUNTIME / ".env", "".join(f"{key}={value}\n" for key, value in values.items()))
    save(state)
    return state


def environment():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SEMANTIC_MCP_", "COMPOSE_"))}
    env.update(dict(line.split("=", 1) for line in (RUNTIME / ".env").read_text().splitlines()))
    return env


def inspect(kind, name):
    # List first: a Docker daemon failure must never be interpreted as absence.
    names = run(
        [
            "docker",
            kind,
            "ls",
            *(["-a"] if kind == "container" else []),
            "--format",
            "{{.Names}}" if kind == "container" else "{{.Name}}",
        ]
    ).splitlines()
    if name not in names:
        return None
    return json.loads(run(["docker", kind, "inspect", name]))[0]


def resources():
    return (
        [("container", f"{PROJECT}-{s}") for s in SERVICES]
        + [("volume", f"{PROJECT}-{s}-data") for s in SERVICES]
        + [("network", f"{PROJECT}-network")]
    )


def verify_ownership(state, *, ports=False):
    found = {}
    for kind, name in resources():
        info = inspect(kind, name)
        if info is not None:
            labels = info.get("Config", {}).get("Labels", {}) if kind == "container" else info.get("Labels", {})
            if labels.get(LABEL) != state["token"] or labels.get("com.docker.compose.project") != PROJECT:
                raise DemoError(f"Refusing unowned {kind}: {name}")
            found[name] = info
    # Do not permit Compose to discover additional project resources.
    expected = {name for _, name in resources()}
    for kind in ("container", "volume", "network"):
        args = [
            "docker",
            kind,
            "ls",
            *(["-a"] if kind == "container" else []),
            "--filter",
            f"label=com.docker.compose.project={PROJECT}",
            "--format",
            "{{.Names}}" if kind == "container" else "{{.Name}}",
        ]
        if any(name not in expected for name in run(args).splitlines()):
            raise DemoError("Unexpected resource in demo Compose project")
    if ports:
        for service in ("sql", "oracle", *(["remote"] if state["remote"] else [])):
            info = found.get(f"{PROJECT}-{service}")
            if info and info.get("State", {}).get("Running"):
                continue
            with socket.socket() as sock:
                try:
                    sock.bind(("127.0.0.1", SERVICES[service]))
                except OSError:
                    raise DemoError(f"Port {SERVICES[service]} is occupied; refusing collision") from None
    return found


def compose(state, *args):
    return run(
        [
            "docker",
            "compose",
            "--project-name",
            PROJECT,
            "--env-file",
            str(RUNTIME / ".env"),
            "-f",
            str(ROOT / "docker/compose.demo.yaml"),
            *(["--profile", "remote"] if state["remote"] else []),
            *args,
        ],
        env=environment(),
        timeout=700,
    )


def verify_ready(state, found):
    """A stored token alone never authorizes bootstrap against an unknown endpoint."""
    for service in ("sql", "oracle", *(["remote"] if state["remote"] else [])):
        info = found.get(f"{PROJECT}-{service}", {})
        if not info.get("State", {}).get("Running"):
            raise DemoError(f"Owned {service} container is not running; use up")
        internal = "1433/tcp" if service == "sql" else "1521/tcp"
        bindings = info.get("NetworkSettings", {}).get("Ports", {}).get(internal)
        if bindings != [{"HostIp": "127.0.0.1", "HostPort": str(SERVICES[service])}]:
            raise DemoError(f"Unexpected {service} endpoint; refusing database access")
        destination = "/var/opt/mssql" if service == "sql" else "/opt/oracle/oradata"
        if not any(
            m.get("Type") == "volume"
            and m.get("Name") == f"{PROJECT}-{service}-data"
            and m.get("Destination") == destination
            for m in info.get("Mounts", [])
        ):
            raise DemoError(f"Unexpected {service} data mount; refusing database access")
