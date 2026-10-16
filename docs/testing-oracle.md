# Disposable SQL Server / Oracle live tests

The normal suite never selects a database merely because `SEMANTIC_MCP_SERVER` exists. Live tests require explicit disposable opt-in and an **absolute fixed v2 profile path**. Profile loading ignores generic DB environment overrides and cwd `.env`. Keep profiles, credential values, wallets and Oracle Client libraries outside Git. Use a dedicated disposable database; the product never provisions links or users.

## Prepare two Oracle databases

Install development dependencies with `uv sync --locked --extra dev --extra oracle`. Use two independent Oracle 19c+ databases/PDBs reachable from each other. The supported-version target is 19c+; see [actual tested versions](validation-0.8.0.md).

1. On each disposable PDB, a DBA creates `DBMCP_TEST` with CREATE SESSION, CREATE TABLE, CREATE VIEW, CREATE PROCEDURE, CREATE SYNONYM, CREATE DATABASE LINK and bounded tablespace quota. Choose credentials privately. This account is for fixtures only.
2. As DBMCP_TEST, run [common.sql](../tests/integration/fixtures/oracle/common.sql) once on **both** databases. It creates two rows, a compound FK, comments, NUMBER(19,4), CLOB/BLOB, TZ data, synonyms, view and a package.
3. On the local DBMCP_TEST connection, run [local-links.sql](../tests/integration/fixtures/oracle/local-links.sql). It prompts for a remote endpoint and hidden remote password. Disable SQL session spooling. The endpoint must be reachable from the local **database server**, which can differ from the test host. Oracle restricts DB link creation according to DBA policy.
4. Optional PUBLIC remote-only discovery fixture: DBA runs [remote-only-owner.sql](../tests/integration/fixtures/oracle/remote-only-owner.sql) on the remote PDB and [local-public.sql](../tests/integration/fixtures/oracle/local-public.sql) on the local PDB. No extra password is created for the NO AUTHENTICATION owner.
5. Copy [Oracle connection](../config/connection.oracle.example.json) and [policy](../config/policy.oracle.example.json) into a private directory as connection.json/policy.json. Update endpoint/service/login and credential env reference. Policy must authorize DBMCP_TEST/PUBLIC locally, and DBMCP_TEST/DBMCP_REMOTE_ONLY remotely. Use the actual catalog link name. The route alone grants nothing.

Schema names and object names in these tests are intentionally fixed. Provision only disposable nodes. The SQL scripts are one-time setup and fail if objects already exist; do not use them as a migration. The suite does not drop schemas/links or modify real user client configuration/keyring. Tear down the owned disposable infrastructure afterward.

## Read/catalog/native-type/LOB/MCP matrix

Set the password environment variable privately, using the name in `credential.key`. Do not put its value in shell commands saved to shared history. Then:

```bash
export SEMANTIC_MCP_DISPOSABLE_ORACLE_TESTS=1
export SEMANTIC_MCP_TEST_ORACLE_PROFILE=/absolute/private/thin/connection.json
uv run --locked --extra dev --extra oracle pytest -q tests/integration/test_live_oracle.py
```

Use a separate profile with `oracle.driver_mode="thick"`, installed official matching Instant Client and `client_lib_dir`/`config_dir` as needed. Run the same command in a **fresh Python process** with its profile path. Never initialize Thin and Thick in one process.

The suite checks product QueryService precision/result metadata, bounded LOB shaping, catalog tuple/FK evidence, synonym SQL, native owner.table@link multi-source SQL, Thin temporal rejection/Thick text, and an actual stdio MCP child running get_tables for each configured source, plan_or_execute_query, describe_object and get_execution_policy. The MCP child uses fixed profile arguments and explicit credential environment references. It does not merely test raw driver connectivity.

Remote cases require a configured source named `remote` and its matching policy grant. Set `SEMANTIC_MCP_ORACLE_PUBLIC_FIXTURE=1` to assert PUBLIC remote-only discovery when the optional fixture is installed. Missing explicit live opt-in skips the whole Oracle matrix. Missing remote configuration skips only dependent cases. Read-only runs skip the two write cases.

## Separate write/rollback opt-in

Only on the two disposable schemas with at least two fixture rows, make a second profile with `readonly=false`, `max_rows_affected=1`, remote route `readonly=false`, active policy `read_write_safe`, and SELECT/INSERT/UPDATE enabled locally and in `scope.sources.remote.operations`. Preserve allowed schemas and WHERE requirement. Then:

```bash
export SEMANTIC_MCP_DISPOSABLE_ORACLE_WRITES=1
export SEMANTIC_MCP_TEST_ORACLE_PROFILE=/absolute/private/writable/connection.json
uv run --locked --extra dev --extra oracle pytest -q tests/integration/test_live_oracle.py
```

The tests UPDATE both fixture rows to exceed cap one, require `transaction_state=rolled_back`, and compare before/after values for both local and DB link writes. They do not commit arbitrary production changes. Missing write opt-in skips these cases even if a writable profile is provided. Unknown acknowledgement failure injection is covered by contract tests; a real in-doubt distributed failure is not induced by this suite.

## SQL Server compatibility

Prepare a disposable SQL Server and fixed SQL Server v2 profile/policy using the [example](../config/connection.sqlserver.example.json). Set its credential reference privately:

```bash
export SEMANTIC_MCP_DISPOSABLE_SQL_TESTS=1
export SEMANTIC_MCP_TEST_SQL_PROFILE=/absolute/private/sqlserver/connection.json
uv run --locked --extra dev pytest -q tests/integration/test_live_contracts.py tests/integration/test_live_warmup.py
```

These cases check exact decimal/result description, catalog tuple formats and structural cache warmup. No writes are performed. A profile selecting Oracle is rejected even when unrelated Oracle runtime environment variables are inherited.

## TLS and reproducibility boundaries

For TCPS, use a trusted server certificate and the correct listener port. Thin needs an appropriate wallet and optional `wallet_password_env` reference. Thick uses Oracle Net trust under `config_dir`; wallets/certificates/keys must remain private. Use `encrypt=true`, then run `db-semantic-mcp doctor --config ABSOLUTE_PROFILE` and the same product suite against that profile. Doctor must report `transport="tcps"`; a configured wallet alone does not prove TLS. DSN/TNS mixed TCP/TCPS, encrypt mismatch and wrong DN must fail, without disabling DN matching. DB link transport remains managed by the database.

Native Thin TIMESTAMP(p>6)/TZ/LTZ is deliberately rejected with TO_CHAR guidance. Thick preserves temporal text rather than lossy Python datetime. Oracle call_timeout is per round trip. Client cancellation cannot abort an already running synchronous operation. A write may finish after disconnect; verify uncertain outcomes before manual retry. Neither cancellation nor timeout guarantees rollback.

Release proof and exact counts are recorded in [validation-0.8.0.md](validation-0.8.0.md). Mock coverage is distinct from real server evidence. Oracle 19c exact-server certification, LDAP current-user links and real distributed in-doubt recovery remain outside the demonstrated matrix.

For isolated wheel CLI/doctor/actual-MCP smoke without pytest, use the committed [check_installed.py](../tests/integration/check_installed.py) commands in the [validation evidence](validation-0.8.0.md). The smoke uses the same disposable opt-in and profile path requirements.
