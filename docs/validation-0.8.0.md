# 0.8.0 validation evidence

Validated on 2026-10-16, macOS ARM64. Product/CLI: `db-semantic-mcp`; module: `db_semantic_mcp`; distribution: `lukestack-db-semantic-mcp==0.8.0`. No publication, push or repository rename was performed. Installation evidence uses local source/built artifacts, never the unrelated generic PyPI project.

## Automated checks

| Command | Result |
|---|---|
| `.venv/bin/pytest -q` | **841 passed, 14 skipped**, 3.54s, no warnings |
| `.venv/bin/ruff check .` | All checks passed |
| `.venv/bin/ruff format --check .` | 176 files already formatted |
| `uv lock --check` | 56 packages, valid |
| `uv build` | wheel + sdist built; no deprecation warnings |
| `git diff --check` | Passed |

The 14 default skips are explicit live tests: SQL Server 3 and Oracle 11. They do not imply live certification. The separate disposable runs below enabled the corresponding fixed profiles and explicit opt-in. The full suite ran on Python 3.13.15 with the lock's retained pytest 9.0.3 / pytest-asyncio 1.3.0 / ruff 0.17.0. Removing the redundant event_loop_policy fixture resolved the previous pytest-asyncio warning; warnings are not suppressed. Two preexisting test files received formatting only.

## Actual disposable server matrix

| Target | Product checks performed in this delivery | Result |
|---|---|---|
| SQL Server 2022, dedicated port 11433 | Decimal/result schema, catalog tuples, cache warmup | 3/3 live tests |
| Oracle 23.26.3.0.0 Thin TCP, dedicated port 11521 | Product native NUMBER/LOB/metadata/FK/comments, local/remote synonyms, multi-source SQL, actual MCP, native temporal rejection, local + distributed cap rollback | 11/11 live tests |
| Oracle 23.26.3.0.0 Thick TCP | Same matrix; FF9, numeric/region TZ and LTZ UTC preservation | 11/11 live tests |
| Oracle Thin TCPS, dedicated port 12484 | Product doctor direct and TNS, wrong DN rejection, TCP downgrade rejection | 4/4 checks |
| Oracle Thick TCPS | Same product doctor/transport matrix | 4/4 checks |
| Clean base wheel, Python 3.11 | CLI help/version, Oracle absent, actionable missing-driver doctor, actual SQL Server MCP query | Passed |
| Clean `[oracle]` wheel, Python 3.11, Thin TCP and TCPS | CLI, product doctor, actual MCP query | 2/2 cases |
| Clean `[oracle]` wheel, Python 3.11, Thick TCP and TCPS | CLI, product doctor, actual MCP query | 2/2 cases |

Each clean-wheel case initialized actual stdio MCP, listed all **12 tools**, and successfully called **plan_or_execute_query**. Oracle live MCP additionally exercised local/remote get_tables, chained synonym query, native `owner.table@link` query, opaque package metadata, full capability policy and PUBLIC remote-only target description. The remote fixture used a second independent Oracle database at host port 11522. Its DB link connected from the local database server to that remote node. The optional PUBLIC target owner was absent from local ALL_USERS. Tests used python-oracledb 26.0.1 and official ARM64 Instant Client 23.26.2 for Thick. Oracle TLS terminated in the actual database listener; a transparent port forward did not terminate TLS. Doctor observed `NETWORK_PROTOCOL=tcps`.

Live writes were separately opted in, confined to disposable fixture rows, exceeded cap one, returned rolled_back, and matched unchanged values afterward for local and DB link targets. No real user keyring/client/profile was changed. The existing user SQL Server on 1433 was not touched. Wallets, credentials, private keys and generated infrastructure state are not part of this repository or distributions.

Earlier independently approved integration evidence also covered wizard Thin/TCP + Thick/TCPS rows in one session, v1 SQL Server migration 6/6 including real MCP/client ownership preservation, local + two remote concurrent contexts, catalog shadow resistance 2/2 and TCPS MCP doctor 12 tools in both modes. Those are prior integration evidence, not additional tests counted in the 841 or 11-per-mode totals above.

## Reproduce

Provision disposable fixtures and fixed profiles using [testing-oracle.md](testing-oracle.md). Credential variables named in each profile must be set privately. Generic runtime environment settings cannot select a live target.

```bash
uv sync --locked --extra dev --extra oracle
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
uv lock --check
uv build

# SQL Server read/catalog/cache checks
SEMANTIC_MCP_DISPOSABLE_SQL_TESTS=1 \
SEMANTIC_MCP_TEST_SQL_PROFILE=/absolute/private/sql/connection.json \
.venv/bin/pytest -q tests/integration/test_live_contracts.py tests/integration/test_live_warmup.py

# Repeat in fresh processes with Thin and Thick profiles.
# Separate WRITES flag + writable profile are needed for both rollback cases.
SEMANTIC_MCP_DISPOSABLE_ORACLE_TESTS=1 \
SEMANTIC_MCP_DISPOSABLE_ORACLE_WRITES=1 \
SEMANTIC_MCP_ORACLE_PUBLIC_FIXTURE=1 \
SEMANTIC_MCP_TEST_ORACLE_PROFILE=/absolute/private/oracle/connection.json \
.venv/bin/pytest -q tests/integration/test_live_oracle.py
```

The installed-wheel smoke is committed as [check_installed.py](../tests/integration/check_installed.py). It exits on a failed assertion. It does not need pytest in the clean environment. Use the same disposable opt-in/profile variables and credential references:

```bash
uv venv --python 3.11 /tmp/dbmcp-base
uv pip install --python /tmp/dbmcp-base/bin/python ./dist/lukestack_db_semantic_mcp-0.8.0-py3-none-any.whl
/tmp/dbmcp-base/bin/python -I "$PWD/tests/integration/check_installed.py" base

uv venv --python 3.11 /tmp/dbmcp-oracle
uv pip install --python /tmp/dbmcp-oracle/bin/python './dist/lukestack_db_semantic_mcp-0.8.0-py3-none-any.whl[oracle]'
/tmp/dbmcp-oracle/bin/python -I "$PWD/tests/integration/check_installed.py" oracle
```

Set `SEMANTIC_MCP_TEST_ORACLE_PROFILE` separately for Thin/Thick and TCP/TCPS. Each command starts a fresh process. For TCPS rejection reproduction, use private copies of that profile with a wrong ssl_server_cert_dn, or a TCP DSN while encrypt remains true; doctor must fail. Contract tests additionally cover mixed-protocol DSN rejection. Never disable DN validation. No remote DB link encryption is inferred from local TCPS.

## Payload and packaging

`source` now retains database_type/id/database, optional connection/db_link/query_hint, and compact driver_mode/native_timestamp_max_precision/native_timestamp_timezone/remote_sources flags. Full narrative capabilities remain available once in get_execution_policy and doctor.

For an Oracle Thin local configuration (`service_name="demo"`, no profile name), `len(json.dumps(source, ensure_ascii=False).encode())` measured **1209 → 200 UTF-8 bytes**, an 83.5% reduction. Compact serialization with `separators=(",", ":")` measured **1165 → 186 bytes**. These are the same configuration/serializer before and after, excluding rows. The earlier integration handoff measured 1189 bytes under its own example; it is not mixed into this comparison. Focused budget tests retain precision/timezone and remote route hints while keeping the ordinary local envelope below 300 bytes.

The lock was solved independently in a temporary directory from final pyproject with explicit temporary constraints from the original pre-task lock, then normalized against the final pyproject without those constraints. All **54/54 applicable preexisting package versions** were preserved, including ruff 0.17.0. The old root distribution was replaced and oracledb 26.0.1 added. Build metadata uses SPDX MIT/license-files and setuptools>=77 to remove license deprecations. The clean wheel smoke intentionally resolves allowed current dependencies separately from the locked development environment.

Artifact inspection checks the exact 0.8.0 filenames. The wheel contains only the new runtime namespace plus dist-info and the `db-semantic-mcp = db_semantic_mcp.main:main` console script. Runtime cache modules are included. The sdist contains source, three READMEs, examples, portable tests/SQL fixtures, lock and validation documents. Neither artifact includes private workspace, runtime caches/credentials/wallets or the old Python namespace. Previously existing 0.7.0 artifacts were left untouched and were not used as evidence.

## Verified limits

Compatibility targets remain SQL Server 2017+ and Oracle 19c+. Exact SQL Server 2017/2019 and Oracle 19c servers were not tested. LDAP/current-user DB links were not certified; fixed-user/other tested principals do not establish LDAP compatibility. Real distributed in-doubt failure/recovery was not induced; unknown commit/rollback outcomes are covered by contract failure injection and are never automatically retried.

Opaque PL/SQL definitions are withheld; only authorized metadata/static dependencies are exposed with unknown IO completeness. Thin native high/unknown precision TIMESTAMP/TZ/LTZ is rejected; TO_CHAR is required or explicit Thick. Remote LOB behavior is driver/version dependent. Oracle timeout is per round trip, not a total deadline. Client cancellation cannot abort a running synchronous driver and may be followed by completed writes. Verify uncertain outcomes before manual retry. DB link transport remains database-managed and is not certified end-to-end TLS.
