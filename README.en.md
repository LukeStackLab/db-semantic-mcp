# db-semantic-mcp

MCP tools for SQL Server and Oracle: discover objects, inspect relationships and run SQL within explicit permissions. Read-only by default. Supports Codex, Claude Code, Claude Desktop and Cursor.

[繁體中文](README.zh-TW.md) · [English](README.en.md) · [Oracle live testing](docs/testing-oracle.md) · [Validation evidence](docs/validation-0.8.0.md)

Compatibility targets: SQL Server 2017+, Oracle 19c+, Python 3.11+. The actual live matrix uses SQL Server 2022 and Oracle 23.26.3, with Oracle Thin/Thick and TCP/TCPS. Exact Oracle 19c and LDAP current-user links have not been live certified.

## Install 0.8.0 from source or a built wheel

Product/CLI/MCP name: `db-semantic-mcp`. Python module: `db_semantic_mcp`. Distribution: **`lukestack-db-semantic-mcp`**. This release has not been published to PyPI. The generic PyPI name belongs to another project. The GitHub repository keeps its current URL.

```bash
git clone https://github.com/LukeStackLab/db-semantic-mcp.git db-semantic-mcp
cd db-semantic-mcp
# SQL Server only; Oracle driver/client are unnecessary
uv tool install --python 3.11 --reinstall .
# Oracle support (also includes SQL Server)
uv tool install --python 3.11 --reinstall '.[oracle]'
db-semantic-mcp --version
db-semantic-mcp --help
```

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first. If uv reports a PATH issue, run `uv tool update-shell` and reopen the terminal. For a local build:

```bash
uv build
uv tool install --reinstall './dist/lukestack_db_semantic_mcp-0.8.0-py3-none-any.whl[oracle]'
```

Thin is the default and needs no Oracle Client. Thick requires matching official Oracle Client libraries, explicit `driver_mode: "thick"`, and `client_lib_dir` when needed. Mode is process-wide: use a fresh MCP process for each mode. See [official driver installation](https://python-oracledb.readthedocs.io/en/stable/user_guide/installation.html) and [initialization](https://python-oracledb.readthedocs.io/en/stable/user_guide/initialization.html).

## Setup and fixed profiles

```bash
db-semantic-mcp setup
# Optional smaller tool surface
db-semantic-mcp setup --tool-profile explore
```

Prompts are currently Traditional Chinese. Select the client and database provider, name the connection, enter the endpoint and credentials, choose read-only or limited writes, then confirm the concrete summary. Oracle prompts cover Thin/Thick, service/SID/TNS, TLS and explicitly authorized existing DB links. The wizard checks metadata and MCP initialization without testing writes. It backs up client files before merging entries. For Claude Code, run it in the intended project. Reload MCP settings after setup.

Each connection gets a separate process, credential reference, policy and cache. Use distinct names such as `erp_prod`, `oracle_test` and `reporting`. Ask the agent to use the corresponding MCP server. Profiles ignore inherited `SEMANTIC_MCP_*` values and cwd `.env`.

Manual v2 examples:

- [SQL Server connection](config/connection.sqlserver.example.json) with [policy](config/policy.example.json).
- [Oracle connection](config/connection.oracle.example.json) with [Oracle source policy](config/policy.oracle.example.json).

Copy the selected connection to a private directory as `connection.json`, and its matching policy as `policy.json`. Set the password variable named by `credential.key` privately. Do not put its value in JSON or client settings. A profile's `readonly`, policy profile name and allowed operations must agree.

```bash
db-semantic-mcp serve --config /absolute/private/connection.json
db-semantic-mcp doctor --config /absolute/private/connection.json
```

Client entries use the installed Python executable with `-I -m db_semantic_mcp.main serve --config ABSOLUTE_PATH`. Moving that environment requires reinstall/setup. Doctor reports SQL Server database/visible-table count, or Oracle server version, driver mode, actual local transport, configured source availability and full capabilities. Both include the MCP tool count; neither reports the driver package version. It never probes write permission by modifying data.

Default profile roots:

| Platform | Root |
|---|---|
| macOS | `~/Library/Application Support/db-semantic-mcp/connections/` |
| Windows | `%APPDATA%\db-semantic-mcp\connections\` |
| Linux | `$XDG_CONFIG_HOME/db-semantic-mcp/connections/`, default `~/.config/db-semantic-mcp/connections/` |

Passwords use the system keyring. On macOS/Linux, explicit consent can enable a private plaintext fallback (directory `0700`, file `0600`). Windows uses keyring. Environment credential references are available for manual profiles. Existing environment startup remains available; see [.env.example](.env.example).

## Oracle connection and source rules

Choose exactly one `oracle.service_name`, `oracle.sid` or `oracle.dsn`. Service/SID also require `server` and `port`. TNS aliases require a suitable `config_dir`; never embed credentials in DSN. `database` identifies the connection/cache, while the Oracle option selects its service or SID.

For direct connections, `encrypt=false` means TCP and `encrypt=true` means TCPS. Specify the correct listener port explicitly. Thin TCPS uses `wallet_location` and an optional `wallet_password_env` reference. Thick trust lives in `sqlnet.ora` under `config_dir`. Server DN matching is mandatory; `ssl_server_cert_dn` optionally fixes the expected DN. DSN/TNS addresses must all match `encrypt`; mixed protocols and downgrade attempts fail. Doctor verifies the actual local `NETWORK_PROTOCOL`. DB link transport is database-managed and is not certified as end-to-end TLS.

`connection.json.sources` defines routes, **not authorization**. An Oracle remote needs a matching `policy.scope.sources` grant with allowed schemas and operations. Unlisted links fail closed. A readonly route cannot write even if policy allows it. The product uses existing links; it never creates links or grants.

Exploration tools accept optional `source`, default `local`. For example, `get_tables(source="remote")` discovers that source through the base login. `plan_or_execute_query` accepts caller SQL and does not add links or translate dialects:

```sql
SELECT AMOUNT FROM DBMCP_TEST.DBMCP_ORDERS@DBMCP.REMOTE.TEST;
SELECT AMOUNT FROM DBMCP_TEST.DBMCP_REMOTE_CHAIN;
```

Use an explicit remote owner when the current schema cannot be proved. Private/public and chained synonyms preserve their exposed name, resolution chain and canonical target. Every path and target must be authorized. Public synonyms can point to an authorized remote-only owner. Cycles, unknown links, unavailable routes and unproved targets are rejected. Every startup binds its cache namespace before cache-first lookup, including environment startup. Namespaces isolate provider, endpoint/service, principal, effective policy, driver options and source/route. The configured cache path remains the root; identical identities reuse a stable path, clones do not nest hashes, and policy reload selects a new namespace. Old cache files are retained. Each request retains one policy snapshot during concurrent reload.

Oracle inventory defaults to application schemas (`ORACLE_MAINTAINED=N`); explicit `allowed_schemas` can include system schemas. Catalog includes columns, precision/scale/length semantics, comments, PK, ordered compound FK, indexes, views, synonyms and static dependencies. PL/SQL PACKAGE/PACKAGE BODY/PROCEDURE/FUNCTION definitions are withheld even with unrestricted local scope. Authorized metadata/static dependencies remain available with IO completeness unknown. No complete dynamic SQL or PL/SQL control-flow analysis is claimed. VIEW definitions require strict reference/function/column proof; unproved MATERIALIZED VIEW definitions are withheld.

## SQL execution and limits

Both providers default to `SELECT`. Explicit limited writes allow a single `INSERT ... VALUES` or `UPDATE ... WHERE`. DELETE, DDL, procedure/PLSQL execution, transaction control, opaque sources and unsafe batches are refused. SQL Server scalar DECLARE/SET before one SELECT is supported within strict restrictions; Oracle uses native Oracle SQL. No dialect translation occurs. Oracle also rejects SELECT FOR UPDATE, sequences, remote/custom functions and unknown possibly side-effecting functions. Policies do not grant database permissions.

Before caller parse/execute, same-session SYS catalog evidence must prove scalar physical columns and supported access paths. An entire target is refused if any column (including hidden columns) is a named type, virtual column or unknown type, or if it has VPD policies, external/object-table storage or domain/function-based/unknown indexes. This also applies through synonyms, views and remote links, even when the query projects only another safe column or uses `*`, filters, joins, grouping or sorting. Ordinary physical scalar tables and approved builtins remain supported. Views additionally require live definition/dependency proof. Materialized views require live `ON_QUERY_COMPUTATION=N`; Y, unknown or missing evidence is conservatively denied. This does not imply every direct real-time materialized-view SELECT invokes custom code. Missing catalog evidence fails closed; no PL/SQL expression parser or bypass is provided.

| Limit | Default | Setting |
|---|---:|---|
| Returned rows | 1000 | `max_rows_returned`; per-call `max_rows` can lower it |
| JSON columns + retained rows | 65536 bytes | `max_response_bytes` |
| Cell text / binary hex | 2000 characters | `max_cell_chars` |
| List items | 500 | `max_list_items` / tool `limit` |
| Affected rows | 100 | `max_rows_affected` |
| Timeout | 30 seconds | `query_timeout` |

Cell truncation retains up to max_cell_chars content characters and adds a visible truncation marker. Response envelopes are additional to the row byte cap. Too-wide headers retain the truthful `executed` state and return `columns_omitted=true`, `truncated=true`, `result_complete=false` without an oversized column list. Check completeness flags even when execution succeeded. Truncation is flagged with its reason; retained `row_count` differs from `total_row_count`, which is null when the result was not exhausted. Narrow SQL with WHERE, fewer columns, TOP/FETCH or aggregates. Oracle LOB reads are bounded while the connection remains open; no full-read fallback. Remote LOB support depends on driver/server.

Oracle NUMBER stays Decimal, never float. Thin accepts only proven TIMESTAMP precision 0..6; native higher/unknown precision, TZ and LTZ are rejected with an explicit `TO_CHAR` workaround. Thick preserves FF9 and timezone text; LTZ is rendered in UTC because its original zone is not stored. For explicit text, use `TO_CHAR(ts, 'YYYY-MM-DD HH24:MI:SS.FF9')` or `TO_CHAR(tz, 'YYYY-MM-DD HH24:MI:SS.FF9 TZR TZD')`. Thin LTZ text uses the login session timezone. SQLGlot analyzes the original SQL without rewriting it.

Oracle `query_timeout` is `call_timeout` per network round trip, not a total query deadline. `connect_timeout` is per-address TCP establishment, not a total login deadline. Cancelling/disconnecting an MCP caller does not interrupt a running synchronous driver; workers retain capacity until cleanup, and a write may finish after cancellation. Never assume cancellation means rollback.

Fixed profiles and every Oracle configuration always roll back affected-row cap breaches, including an Oracle caller requesting report mode. Non-fixed SQL Server environment/policy configurations use report behavior by default; enable `strict_rows_affected_cap` or `affected_rows_policy="strict"` for rollback. Autocommit is off. Unknown commit/rollback acknowledgement returns `transaction_state="unknown"`, `automatic_retry_allowed=false`; `executed=false` does not prove rollback. Verify outcome before manual retry after an unknown result, timeout or client cancellation. Distributed writes use the same rule; no forced transaction resolution or automatic retry.

`validate_result_schema=true` adds authorized SELECT metadata preflight; this is optional and does not prove business correctness. Oracle preflight never parses unproved DDL. Denials include structured rule/message/hint. Restricted definitions carry omission/completeness flags.

## Tools, cache and diagnostics

The consolidated surface has 12 tools: get_tables, describe_table, describe_object, get_table_relationships, get_dependency_chain, detect_lookup_tables, find_join_path, discover_relevant_tables, plan_or_execute_query, get_execution_policy, refresh_schema_cache and tool_metrics. Profiles: `all` 12, `query` 2, `explore` 8, `maintenance` 3. Selection changes the surface, not authorization. Resources support `?source=CONFIGURED_ID`.

Responses include compact provider/source/database provenance and precision/driver flags. Call `get_execution_policy` for full capability descriptions, timeout semantics, inventory scope and temporal workarounds; doctor also includes full capabilities. No summary/sample/detail modes or token budgets are implemented. Version 2 JSON rejects the retired `budget` field; `migrate` removes it from version 1 profiles. Only unversioned in-process profile construction discards it for compatibility. Output is bounded and structuredContent/outputSchema validated.

Metadata, resources, dependencies and queries share scope. Compound FK ordering and identity are retained. Cardinality stays unknown without valid uniqueness evidence. Discovery uses local metadata and optional `discovery_aliases`, without external models. Cache snapshots include comments/constraints/index changes; default TTL is 300 seconds with single-flight background revalidation. Remote cache warmup is on demand. Offline cached content is explicitly stale. Snapshot identity refers to returned content, not a concurrent newer refresh. Default DB worker limit is 4. `tool_metrics` reports bytes, latency, worker waits and failure categories without storing SQL, rows or passwords.

## Migrate an older SQL Server installation

Old CLI/module aliases are removed. Preview first; preview reads no credential and writes nothing:

```bash
db-semantic-mcp migrate --legacy-root /absolute/old/connections --config-dir /absolute/new/connections --dry-run
db-semantic-mcp migrate --legacy-root /absolute/old/connections --config-dir /absolute/new/connections --apply
```

Optional `--client CLIENT --client-config PATH` or repeated `--client-path CLIENT=PATH` selects client files. Apply copies credentials to new references, preserves names/scope/stricter caps, creates v2 profiles and validates MCP. Only positively identified old-owned client entries are rewritten. Originals and backups remain; concurrent edits are preserved. Old cache is not migrated. Rebuild the cache against the new isolated profile.

## Develop and validate

```bash
uv sync --locked --extra dev --extra oracle
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv lock --check
uv build
```

Live tests skip without explicit disposable opt-in. See [portable fixtures and commands](docs/testing-oracle.md) and [actual release evidence](docs/validation-0.8.0.md). Mock tests do not certify a database version.

## Local Docker demo

Create isolated SQL Server and Oracle databases with sample data and run actual MCP checks. An optional second Oracle exercises DB links; Thin and Thick share the same Oracle server. See the [local Docker guide](docs/local-docker.md) for commands, private profiles, sample SQL, and explicit data cleanup.

[Issues](https://github.com/LukeStackLab/db-semantic-mcp/issues) · [MIT License](LICENSE)
