<div align="center">

# sqlserver-semantic-mcp

**Give AI agents MCP tools to explore SQL Server, find table relationships and query data within configured permissions.**

[![PyPI](https://img.shields.io/pypi/v/sqlserver-semantic-mcp?color=blue&label=PyPI)](https://pypi.org/project/sqlserver-semantic-mcp/)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://pypi.org/project/sqlserver-semantic-mcp/)
[![Protocol](https://img.shields.io/badge/Protocol-MCP-8A2BE2)](https://modelcontextprotocol.io)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![SQL Server](https://img.shields.io/badge/SQL%20Server-2017%2B-CC2927?logo=microsoftsqlserver&logoColor=white)](#quick-installation)

[![繁體中文](https://img.shields.io/badge/Language-%E7%B9%81%E9%AB%94%E4%B8%AD%E6%96%87-64748B?style=for-the-badge)](README.zh-TW.md)
[![English](https://img.shields.io/badge/Language-English-2563EB?style=for-the-badge)](README.en.md)

[Installation](#quick-installation) · [Setup wizard](#setup-wizard) · [Multiple connections](#multiple-connections) · [Permissions and cost](#permissions-and-cost) · [Maintenance](#profile-storage-and-maintenance)

Supports **Codex · Claude Code · Claude Desktop · Cursor**

</div>

| Explore databases | Keep responses compact | Isolate connections |
|:---|:---|:---|
| Discover schema relationships and cache metadata and semantic analysis. | Get full columns, indexes and query results directly; oversized output is cut and flagged. | Each process has its own account, permissions, output limits and cache. Read-only by default. |

---

## Quick installation

> **Before you start**: prepare Git, a reachable SQL Server 2017+ instance and a SQL username/password. The wizard uses SQL authentication; the account needs the required data-read and metadata permissions.

### 1. Install uv

**macOS / Linux**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows PowerShell**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Open a new terminal after installation. See the [official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/) for other methods.

### 2. Install the MCP server

These commands work on Windows, macOS and Linux. If you already have the source, run the last command from its directory.

```bash
git clone https://github.com/LukeStackLab/sqlserver-semantic-mcp.git
cd sqlserver-semantic-mcp
uv tool install --python 3.11 --reinstall .
```

Use a source version that includes `setup`. uv downloads Python when needed and creates a persistent tool environment. If installation displays a PATH warning, run `uv tool update-shell` and open a new terminal; see [uv tool environments](https://docs.astral.sh/uv/concepts/tools/).

## Setup wizard

For Claude Code, first change to the project where you want to use the MCP server. The wizard writes that project's `.mcp.json`.

```bash
sqlserver-semantic-mcp setup
```

The terminal prompts are currently in Traditional Chinese.

1. Select an MCP client. The wizard selects it automatically when exactly one client is detected.
2. Enter an MCP name, SQL Server host, port, database, username and password. Names use letters, numbers, underscores or hyphens, such as `erp_prod`. The default port is `1433`; password input is hidden.
3. At the prompt to allow inserts and updates, press Enter for read-only access on a new connection. Answer `y` only when writes are needed.
4. After a successful connection check, answer `y` to add another connection or `n` to finish.
5. Confirm the summary. The wizard saves profiles, verifies each MCP handshake, then merges client settings and backs up the original file.

> **After setup**: reload MCP settings or restart the client. Claude Code still handles project trust prompts. Claude Desktop default settings paths are supported on macOS and Windows.

## Multiple connections

Give each connection a different name. Create all five below in one wizard session, or run `setup` again later. Each profile can tune its output cap (`max_response_bytes`) in `connection.json`.

| MCP name | Example use | Permissions |
|:---|:---|:---|
| `erp_prod` | Production ERP | Read-only |
| `erp_test` | Test ERP | Limited writes |
| `reporting` | Reporting database | Read-only |
| `crm` | Customer database | Read-only |
| `warehouse` | Warehouse database | Read-only |

Name the MCP server in your AI conversation:

> Use `erp_prod` to find the relationship between orders and customers.
>
> Next, use `reporting` to calculate last month's sales.

The agent selects that server's tools. Each MCP process keeps its fixed profile; response `source` fields identify the connection and database.

## Permissions and cost

### Operations

Only decide whether limited writes are needed:

| Setting | Allowed operations |
|---|---|
| Read-only, default | `SELECT` |
| Limited writes | `SELECT`, one `INSERT ... VALUES`, or one `UPDATE` with `WHERE` |

Limited writes exclude `DELETE`, DDL, `EXEC`, write subqueries, CTEs and `OUTPUT`. New profiles default to 1,000 returned rows, 100 affected rows and a 30-second query timeout. Writes from these fixed connection profiles are rolled back when they exceed the affected-row cap.

MCP policies do not grant SQL Server permissions. Use a dedicated account for each purpose and enforce read-only access in the database for production connections. The wizard checks login and metadata access; it does not perform writes to test permissions.

### Output limits

`response_mode`, `return_mode`, `detail` and `token_budget_hint` have been removed. Existing `connection.json` files can still contain `budget`, but it is ignored; use the output limits below. There are no summary/sample response modes and no detail tiers. `plan_or_execute_query` returns columns and rows for executed SELECT statements, bounded by three hard limits; the first one reached wins:

| Limit | Default | Setting |
|:---|:---|:---|
| Rows | 1000 | `max_rows_returned`; lower per call with `max_rows` |
| Serialized column names and rows | 65536 | `max_response_bytes` |
| Characters per cell | 2000 | `max_cell_chars` (binary is truncated as hex) |

`max_response_bytes` counts the JSON size of column names and retained rows. It excludes response envelope fields such as validation and cache metadata.

When a result is cut, the response carries `truncated=true` and `truncation_reason` (`row_limit` or `byte_limit`), and `total_row_count` is null. Narrow it with fewer columns, `WHERE`/`TOP`, or an aggregate. `get_tables` lists up to 500 tables (`limit` overrides).

These are hard output-size limits, not token or monetary caps; actual fees depend on your client, model and usage. Use `tool_metrics` to inspect response sizes in bytes. Enabling more MCP servers may add tool descriptions to the context, so enable only the connections you need.

<details>
<summary><b>Query results, joins and tool profiles</b></summary>

`row_count` is the retained count. `total_row_count` is exact only when the result set is exhausted, otherwise null. `truncated` indicates omitted results. Request COUNT_BIG explicitly for an exact full count.

`describe_table` always returns full columns, keys, indexes and descriptions; pass `columns=["Id", "Amount"]` to select columns with exact SQL types, complete join constraints, and required join-column metadata. Compound FKs include every column_pair. Parallel FK roles produce bounded candidates and ambiguity. Scores are heuristics; cardinality is unknown without valid uniqueness evidence. Index metadata includes XML/spatial indexes while cardinality inference still requires valid unique-key evidence. Classification consistently uses scope-visible FKs; restricted classifications are not shared across scopes. Discovery searches local names, column names, descriptions and Traditional Chinese aliases without external models. Qualified exact table names rank ahead of ordinary text matches. Customize `discovery_aliases` in connection.json.

A denied statement carries `denial`: `layer` (`policy`, `keyword_guard` or `fixed_profile`), `rule`, `message` and `hint`; keyword-guard denials also list `matches` with keyword, line and column. Fix the SQL from `rule` and `hint` instead of guessing.

A single `SELECT` may be preceded by scalar `DECLARE @v type [= expr]` and `SET @v = expr`. Initializers cannot read objects (subqueries, tables, user functions); table variables and options such as `SET NOCOUNT` are refused. `SELECT INTO`, temp tables, `EXEC` and multiple result sets remain unsupported.

When a write exceeds `max_rows_affected`, fixed connection profiles (wizard-created `connection.json`) always roll back. Configurations from environment variables or a policy file only report `exceeded_cap` by default; set `SEMANTIC_MCP_STRICT_ROWS_AFFECTED_CAP=true` or pass `affected_rows_policy="strict"` to roll back.

`plan_or_execute_query(validate_result_schema=true)` optionally describes SQL Server result metadata before execution. It adds a DB round trip, has limitations such as temporary tables, and does not prove business correctness. It is off by default.

```bash
sqlserver-semantic-mcp setup --tool-profile explore
```

| Profile | Tools | Purpose |
|---|---:|---|
| all (default) | 12 | Full functionality |
| query | 2 | Policy and execution for already verified SQL |
| explore | 8 | Discover tables, columns and joins, then query |
| maintenance | 3 | Policy, cache refresh and metrics |

Existing profiles can set tool_profile in connection.json and restart. Omitting the setup option preserves the existing selection. Tool selection does not grant DB permissions. List results use a data envelope; tools provide structuredContent/outputSchema and isError for failures.

</details>

<details>
<summary><b>Authorization, cache and compatibility</b></summary>

Authorization always uses strict TSQL AST analysis, including when routing uses regex. It checks every statement and physical reference. Unsupported/remote/dynamic sources, transaction control and SELECT INTO fail closed. FOR SYSTEM_TIME and external DDL remain unsupported until their physical history tables or external sources can be resolved and authorized. Execution accepts one statement; validation can inspect a policy-approved batch. Explicit invalid/missing policy files and unknown policy fields now stop startup. A caller's report option cannot lower a server strict affected-row cap.

SQL, metadata, resources, FKs and object dependencies share scope. allowed_tables supports bare, schema-qualified and database-qualified TSQL identifiers. Allow comparisons retain case for case-sensitive databases; deny comparisons also reject case variants. Restricted scopes conservatively reject metadata functions that may read other objects, including FileTableRootPath and STATS_DATE. Dedicated SQL accounts remain the final enforcement boundary. Confirmed commits remain successful even if cleanup fails. An uncertain commit/rollback returns transaction_state="unknown" and automatic_retry_allowed=false: executed=false does not prove rollback, so verify the DB outcome before retrying. Unverifiable or restricted definitions are withheld with explicit omission flags.

Old SQLite stores migrate automatically. Legacy FK rows without constraint identity and missing valid uniqueness evidence remain incomplete until refreshed. Snapshots, including comments, have a default maximum age of 300 seconds. Both tools and resources schedule single-flight background revalidation. Capture time and stale state belong to the snapshot used by the response; a concurrent refresh cannot relabel old content as fresh. Refreshing/refresh_failed describe the current update state. Responses without a content snapshot identity use cache.snapshot_unknown=true; current persisted state is reported separately in cache.latest_snapshot and does not identify the response content. Every full update compares previous fingerprints and invalidates changed module bodies and dependent definitions, including manual refresh. An offline DB can leave explicitly stale cached results.

Each process defaults to 4 DB workers. Cancelling a waiter does not interrupt the synchronous driver: capacity remains occupied until cleanup finishes. Configure db_worker_limit and schema_cache_ttl_s in connection.json. tool_metrics includes bytes, latency percentiles, worker wait, DB lifecycle duration, fetched rows, observed cache hits and failure categories without storing SQL, rows or passwords. DB lifecycle duration includes connection/fetch time, not SQL Server CPU time.

Tool descriptions are shorter while full outputSchema contracts and server-side response validation remain intact. AST routing reuses one analysis; keyword checks and cache freshness calculations share their results. Unused internal domain models and enums have been removed. Choose query for known SQL to reduce context. Measure actual tokens and throughput with your client and database. MCP Python SDK 1.27+ is required.

</details>

## Profile storage and maintenance

Each profile lives in `connections/<MCP name>/` and contains `connection.json`, `policy.json` and a separate cache. Passwords are stored in the system keyring when available.

<details>
<summary><b>Profile locations and password storage</b></summary>

| Platform | Default profile root |
|---|---|
| macOS | `~/Library/Application Support/sqlserver-semantic-mcp/connections/` |
| Windows | `%APPDATA%\sqlserver-semantic-mcp\connections\` |
| Linux | `$XDG_CONFIG_HOME/sqlserver-semantic-mcp/connections/`; defaults to `~/.config/sqlserver-semantic-mcp/connections/` |

On macOS / Linux, an unavailable keyring can be replaced by a plaintext credential file only with explicit consent, using directory mode `0700` and file mode `0600`. Windows uses the system keyring. Passwords are never written to client settings or `connection.json`.

</details>

<details>
<summary><b>Updates, diagnostics and existing startup options</b></summary>

- **Update a connection**: rerun `setup`, enter its existing MCP name and confirm the update. Reload the client afterward.
- **Check a connection**: replace the placeholder below with the full `connection.json` path printed by the wizard.

```bash
sqlserver-semantic-mcp doctor --config "PATH_TO_CONNECTION_JSON"
```

Client entries use the Python environment created during installation. If you move or remove it, reinstall and rerun `setup`. To update an installed package, run `uv tool install --python 3.11 --reinstall .` from the updated source directory.

Existing environment-variable and `.env` startup remains available; see [`.env.example`](.env.example). Fixed profiles created by the wizard ignore cwd `.env` and inherited `SEMANTIC_MCP_*` overrides.

</details>

<details>
<summary><b>Development and tests</b></summary>

```bash
uv sync --locked --extra dev
uv run pytest
uvx ruff check .
uvx ruff format .
```

</details>

---

<div align="center">

[Report an issue](https://github.com/LukeStackLab/sqlserver-semantic-mcp/issues) · [MIT License](LICENSE)

</div>
