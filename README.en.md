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
| Discover schema relationships and cache metadata and semantic analysis. | Start with summaries; request columns, indexes or samples when needed. | Each process has its own account, permissions, budget and cache. Read-only by default. |

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

Give each connection a different name. Create all five below in one wizard session, or run `setup` again later. Every new profile defaults to the `medium` response budget.

| MCP name | Example use | Permissions | Response budget |
|:---|:---|:---|:---|
| `erp_prod` | Production ERP | Read-only | `medium` |
| `erp_test` | Test ERP | Limited writes | `medium` |
| `reporting` | Reporting database | Read-only | `medium` |
| `crm` | Customer database | Read-only | `medium` |
| `warehouse` | Warehouse database | Read-only | `medium` |

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

Limited writes exclude `DELETE`, DDL, `EXEC`, write subqueries, CTEs and `OUTPUT`. New profiles default to 1,000 returned rows, 100 affected rows and a 30-second query timeout. Writes exceeding the affected-row cap are rolled back.

MCP policies do not grant SQL Server permissions. Use a dedicated account for each purpose and enforce read-only access in the database for production connections. The wizard checks login and metadata access; it does not perform writes to test permissions.

### Response budget

**Keep it at `medium`.** By default, `get_tables` lists up to 500 tables, and query `sample` mode returns up to 50 sample rows. These are item limits, not fixed token or monetary caps.

To reduce AI usage, ask the agent to find relevant tables before requesting detailed columns. Start queries with summaries or samples. Use `tool_metrics` to inspect response sizes in bytes; actual fees depend on your client, model and usage. Enabling more MCP servers may add tool descriptions to the context, so enable only the connections you need in the client.

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
uv run pytest tests/unit
```

</details>

---

<div align="center">

[Report an issue](https://github.com/LukeStackLab/sqlserver-semantic-mcp/issues) · [MIT License](LICENSE)

</div>
