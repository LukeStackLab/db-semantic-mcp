#!/usr/bin/env python3
"""Manage only the isolated, token-owned local Docker demo."""

import argparse
import os
import sys

from demo_fixtures import initialize
from demo_support import (
    PROJECT,
    RUNTIME,
    DemoError,
    compose,
    environment,
    load,
    resources,
    run,
    save,
    verify_ownership,
    verify_ready,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["up", "init", "check", "down", "mcp", "doctor"])
    parser.add_argument("--remote", action="store_true", help="add a second Oracle for DB link tests")
    parser.add_argument("--purge", action="store_true", help="with down: explicitly delete owned database volumes")
    parser.add_argument("--provider", choices=["sql", "oracle", "oracle-remote"], default="oracle")
    args = parser.parse_args()
    if args.purge and args.command != "down":
        parser.error("--purge requires down")
    if args.remote and args.command != "up":
        parser.error("--remote requires up")
    state = load(create=args.command == "up")
    if args.remote:
        state["remote"] = True
    owned = verify_ownership(state, ports=args.command == "up")
    for service in ("sql", "oracle", "remote"):
        if args.command != "down" and service in state["initialized"] and f"{PROJECT}-{service}-data" not in owned:
            raise DemoError("Initialized data volume is missing; refusing automatic recreation")
    if args.command not in ("up", "down"):
        verify_ready(state, owned)
    if args.command == "up":
        save(state)
        compose(state, "up", "-d", "--wait", "--wait-timeout", "600")
        verify_ready(state, verify_ownership(state))
        initialize(state)
        print(
            "Demo ready: SQL 127.0.0.1:21433; Oracle 127.0.0.1:21521"
            + ("; remote Oracle 127.0.0.1:21522" if state["remote"] else "")
        )
    elif args.command == "init":
        initialize(state)
        print("Fixtures ready; existing completed data preserved")
    elif args.command == "check":
        from demo_checks import check

        check(state)
    elif args.command in ("mcp", "doctor"):
        path = RUNTIME / args.provider / "connection.json"
        if not path.is_file():
            raise DemoError("Requested profile is not initialized")
        command = [
            sys.executable,
            "-m",
            "db_semantic_mcp.main",
            "doctor" if args.command == "doctor" else "serve",
            "--config",
            str(path),
        ]
        os.execve(sys.executable, command, environment())
    elif args.purge:
        # Exact verified resources only; never use global prune or broad Compose down.
        for kind, name in resources():
            if name in owned:
                run(["docker", kind, "rm", *(["-f"] if kind == "container" else []), name])
        state["initialized"] = []
        state["remote"] = False
        # Keep credentials and user's profile edits; remove only generated profiles
        # by archiving them before a future up creates fresh ones.
        for name in ("sql", "oracle", "oracle-remote"):
            path = RUNTIME / name
            if path.exists():
                import time

                path.rename(RUNTIME / f"{name}-purged-{time.time_ns()}")
        save(state)
        print("Owned demo containers, network and database volumes removed; private profiles archived")
    else:
        compose(state, "stop", "--timeout", "30")
        print("Demo stopped; database volumes, credentials and profiles preserved")


if __name__ == "__main__":
    try:
        main()
    except DemoError as exc:
        print(f"Demo: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception:  # noqa: BLE001 -- DB/bootstrap exceptions may contain passwords
        print(
            "Demo operation failed; details withheld to protect credentials. Existing data was not reset.",
            file=sys.stderr,
        )
        sys.exit(1)
