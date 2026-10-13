# sqlserver-semantic-mcp

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-1.0%2B-purple.svg)](https://modelcontextprotocol.io)
[![Version](https://img.shields.io/badge/version-0.6.0-green.svg)](pyproject.toml)
[![繁體中文](https://img.shields.io/badge/lang-繁體中文-red.svg)](README.zh-TW.md)

> **Semantic intelligence layer for SQL Server databases, exposed via MCP.**
> Not a SQL executor — a database understanding engine for AI agents.

AI agents don't need raw `execute_sql`. They need to understand schema structure, relationships, object dependencies, and — most importantly — to operate inside a safety boundary that an operator can define.

`sqlserver-semantic-mcp` provides all of this through 12 MCP tools, 1 concrete MCP resource, and 5 MCP resource templates, backed by a two-tier SQLite cache for speed and a JSON-based policy system for safety.

---

## Quick Start

### Interactive setup and independent MCP servers

From this source checkout, run:

```bash
uv sync --extra dev
uv run sqlserver-semantic-mcp setup
```

The Traditional Chinese wizard detects MCP clients and asks for a name, SQL Server host, port, database, user, hidden password and whether limited writes are needed. Permissions default to read-only and the response budget defaults to `medium`. Repeat the add-connection step to configure five or more independent servers.

Supported clients are Codex, Claude Code, Claude Desktop and Cursor. Claude Desktop default paths are supported on macOS and Windows. Claude Code uses the current project's `.mcp.json`. After confirmation, each server must pass an MCP handshake before registration. Existing client settings and other MCP servers are preserved, with timestamped backups. Conflicting names are rejected. Reload the client's MCP configuration when needed; Claude Code project trust remains controlled by the client.

Each entry launches a fixed-profile process. Ask the agent to use a server by name, for example `erp_test`. Every process has its own connection, policy and cache. Tool and resource results identify the connection and database in `source`; the server does not switch global connections.

Profiles are stored under `~/Library/Application Support/sqlserver-semantic-mcp/connections/` on macOS, `%APPDATA%/sqlserver-semantic-mcp/connections/` on Windows, and `$XDG_CONFIG_HOME/sqlserver-semantic-mcp/connections/` on Linux, defaulting to `~/.config/` if XDG_CONFIG_HOME is unset. Each profile directory contains `connection.json`, `policy.json` and a separate cache.

Passwords are stored in the system keyring when available; profile and client files contain only references. On POSIX, an unavailable keyring can be replaced by a plaintext credential file only after explicit consent, with directory mode 0700 and file mode 0600. Windows uses the system keyring. Manually managed profiles may also reference a password environment variable.

Run setup again to add or update profiles. Advanced path overrides and diagnostics:

```bash
uv run sqlserver-semantic-mcp setup --client cursor --config-dir /absolute/connections --client-config /absolute/mcp.json
uv run sqlserver-semantic-mcp doctor --config /absolute/connections/erp_test/connection.json
```

Registrations use the Python environment that runs setup, with `-I` to isolate imports from the client working directory and PYTHONPATH. Rerun setup if that environment is moved or removed. Once a package release includes this feature, `uvx sqlserver-semantic-mcp setup` can also be used.

Fixed-profile serving ignores cwd `.env` and inherited `SEMANTIC_MCP_*` settings. Missing credentials or invalid policy prevent startup. A changed target, principal or policy selects a new cache path. Writes use advisory locks, snapshot checks and atomic replacement; cooperating installers are serialized, while external editors may not honor the lock. Backups support recovery.

Limited writes support one simple `INSERT ... VALUES` or `UPDATE` with WHERE; an affected-row overflow is rolled back. Write subqueries, CTEs, OUTPUT, DDL and EXEC are rejected. Database scope still requires a dedicated SQL Server account with appropriate grants. The wizard only performs read-only connection checks. It currently uses SQL username/password authentication; the existing environment-based Windows Authentication path remains available.

### Existing manual single-connection configuration

Pick the path that matches your client. All paths use [`uvx`](https://docs.astral.sh/uv/) — no `git clone`, no virtualenv, no manual install. `uvx` downloads and runs the package on demand and caches it for next time.

> **Prerequisite:** Install [uv](https://docs.astral.sh/uv/getting-started/installation/) once (`curl -LsSf https://astral.sh/uv/install.sh | sh`). Python 3.11+ is fetched automatically by `uv` if needed.

> **Replace** `localhost` / `YourDatabase` / `sa` / `YourPassword` in every example with your real SQL Server credentials.

### 🤖 Claude Code CLI

One command registers the server. `uvx` resolves and caches `sqlserver-semantic-mcp` on first use:

```bash
claude mcp add sqlserver-semantic -- uvx sqlserver-semantic-mcp \
  -e SEMANTIC_MCP_MSSQL_SERVER=localhost \
  -e SEMANTIC_MCP_MSSQL_DATABASE=YourDatabase \
  -e SEMANTIC_MCP_MSSQL_USER=sa \
  -e SEMANTIC_MCP_MSSQL_PASSWORD=YourPassword
```

Or commit the config to your repo as `.mcp.json` for the whole team to share:

```json
{
  "mcpServers": {
    "sqlserver-semantic": {
      "command": "uvx",
      "args": ["sqlserver-semantic-mcp"],
      "env": {
        "SEMANTIC_MCP_MSSQL_SERVER": "localhost",
        "SEMANTIC_MCP_MSSQL_DATABASE": "YourDatabase",
        "SEMANTIC_MCP_MSSQL_USER": "sa",
        "SEMANTIC_MCP_MSSQL_PASSWORD": "YourPassword"
      }
    }
  }
}
```

Verify with `claude mcp list`. The server speaks MCP over stdio and will be ready as soon as Claude Code launches a session.

### 🛠 Codex CLI

Add this block to `~/.codex/config.toml`:

```toml
[mcp_servers.sqlserver-semantic]
command = "uvx"
args = ["sqlserver-semantic-mcp"]
env = { SEMANTIC_MCP_MSSQL_SERVER = "localhost", SEMANTIC_MCP_MSSQL_DATABASE = "YourDatabase", SEMANTIC_MCP_MSSQL_USER = "sa", SEMANTIC_MCP_MSSQL_PASSWORD = "YourPassword" }
```

Then run `codex` — the server will appear in your MCP tool list.

### 🖥 Claude Desktop

Edit your config file:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "sqlserver-semantic": {
      "command": "uvx",
      "args": ["sqlserver-semantic-mcp"],
      "env": {
        "SEMANTIC_MCP_MSSQL_SERVER": "localhost",
        "SEMANTIC_MCP_MSSQL_DATABASE": "YourDatabase",
        "SEMANTIC_MCP_MSSQL_USER": "sa",
        "SEMANTIC_MCP_MSSQL_PASSWORD": "YourPassword"
      }
    }
  }
}
```

Restart Claude Desktop after saving.

### 🧪 Smoke test (optional, all clients)

Confirm the package can run before wiring it into a host:

```bash
SEMANTIC_MCP_MSSQL_SERVER=localhost \
SEMANTIC_MCP_MSSQL_DATABASE=YourDatabase \
SEMANTIC_MCP_MSSQL_USER=sa \
SEMANTIC_MCP_MSSQL_PASSWORD=YourPassword \
  uvx sqlserver-semantic-mcp
```

You should see startup log lines confirming cache initialisation and tool registration. Press `Ctrl+C` to stop.

### 🧰 Local development (contributors only)

Skip this section if you just want to use the server.

```bash
git clone https://github.com/LukeStackLab/sqlserver-semantic-mcp.git
cd sqlserver-semantic-mcp
cp .env.example .env             # then fill in MSSQL credentials
uv sync --dev                    # creates .venv with dev deps
uv run python -m sqlserver_semantic_mcp.main
```

For an editable install with pip instead:

```bash
pip install -e ".[dev]"
sqlserver-semantic-mcp
```

When pointing an MCP client at a local checkout, replace `uvx sqlserver-semantic-mcp` with:

```json
"command": "uv",
"args": ["run", "--project", "/absolute/path/to/sqlserver-semantic-mcp",
         "python", "-m", "sqlserver_semantic_mcp.main"]
```

See the full env-var matrix in [Configuration](#configuration).

---

## Features

- **12 MCP tools** across 9 capability groups (metadata, relationship, semantic, object, query, policy, cache, metrics, workflow) — consolidated from an earlier 29-tool surface; absorbed capabilities live on as parameters (see [MCP Tools](#mcp-tools))
- **Two-tier SQLite cache** — Structural Cache (warm on startup) + Semantic Cache (lazy + background fill)
- **Cache-first startup** — reuse existing structural cache by default and avoid mandatory full warmup on every process start
- **Automatic drift detection (L1 schema probe)** — a throttled, catalog-only fingerprint probe detects column type/length changes, new/dropped tables, index changes, and view/procedure/function body edits, then refreshes the cache automatically
- **Per-table invalidation** — only tables whose own structure changed are re-analysed; views/functions depending on a changed table are cascaded dirty via `sys.sql_expression_dependencies`
- **3-hash schema versioning** — detect when structural / object / comment changes invalidate cached analysis
- **Policy-gated execution** — SELECT/INSERT/UPDATE/DELETE/… permissions, WHERE-clause requirements, row caps, schema/table allowlists
- **Semantic classification** — automatic detection of fact / dimension / lookup / bridge / audit tables
- **Join path discovery** — BFS over the FK graph to find how two tables relate
- **Object inspection** — view / procedure / function definitions with dependency tracing plus read/write split
- **Workflow shortcuts** — candidate-table discovery plus a direct-execution fast path (`plan_or_execute_query`) that folds in risk estimation; context bundling is now an MCP resource rather than a tool
- **Payload metrics** — built-in measurement for per-tool response size
- **Graceful degradation** — missing or malformed policy file falls back to read-only; unreachable DB doesn't corrupt cache

---

## Architecture

Five-layer architecture with strict one-way dependencies:

```
MCP Interface      (server/)          ← tool / resource registration
      ↓
Application        (services/)        ← 6 services orchestrate cache + policy + DB
      ↓
Policy / Domain    (policy/, domain/) ← models, SQL intent analysis, enforcement
      ↓
Infrastructure     (infrastructure/)  ← pymssql + SQLite + background task
      ↓
SQL Server + SQLite
```

### Cache Model

| Layer | Contents | Strategy | Invalidation |
|---|---|---|---|
| **Structural Cache** | tables, columns, PK/FK, indexes, objects list, comments | warm on startup, SQLite persisted | schema probe drift, per-table `structural_hash` mismatch |
| **Semantic Cache** | table classification, column semantics, object definitions, dependencies | lazy + background incremental fill | per-table hash change → affected rows marked `dirty` → recomputed |
| **Fingerprint Baseline** | per-table / per-object catalog fingerprints (`modify_date` + column/index checksums + module definition hash) | written on every warmup | compared by the schema probe on each window |

### Schema Drift Detection (L1 Probe)

The cache is hash-driven, not TTL-driven: nothing expires on a clock, and an
unchanged database costs zero re-fetches. Freshness is guaranteed by a
lightweight probe that runs at most once per `SEMANTIC_MCP_PROBE_INTERVAL_S`
(default 60s), triggered lazily by incoming tool calls:

1. **Probe** — five catalog-only queries (`sys.tables`, `sys.columns`,
   `sys.indexes`, `sys.sql_modules`, `sys.sql_expression_dependencies`)
   compute per-table and per-object fingerprints in milliseconds, without
   touching any data rows. Column type changes (`int → bigint`), length
   changes (`nvarchar(4) → nvarchar(10)`), precision/nullability/collation
   changes, index changes, and view/procedure/function body edits are all
   captured.
2. **Compare** — fingerprints are diffed against the baseline stored in
   SQLite. No drift → done (the served response always comes from cache).
3. **Refresh on drift** — the full structural snapshot is re-fetched, only
   the *changed* tables' semantic rows are marked `dirty`, dropped
   tables/objects are pruned, and modules that depend on a changed table
   (which SQL Server does not touch when an underlying table changes) are
   cascaded `dirty` via the dependency graph. The background fill loop then
   recomputes the dirty rows.

Revalidation is stale-while-revalidate: tool calls are always served from
cache immediately while the probe (and refresh, if drift was found) runs in
the background. `refresh_schema_cache` remains available for an immediate
forced refresh, and disabling the cache (`SEMANTIC_MCP_CACHE_ENABLED=false`)
disables probing too.

---

## Installation

> Most users should follow [Quick Start](#quick-start) instead — it uses `uvx` and needs no install step. This section is for contributors and offline / air-gapped setups.

Requires Python 3.11+.

**One-shot run via uvx** (no install, recommended for end users):

```bash
uvx sqlserver-semantic-mcp
```

**Install globally as a CLI tool:**

```bash
uv tool install sqlserver-semantic-mcp
# or:
pipx install sqlserver-semantic-mcp
```

**Editable install from source with pip** (registers the `sqlserver-semantic-mcp` console script on your PATH):

```bash
pip install -e ".[dev]"
```

**Install with uv from source:**

```bash
uv sync
# include dev dependencies:
uv sync --dev
```

**Run without installing** (uv project mode):

```bash
uv run python -m sqlserver_semantic_mcp.main
```

Installed dependencies:

| Package | Role |
|---|---|
| `mcp` | MCP SDK (stdio transport) |
| `pymssql` | SQL Server wire driver (wraps FreeTDS) |
| `pydantic` + `pydantic-settings` | Config validation, env var loading |
| `aiosqlite` | Async SQLite for the two-tier cache |

Dev-only dependencies: `pytest`, `pytest-asyncio`, `pytest-mock`.

> **Linux note:** `pymssql` links against FreeTDS. If `pip install` fails with a compiler error, install system headers first — see [Troubleshooting](#troubleshooting).

---

## Configuration

All configuration is via environment variables with the `SEMANTIC_MCP_` prefix. A `.env` file in the working directory is also loaded automatically. Start from `.env.example`.

For normal use, configure only the connection. Permissions default to read-only and the response budget defaults to `medium`; no extra tier selection is needed. Enable `read_write_safe` through the Custom Policy File instructions below only when writes are needed. Other settings are for advanced use.

### Required

| Variable | Description |
|---|---|
| `SEMANTIC_MCP_MSSQL_SERVER` | SQL Server host (supports `(localdb)\Instance` and `*.database.windows.net`) |
| `SEMANTIC_MCP_MSSQL_DATABASE` | Target database name |
| `SEMANTIC_MCP_MSSQL_USER` | SQL auth user (not required when `SEMANTIC_MCP_MSSQL_WINDOWS_AUTH=true`) |
| `SEMANTIC_MCP_MSSQL_PASSWORD` | SQL auth password |

### Optional

| Variable | Default | Description |
|---|---|---|
| `SEMANTIC_MCP_MSSQL_PORT` | `1433` | TCP port |
| `SEMANTIC_MCP_MSSQL_WINDOWS_AUTH` | `false` | Use Windows Authentication |
| `SEMANTIC_MCP_MSSQL_ENCRYPT` | `false` | Force TLS (auto-enabled for Azure SQL) |
| `SEMANTIC_MCP_CACHE_PATH` | `./cache/semantic_mcp.db` | SQLite cache file location |
| `SEMANTIC_MCP_CACHE_ENABLED` | `true` | Disable to skip startup warmup |
| `SEMANTIC_MCP_STARTUP_MODE` | `cache_first` | `cache_first` reuses existing cache on restart; `full` always refreshes from SQL Server before serving |
| `SEMANTIC_MCP_PROBE_INTERVAL_S` | `60` | Minimum seconds between schema drift probes (throttle window) |
| `SEMANTIC_MCP_BACKGROUND_BATCH_SIZE` | `5` | Tables processed per background batch |
| `SEMANTIC_MCP_BACKGROUND_INTERVAL_MS` | `500` | Delay between batches |
| `SEMANTIC_MCP_POLICY_FILE` | *(builtin readonly)* | Path to policy JSON |
| `SEMANTIC_MCP_POLICY_PROFILE` | *(file's active_profile)* | Override which profile is active |
| `SEMANTIC_MCP_MAX_ROWS_RETURNED` | `1000` | Override SELECT row cap |
| `SEMANTIC_MCP_MAX_ROWS_AFFECTED` | `100` | Override DML affected-row cap |
| `SEMANTIC_MCP_QUERY_TIMEOUT` | `30` | Query timeout in seconds |
| `SEMANTIC_MCP_TOOL_PROFILE` | `all` | Comma-separated tool groups: metadata, relationship, semantic, object, query, policy, cache, metrics, workflow |
| `SEMANTIC_MCP_WORKFLOW_TOOLS_ENABLED` | `true` | Disable workflow-layer shortcut tools |
| `SEMANTIC_MCP_METRICS_ENABLED` | `true` | Enable per-tool response size metrics |
| `SEMANTIC_MCP_DEFAULT_DETAIL` | `brief` | Default detail tier for agent-facing tools |
| `SEMANTIC_MCP_DEFAULT_RESPONSE_MODE` | `summary` | Default query execution response shape |
| `SEMANTIC_MCP_DEFAULT_TOKEN_BUDGET_HINT` | `medium` | Default response budget: up to 500 listed tables or 50 sample rows. These are item limits, not a fixed token cap |
| `SEMANTIC_MCP_DIRECT_EXECUTE_ENABLED` | `true` | Allow workflow fast-path direct execution when policy approves |
| `SEMANTIC_MCP_STRICT_ROWS_AFFECTED_CAP` | `true` | Roll back writes that exceed affected-row cap by default |
| `SEMANTIC_MCP_INTENT_ANALYZER` | `regex` | SQL intent analyzer backend (`regex` or `ast`) |

---

## Connection Scenarios

Copy the relevant block into your `.env` file (or into the `env` map in your MCP client config).

### SQL Authentication (default)

The most common setup. SQL Server Authentication must be enabled on the instance.

```env
SEMANTIC_MCP_MSSQL_SERVER=localhost
SEMANTIC_MCP_MSSQL_DATABASE=YourDatabase
SEMANTIC_MCP_MSSQL_USER=sa
SEMANTIC_MCP_MSSQL_PASSWORD=YourPassword
```

### Windows Authentication

Omit `MSSQL_USER` and `MSSQL_PASSWORD`; the process must run under a Windows account that has SQL Server access. Use double-backslash for named instances in `.env` files.

```env
SEMANTIC_MCP_MSSQL_SERVER=MY-PC\\SQLEXPRESS
SEMANTIC_MCP_MSSQL_DATABASE=YourDatabase
SEMANTIC_MCP_MSSQL_WINDOWS_AUTH=true
```

> Windows Authentication is only available on Windows. pymssql does not support it on Linux or macOS — use SQL Authentication on those platforms.

### Azure SQL Database

TLS is automatically enabled when the server name ends in `.database.windows.net`; you do not need to set `MSSQL_ENCRYPT` explicitly.

```env
SEMANTIC_MCP_MSSQL_SERVER=yourserver.database.windows.net
SEMANTIC_MCP_MSSQL_DATABASE=YourDatabase
SEMANTIC_MCP_MSSQL_USER=youradmin@yourserver
SEMANTIC_MCP_MSSQL_PASSWORD=YourPassword
```

### LocalDB (Windows only)

LocalDB communicates over a named pipe — no TCP port is required. Windows Authentication is used by default.

```env
SEMANTIC_MCP_MSSQL_SERVER=(localdb)\MSSQLLocalDB
SEMANTIC_MCP_MSSQL_DATABASE=YourDatabase
SEMANTIC_MCP_MSSQL_WINDOWS_AUTH=true
```

### Custom Policy File

Point `SEMANTIC_MCP_POLICY_FILE` at a JSON policy file you control. Without this setting the server operates in built-in read-only mode.

```env
SEMANTIC_MCP_POLICY_FILE=./config/policy.example.json
SEMANTIC_MCP_POLICY_PROFILE=read_write_safe
```

See [Policy System](#policy-system) for the full policy file format and available profiles.

### Custom Cache Location

Useful when running multiple server instances against different databases, or when the default `./cache/` directory is not writable.

```env
SEMANTIC_MCP_CACHE_PATH=/var/lib/sqlserver-mcp/mydb.db
```

---

## Policy System

**Default security posture:** if no policy file is configured, the server operates in built-in **read-only** mode by default — no configuration needed to enforce it. In this mode: only `SELECT` statements are permitted, results are capped at 1000 rows, multi-statement queries are rejected, and every query still passes through the policy enforcer before reaching `cursor.execute()`. No unrestricted SQL execution path exists.

To enable writes or change any constraint, create a policy JSON file and point `SEMANTIC_MCP_POLICY_FILE` at it (see `config/policy.example.json`):

```json
{
  "active_profile": "read_write_safe",
  "profiles": {
    "readonly":        { "operations": { "select": true } },
    "read_write_safe": {
      "operations": { "select": true, "insert": true, "update": true },
      "constraints": {
        "require_where_for_update": true,
        "max_rows_affected": 100
      }
    },
    "admin": {
      "operations": { "select": true, "insert": true, "update": true, "delete": true },
      "constraints": { "allow_multi_statement": true }
    }
  }
}
```

### Policy fields

**Operations** — 10 flags (select / insert / update / delete / truncate / create / alter / drop / execute / merge)

**Constraints** — `require_where_for_update`, `require_where_for_delete`, `require_top_for_select`, `max_rows_returned`, `max_rows_affected`, `allow_multi_statement`, `query_timeout_seconds`

**Scope** — `allowed_databases`, `allowed_schemas`, `allowed_tables`, `denied_tables`

### Profile quick reference

| Profile | SELECT | INSERT | UPDATE | DELETE | WHERE required | Row cap |
|---|---|---|---|---|---|---|
| `readonly` (builtin default) | Yes | No | No | No | N/A | 1000 returned |
| `read_write_safe` | Yes | Yes | Yes | No | UPDATE requires WHERE | 100 affected |
| `admin` | Yes | Yes | Yes | Yes | No | 10 000 affected |

> **Safety note:** when `allowed_schemas` is set, queries that reference a table without a schema prefix (e.g. `SELECT * FROM Users` instead of `dbo.Users`) are rejected — you cannot bypass schema-level access control with implicit defaults.

### Failure behavior

| Condition | Behavior |
|---|---|
| Policy file path unset | Builtin readonly, log warning |
| Policy file missing | Builtin readonly, log warning |
| Policy file unreadable | Builtin readonly, log error |
| Policy file has invalid JSON | Builtin readonly, log error |
| Policy file fails schema validation | Builtin readonly, log error |
| `active_profile` / override points to a missing profile | Server refuses to start (misconfiguration surfaced) |

---

## MCP Tools

The surface was consolidated from an earlier 29-tool set down to **12 tools** across 9 capability groups. Removed single-purpose tools were folded into the closest surviving tool via a parameter — see "Absorbed capability" below.

| Group | Tool(s) | Absorbed capability (via parameter) |
|---|---|---|
| `metadata` (2) | `get_tables`, `describe_table` | `describe_table(detail=brief\|standard\|full)` replaces `get_columns` (full column detail); classification is included at every tier, replacing `classify_table`; `important_columns` in the response replaces `summarize_table_for_joining` |
| `relationship` (3) | `get_table_relationships`, `find_join_path`, `get_dependency_chain` | `find_join_path(score=true)` replaces `score_join_candidate` |
| `semantic` (1) | `detect_lookup_tables` | `analyze_columns`' per-column semantic tagging now surfaces through `describe_table` |
| `object` (1) | `describe_object` | `describe_object(type=VIEW\|PROCEDURE\|FUNCTION, detail=...)` replaces `describe_view`, `describe_procedure`, `trace_object_dependencies`, and `summarize_object_for_impact` |
| `query` (1) | `plan_or_execute_query` | `plan_or_execute_query(mode=auto\|validate\|dry_run\|execute_if_safe)` replaces `validate_query`, `run_safe_query`, `preview_safe_query`, and `estimate_execution_risk` |
| `policy` (1) | `get_execution_policy` | `get_execution_policy(reload=true)` replaces `refresh_policy`; `plan_or_execute_query(mode="validate")` replaces `validate_sql_against_policy` |
| `cache` (1) | `refresh_schema_cache` | — |
| `metrics` (1) | `tool_metrics` | `tool_metrics(action=get\|reset)` replaces `get_tool_metrics` / `reset_tool_metrics` |
| `workflow` (1) | `discover_relevant_tables` | `suggest_next_tool` was dropped — every response envelope already carries `next_action` / `recommended_tool`; `bundle_context_for_next_step` lives on as the `semantic://bundle/joining` MCP **resource**, not a tool |

For smaller prompts and faster discovery, prefer `detail="brief"` and filtered metadata calls, and call `plan_or_execute_query(mode="validate")` before executing anything you're unsure about.

---

## MCP Resources

Auto-listed concrete resources:

- `semantic://summary/database`

Auto-listed resource templates:

- `semantic://schema/tables/{qualified}`
- `semantic://analysis/classification/{qualified}`
- `semantic://summary/table/{qualified}`
- `semantic://summary/object/{type}/{qualified}`
- `semantic://bundle/joining/{qualified}`

Backward-compatible direct reads are also supported for:

- `semantic://schema/tables`
- `semantic://analysis/dependencies/{type}/{schema}.{name}`

---

## Running the Server

**Via the installed console script** (after `pip install -e .`):

```bash
sqlserver-semantic-mcp
```

**Via uv (without installing):**

```bash
uv run python -m sqlserver_semantic_mcp.main
```

**Via Python directly** (when the package is already on `sys.path`):

```bash
python -m sqlserver_semantic_mcp.main
```

The server speaks MCP over stdio. On startup it:

1. Opens (or creates) the SQLite cache
2. Reuses the existing Structural cache when `SEMANTIC_MCP_STARTUP_MODE=cache_first`, otherwise refreshes from SQL Server
3. Enqueues all tables for Semantic analysis
4. Launches the background fill task
5. Accepts MCP tool/resource calls — each call lazily triggers the throttled schema probe (see *Schema Drift Detection*), so schema changes made while the server runs are picked up automatically

Background fill uses exponential backoff (2ⁿ seconds, capped at 60s) on persistent errors to avoid log spam or CPU burn.

---

## Development

### Running tests

```bash
uv run --extra dev pytest tests/unit
uv run --extra dev pytest tests/integration -m integration
```

### Publishing a release (maintainers)

The package is distributed on PyPI so end users can run `uvx sqlserver-semantic-mcp` without cloning. Release flow:

1. Bump `version` in `pyproject.toml` (semantic versioning).
2. Update the changelog in `docs/` and the version badge at the top of this README.
3. Commit and tag:
   ```bash
   git commit -am "chore: bump to vX.Y.Z"
   git tag vX.Y.Z
   git push origin main --tags
   ```
4. Build and verify the artifacts locally:
   ```bash
   uv build                       # produces dist/*.whl + dist/*.tar.gz
   uvx --from twine twine check dist/*
   ```
5. (Recommended) Smoke test the wheel against TestPyPI first:
   ```bash
   uvx --from twine twine upload --repository testpypi dist/*
   uvx --index-url https://test.pypi.org/simple/ sqlserver-semantic-mcp
   ```
6. Publish to PyPI:
   ```bash
   uv publish                     # uses UV_PUBLISH_TOKEN or ~/.pypirc
   ```

> Configure a PyPI API token once: `export UV_PUBLISH_TOKEN=pypi-…` (or store under `[pypi]` in `~/.pypirc`).

### Project structure

```
sqlserver_semantic_mcp/
├── config.py                         — env-backed Pydantic settings
├── main.py                           — stdio server + startup + background task
├── domain/
│   ├── enums.py                      — TableType, ObjectType, CacheStatus, RiskLevel, SqlOperation
│   └── models/                       — Column, Table, ForeignKey, Index, Relationship, DbObject
├── policy/
│   ├── models.py                     — PolicyProfile / PolicyOperations / PolicyConstraints / PolicyScope
│   ├── loader.py                     — JSON loading with graceful fallback
│   ├── analyzer.py                   — regex-based SQL intent extraction
│   └── enforcer.py                   — policy decision (allow/reject + reason)
├── infrastructure/
│   ├── connection.py                 — pymssql connection + helpers
│   ├── background.py                 — background semantic fill loop with backoff
│   ├── cache/
│   │   ├── store.py                  — SQLite DDL + init
│   │   ├── structural.py             — hashing + warmup + snapshot persistence
│   │   ├── probe.py                  — L1 catalog fingerprints + drift diff
│   │   ├── revalidation.py           — throttled stale-while-revalidate orchestration
│   │   └── semantic.py               — analysis/definition I/O + pending queue
│   └── queries/                      — SQL Server queries (metadata / comments / objects / probe)
├── services/                         — 6 services (metadata / relationship / semantic / object / policy / query)
└── server/
    ├── app.py                        — MCP Server, tool registry, JSON envelope
    ├── tools/                        — 7 tool modules (one per capability group)
    └── resources/                    — schema / analysis / summary URIs
```

### Testing conventions

- **Unit tests** use in-memory or tmp-dir SQLite and mock pymssql.
- **Integration tests** are marked `@pytest.mark.integration` and skip unless `SEMANTIC_MCP_MSSQL_SERVER` is set.
- Pydantic models are exercised directly; infrastructure layers are tested with mocked connections.

---

## Troubleshooting

### pymssql / FreeTDS installation fails on Linux

`pymssql` links against FreeTDS. On Debian/Ubuntu, install the required system libraries before running `pip install`:

```bash
sudo apt-get install -y libssl-dev libkrb5-dev freetds-dev
pip install pymssql
```

On Alpine / Docker: `apk add freetds-dev openssl-dev krb5-dev`.

### "Cannot open server" or connection refused

- Confirm the server name and port are correct (`SEMANTIC_MCP_MSSQL_SERVER`, `SEMANTIC_MCP_MSSQL_PORT`).
- Check that TCP/IP is enabled in SQL Server Configuration Manager.
- If using a named instance (e.g. `MY-PC\SQLEXPRESS`), confirm SQL Server Browser is running so the port can be resolved dynamically.
- If using a non-default port, set `SEMANTIC_MCP_MSSQL_PORT` explicitly — SQL Server Browser is not needed when the port is fixed.
- Check firewall rules: port 1433 (or your custom port) must be reachable from the machine running the MCP server.

### "Login failed for user"

- Confirm `SEMANTIC_MCP_MSSQL_USER` and `SEMANTIC_MCP_MSSQL_PASSWORD` are correct.
- Verify that SQL Server Authentication is enabled on the instance (Server Properties → Security → SQL Server and Windows Authentication mode).
- For Azure SQL, the user may need to be in the format `user@servername` depending on your driver version.

### Windows Authentication not working on Linux or macOS

pymssql does not support Windows Authentication (Kerberos/NTLM) on non-Windows platforms. Use SQL Authentication (`MSSQL_USER` + `MSSQL_PASSWORD`) instead. `SEMANTIC_MCP_MSSQL_WINDOWS_AUTH=true` is only effective on Windows.

### LocalDB not connecting

- LocalDB is Windows-only and communicates over a named pipe, not TCP.
- Use the exact format `(localdb)\MSSQLLocalDB` (or your instance name) for `SEMANTIC_MCP_MSSQL_SERVER`.
- Set `SEMANTIC_MCP_MSSQL_WINDOWS_AUTH=true`; SQL auth is not supported by LocalDB by default.
- Run `sqllocaldb info` in a Windows terminal to list available instances and confirm they are running.

### Policy file not found or ignored

When the policy file cannot be read (missing, unreadable, or invalid JSON), the server falls back to built-in read-only mode and logs a warning. Check the startup logs for lines containing `policy`. If `SEMANTIC_MCP_POLICY_FILE` is set to a relative path, it is resolved from the process working directory — use an absolute path to avoid ambiguity.

### Server starts but tools return empty results

The Structural Cache may not have been populated yet. Check the startup logs for warmup progress. You can force a full refresh with the `refresh_schema_cache` MCP tool. Also verify that the connected database user has `VIEW DEFINITION` permission; without it, object definitions and comments will be absent from the cache.

---

## Security Design

- **Default read-only**: if no policy is configured, only `SELECT` is allowed.
- **SQL validation required**: every query passes through the intent analyzer and policy enforcer before reaching `cursor.execute()`.
- **Denied dangerous statements**: `DROP` / `TRUNCATE` are classified as `CRITICAL` risk level; blocked unless explicitly allowed.
- **Schema-aware access control**: `allowed_schemas` rejects implicit-schema queries to prevent schema-default bypass.
- **Policy hardening**: malformed policy files fall back to read-only rather than crashing the server.

---

## Limitations / Future Work

- SQL intent analyzer is regex-based, not a full T-SQL parser — CTE-defined names may appear as tables. Use `plan_or_execute_query(mode="validate")` first when in doubt.
- `STRING_AGG` used in the index query requires SQL Server 2017+. Older versions will need an alternative query. The schema probe's `HASHBYTES` over `nvarchar(max)` definitions requires SQL Server 2016+.
- The probe's column/index checksums use `CHECKSUM_AGG`/`BINARY_CHECKSUM` — a heuristic with a theoretical (astronomically small) collision chance. `refresh_schema_cache` always performs a full re-fetch regardless.
- Encrypted modules (`WITH ENCRYPTION`) expose no definition; their probe fingerprint falls back to `modify_date` only.
- `sys.extended_properties` reads require `VIEW DEFINITION` permission; comments on restricted objects won't appear in the cache.
- Background fill is single-worker; on very large schemas the Semantic Cache may take time to converge (use `refresh_schema_cache` to force a structural refresh; semantic classification still fills lazily).

---

## License

Licensed under the MIT License — see `LICENSE` for details.
