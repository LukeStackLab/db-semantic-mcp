# sqlserver-semantic-mcp

[English](README.en.md) | [繁體中文](README.zh-TW.md)

Give AI agents MCP tools to explore SQL Server, find table relationships and query data within configured permissions.

- **Reuse database context**: cache schema metadata and semantic analysis to reduce repeated database reads.
- **Keep responses compact**: start with summaries and request columns, indexes or query samples when needed.
- **Isolate connections**: each MCP process has its own account, permissions, response budget and cache. New profiles are read-only by default.

Supports Codex, Claude Code, Claude Desktop and Cursor.

## Quick installation

Prepare Git, a reachable SQL Server 2017+ instance and a SQL username/password. The wizard currently uses SQL authentication. The account needs the required data-read and metadata permissions.

### 1. Install uv

macOS / Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows PowerShell:

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

## Configure a connection with the wizard

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

Reload MCP settings or restart the client. Claude Code still handles project trust prompts. Claude Desktop default settings paths are supported on macOS and Windows.

## Create and use multiple connections

Give each connection a different name. Create all five below in one wizard session, or run `setup` again later. Every new profile defaults to the `medium` response budget.

| MCP name | Example use | Permissions |
|---|---|---|
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

Only decide whether limited writes are needed:

| Setting | Allowed operations |
|---|---|
| Read-only, default | `SELECT` |
| Limited writes | `SELECT`, one `INSERT ... VALUES`, or one `UPDATE` with `WHERE` |

Limited writes exclude `DELETE`, DDL, `EXEC`, write subqueries, CTEs and `OUTPUT`. New profiles default to 1,000 returned rows, 100 affected rows and a 30-second query timeout. Writes exceeding the affected-row cap are rolled back.

MCP policies do not grant SQL Server permissions. Use a dedicated account for each purpose and enforce read-only access in the database for production connections. The wizard checks login and metadata access; it does not perform writes to test permissions.

**Keep the response budget at `medium`.** By default, `get_tables` lists up to 500 tables, and query `sample` mode returns up to 50 sample rows. These are item limits, not fixed token or monetary caps.

To reduce AI usage, ask the agent to find relevant tables before requesting detailed columns. Start queries with summaries or samples. Use `tool_metrics` to inspect response sizes in bytes; actual fees depend on your client, model and usage. Enabling more MCP servers may add tool descriptions to the context, so enable only the connections you need in the client.

## Profile storage and maintenance

Each profile lives in `connections/<MCP name>/` and contains `connection.json`, `policy.json` and a separate cache.

| Platform | Default profile root |
|---|---|
| macOS | `~/Library/Application Support/sqlserver-semantic-mcp/connections/` |
| Windows | `%APPDATA%\sqlserver-semantic-mcp\connections\` |
| Linux | `$XDG_CONFIG_HOME/sqlserver-semantic-mcp/connections/`; defaults to `~/.config/sqlserver-semantic-mcp/connections/` |

Passwords are stored in the system keyring when available. On macOS / Linux, an unavailable keyring can be replaced by a plaintext credential file only with explicit consent, using directory mode `0700` and file mode `0600`. Windows uses the system keyring. Passwords are never written to client settings or `connection.json`.

- **Update a connection**: rerun `setup`, enter its existing MCP name and confirm the update. Reload the client afterward.
- **Check a connection**: replace the placeholder below with the full `connection.json` path printed by the wizard.

```bash
sqlserver-semantic-mcp doctor --config "PATH_TO_CONNECTION_JSON"
```

Client entries use the Python environment created during installation. If you move or remove it, reinstall and rerun `setup`. To update an installed package, run `uv tool install --python 3.11 --reinstall .` from the updated source directory.

Existing environment-variable and `.env` startup remains available; see [`.env.example`](.env.example). Fixed profiles created by the wizard ignore cwd `.env` and inherited `SEMANTIC_MCP_*` overrides.

## Development and license

```bash
uv sync --locked --extra dev
uv run pytest tests/unit
```

[Report an issue](https://github.com/LukeStackLab/sqlserver-semantic-mcp/issues) · [MIT License](LICENSE)
