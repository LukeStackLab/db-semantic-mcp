"""Explicit disposable Oracle implicit-code probe (never collected by pytest).

Requires SEMANTIC_MCP_DISPOSABLE_ORACLE_TESTS=1, an absolute fixed
SEMANTIC_MCP_TEST_ORACLE_PROFILE and DBMCP_ORACLE_ADMIN_PASSWORD for SYSTEM on
that same owned disposable endpoint. Creates one fresh NO AUTHENTICATION schema;
always drops it. Run Thin/Thick in separate processes. Never print credentials.
"""

import json
import os
import re
import secrets
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from db_semantic_mcp.infrastructure.connection import open_connection
from db_semantic_mcp.policy.models import PolicyProfile
from db_semantic_mcp.profiles import load_profile
from db_semantic_mcp.services import query_service
from db_semantic_mcp.services.policy_service import PolicyService


def run():
    assert os.environ.get("SEMANTIC_MCP_DISPOSABLE_ORACLE_TESTS") == "1"
    path = Path(os.environ["SEMANTIC_MCP_TEST_ORACLE_PROFILE"])
    assert path.is_absolute()
    cfg = load_profile(path)
    assert cfg.database_type == "oracle" and re.fullmatch(r"[A-Za-z][A-Za-z0-9_$#]*", cfg.user or "")
    admin = cfg.model_copy(deep=True)
    admin.user = "SYSTEM"
    admin.password = os.environ["DBMCP_ORACLE_ADMIN_PASSWORD"]
    owner = "DBMCP_PROBE_" + secrets.token_hex(6).upper()
    made = False
    outcomes = []
    result = {"driver_mode": cfg.oracle.driver_mode, "cases": outcomes}
    with open_connection(admin) as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(f"CREATE USER {owner} NO AUTHENTICATION DEFAULT TABLESPACE USERS QUOTA 5M ON USERS")
            made = True
            cursor.execute(f"CREATE TABLE {owner}.PROBE_LOG (N NUMBER)")
            cursor.execute(
                f"CREATE TYPE {owner}.PROBE_OBJ AS OBJECT (N NUMBER, MAP MEMBER FUNCTION KEYVAL RETURN NUMBER)"
            )
            cursor.execute(f"""CREATE TYPE BODY {owner}.PROBE_OBJ AS
                MAP MEMBER FUNCTION KEYVAL RETURN NUMBER IS PRAGMA AUTONOMOUS_TRANSACTION;
                BEGIN INSERT INTO {owner}.PROBE_LOG VALUES (SELF.N); COMMIT; RETURN SELF.N; END; END;""")
            cursor.execute(f"""CREATE FUNCTION {owner}.PROBE_FN (P_N NUMBER) RETURN NUMBER DETERMINISTIC IS
                PRAGMA AUTONOMOUS_TRANSACTION; BEGIN INSERT INTO {owner}.PROBE_LOG VALUES (P_N);
                COMMIT; RETURN P_N; END;""")
            cursor.execute(f"CREATE TABLE {owner}.OBJECT_ROWS (ID NUMBER, OBJ {owner}.PROBE_OBJ)")
            cursor.execute(f"CREATE TABLE {owner}.VIRTUAL_ROWS (ID NUMBER, VIRT AS ({owner}.PROBE_FN(ID)))")
            cursor.execute(f"CREATE TABLE {owner}.SCALAR_ROWS (ID NUMBER)")
            for n in (1, 2):
                cursor.execute(f"INSERT INTO {owner}.OBJECT_ROWS VALUES ({n},{owner}.PROBE_OBJ({3 - n}))")
                cursor.execute(f"INSERT INTO {owner}.VIRTUAL_ROWS (ID) VALUES ({n})")
                cursor.execute(f"INSERT INTO {owner}.SCALAR_ROWS VALUES ({n})")
            connection.commit()
            cursor.execute(f"TRUNCATE TABLE {owner}.PROBE_LOG")
            for name in ("OBJECT_ROWS", "VIRTUAL_ROWS", "SCALAR_ROWS"):
                cursor.execute(f"GRANT SELECT ON {owner}.{name} TO {cfg.user}")
            for name in ("PROBE_OBJ", "PROBE_FN"):
                cursor.execute(f"GRANT EXECUTE ON {owner}.{name} TO {cfg.user}")
            cursor.execute(f"CREATE SYNONYM {owner}.ALIAS_ROWS FOR {owner}.OBJECT_ROWS")
            cursor.execute(f"CREATE VIEW {owner}.VIEW_ROWS AS SELECT VIRT FROM {owner}.VIRTUAL_ROWS")
            cursor.execute(f"GRANT SELECT ON {owner}.VIEW_ROWS TO {cfg.user}")
            policy = PolicyService(cfg)
            policy._policy = PolicyProfile(
                profile_name="readonly",
                scope={"allowed_schemas": [owner], "denied_tables": [f"{owner}.PROBE_LOG"]},
            )
            caller_work = []

            class Cursor:
                def __init__(self, wrapped):
                    self.wrapped = wrapped

                def execute(self, sql, *args, **kwargs):
                    if owner in sql:
                        caller_work.append("execute")
                    return self.wrapped.execute(sql, *args, **kwargs)

                def parse(self, sql, *args, **kwargs):
                    caller_work.append("parse")
                    return self.wrapped.parse(sql, *args, **kwargs)

                def __getattr__(self, name):
                    return getattr(self.wrapped, name)

            class Connection:
                def __init__(self, wrapped):
                    object.__setattr__(self, "wrapped", wrapped)

                def cursor(self):
                    return Cursor(self.wrapped.cursor())

                def __getattr__(self, name):
                    return getattr(self.wrapped, name)

                def __setattr__(self, name, value):
                    setattr(self.wrapped, name, value)

            @contextmanager
            def tracked(config):
                with open_connection(config) as wrapped:
                    yield Connection(wrapped)

            with patch.object(query_service, "open_connection", tracked):
                for case, sql in (
                    ("map_order", f"SELECT ID FROM {owner}.OBJECT_ROWS ORDER BY OBJ"),
                    ("virtual_number", f"SELECT VIRT FROM {owner}.VIRTUAL_ROWS"),
                    ("object_star", f"SELECT * FROM {owner}.OBJECT_ROWS"),
                    ("virtual_star", f"SELECT * FROM {owner}.VIRTUAL_ROWS"),
                    ("synonym", f"SELECT ID FROM {owner}.ALIAS_ROWS ORDER BY OBJ"),
                    ("view", f"SELECT VIRT FROM {owner}.VIEW_ROWS"),
                ):
                    response = query_service.QueryService(policy, cfg).execute_query(sql, validate_result_schema=True)
                    cursor.execute(f"SELECT COUNT(*) FROM {owner}.PROBE_LOG")
                    count = int(cursor.fetchone()[0])
                    assert response.get("executed") is False and response["validation"]["allowed"] is False
                    assert count == 0 and caller_work == []
                    outcomes.append(
                        {"case": case, "denied": True, "caller_parse_execute": 0, "autonomous_log_rows": count}
                    )
                response = query_service.QueryService(policy, cfg).execute_query(
                    f"SELECT ID FROM {owner}.SCALAR_ROWS ORDER BY ID", validate_result_schema=True
                )
                assert response.get("executed") and response["row_count"] == 2
                result["physical_scalar_read"] = True
        finally:
            if made:
                cursor.execute(f"DROP USER {owner} CASCADE")
                cursor.execute("SELECT COUNT(*) FROM sys.all_users WHERE username=:name", {"name": owner})
                result["fixture_schema_removed"] = cursor.fetchone()[0] == 0
            cursor.close()
    assert result.get("fixture_schema_removed")
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(run()))
    except Exception as exc:  # noqa: BLE001 -- driver errors may contain credentials
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
