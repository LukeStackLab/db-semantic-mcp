"""One-time admin bootstrap; product MCP remains read-only."""

import json

from demo_support import PROJECT, ROOT, RUNTIME, SERVICES, DemoError, environment, private_write, run, save

ORACLE_FIXTURES = ROOT / "tests/integration/fixtures/oracle"


def sqlplus(service, user, password, sql):
    # SQLPlus connects through stdin; neither secrets nor SQL appear on argv/logs.
    script = (
        f'SET ECHO OFF\nSET VERIFY OFF\nWHENEVER SQLERROR EXIT FAILURE\nCONNECT {user}/"{password}"@//localhost:1521/FREEPDB1\n'
        + sql
        + "\nEXIT\n"
    )
    output = run(["docker", "exec", "-i", f"{PROJECT}-{service}", "sqlplus", "-s", "/nolog"], input=script)
    if "ORA-" in output or "SP2-" in output:
        raise DemoError(f"Oracle {service} bootstrap failed; output withheld to protect credentials")


def initialize_sql(env):
    import pymssql

    with pymssql.connect(
        server="127.0.0.1",
        port=SERVICES["sql"],
        user="sa",
        password=env["SQL_ADMIN_PASSWORD"],
        database="master",
        login_timeout=15,
        timeout=30,
        autocommit=True,
    ) as conn:
        cur = conn.cursor()
        cur.execute("SELECT DB_ID('DBSemanticDemo')")
        if cur.fetchone()[0] is not None:
            raise DemoError("Existing SQL demo database without completion marker; refusing overwrite")
        cur.execute("CREATE DATABASE DBSemanticDemo")
        cur.execute("USE DBSemanticDemo")
        for batch in (ROOT / "docker/sqlserver-fixture.sql").read_text().split("\nGO\n"):
            if batch.strip():
                cur.execute(batch)
        cur.execute("CREATE LOGIN dbmcp_reader WITH PASSWORD = '{}'".format(env["DEMO_SQL_PASSWORD"]))
        cur.execute("CREATE USER dbmcp_reader FOR LOGIN dbmcp_reader")
        cur.execute("GRANT SELECT, VIEW DEFINITION TO dbmcp_reader")


def initialize_oracle(service, env):
    password = env["DEMO_ORACLE_PASSWORD"]
    # CREATE USER fails closed on partial/foreign initialization, never drops data.
    sqlplus(
        service,
        "system",
        env["ORACLE_ADMIN_PASSWORD"],
        f'CREATE USER DBMCP_TEST IDENTIFIED BY "{password}" DEFAULT TABLESPACE USERS QUOTA 20M ON USERS;\nGRANT CREATE SESSION,CREATE TABLE,CREATE VIEW,CREATE PROCEDURE,CREATE SYNONYM,CREATE DATABASE LINK TO DBMCP_TEST;',
    )
    sqlplus(service, "DBMCP_TEST", password, (ORACLE_FIXTURES / "common.sql").read_text())
    if service == "remote":
        sqlplus(
            service, "system", env["ORACLE_ADMIN_PASSWORD"], (ORACLE_FIXTURES / "remote-only-owner.sql").read_text()
        )


def initialize_links(env):
    text = (ORACLE_FIXTURES / "local-links.sql").read_text()
    text = "\n".join(line for line in text.splitlines() if not line.startswith(("ACCEPT ", "UNDEFINE ")))
    text = text.replace("&remote_password", env["DEMO_ORACLE_PASSWORD"]).replace(
        "&remote_endpoint", "remote:1521/FREEPDB1"
    )
    sqlplus("oracle", "DBMCP_TEST", env["DEMO_ORACLE_PASSWORD"], text)
    sqlplus("oracle", "system", env["ORACLE_ADMIN_PASSWORD"], (ORACLE_FIXTURES / "local-public.sql").read_text())


def write_profile(name, remote=False):
    from db_semantic_mcp.profiles import ConnectionProfile, policy_bytes, profile_bytes

    oracle = name.startswith("oracle")
    profile = ConnectionProfile(
        name="demo_" + name.replace("-", "_"),
        database_type="oracle" if oracle else "sqlserver",
        server="127.0.0.1",
        port=SERVICES["oracle" if oracle else "sql"],
        database="FREEPDB1" if oracle else "DBSemanticDemo",
        user="DBMCP_TEST" if oracle else "dbmcp_reader",
        encrypt=False,
        readonly=True,
        max_cell_chars=64,
        oracle={"service_name": "FREEPDB1", "driver_mode": "thin"} if oracle else None,
        sources={"remote": {"db_link": "DBMCP.REMOTE.TEST", "readonly": True}} if remote else {},
        credential={"storage": "env", "key": "DEMO_ORACLE_PASSWORD" if oracle else "DEMO_SQL_PASSWORD"},
    )
    policy = json.loads(policy_bytes(profile))
    scope = policy["profiles"]["readonly"]["scope"]
    scope["allowed_schemas"] = ["DBMCP_TEST", "PUBLIC"] if oracle else ["dbo"]
    if remote:
        scope["sources"] = {
            "remote": {"allowed_schemas": ["DBMCP_TEST", "DBMCP_REMOTE_ONLY"], "operations": {"select": True}}
        }
    directory = RUNTIME / name
    if directory.exists():
        raise DemoError(f"Existing {name} profile without completion marker; refusing overwrite")
    private_write(directory / "connection.json", profile_bytes(profile).decode())
    private_write(directory / "policy.json", json.dumps(policy, indent=2) + "\n")


def initialize(state):
    env = environment()
    for service in ("sql", "oracle", *(["remote"] if state["remote"] else [])):
        if service in state["initialized"]:
            continue
        if service == "sql":
            initialize_sql(env)
        else:
            initialize_oracle(service, env)
        state["initialized"].append(service)
        save(state)
    if state["remote"] and "links" not in state["initialized"]:
        initialize_links(env)
        state["initialized"].append("links")
        save(state)
    for name in ("sql", "oracle", *(["oracle-remote"] if state["remote"] else [])):
        marker = "profile-" + name
        if marker not in state["initialized"]:
            write_profile(name, remote=name == "oracle-remote")
            state["initialized"].append(marker)
            save(state)
