# DB Semantic MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver db-semantic-mcp 0.8.0 with SQL Server and Oracle, unified safe tooling, installation and migration.

**Architecture:** Keep shared MCP services/workflows/SQLite caching. Introduce backend selection and Oracle-specific catalog, strict dialect authorization and synonym resolution. Source contexts isolate local/remote state without rewriting caller SQL.

**Tech Stack:** Python 3.11+, MCP 1.27+, Pydantic 2, pymssql, optional python-oracledb, SQLGlot 30.22.x, aiosqlite, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-10-15-db-semantic-mcp-design.md`

## Global Constraints

- User explicitly delegated all design/implementation choices; no approval pauses. Do not publish, push or rename remote GitHub repository.
- Work only in the authorized checkout on feat/db-semantic-mcp. No subagents from implementers. Commit only task files; never stage all indiscriminately.
- Read relevant spec sections. Write full reports to the provided report path; return status, commits, concise test result and concerns.
- Distribution lukestack-db-semantic-mcp, product/CLI db-semantic-mcp, module db_semantic_mcp, version 0.8.0; no old CLI/module shim. Leave historical docs unchanged.
- Runtime Config neutral fields: server, database, user, password, port, windows_auth, encrypt. Env prefix SEMANTIC_MCP_. Keep defaults/caps/policies and 12 tools.
- Read-only default, opt-in INSERT VALUES / UPDATE WHERE only. Oracle 19c+, Thin default and configurable Thick. No PL/SQL execution/DDL/DELETE/unknown opaque queries.
- Preserve all existing tests' semantic coverage; adjust names mechanically, do not delete failing tests to ship.
- Existing uv.lock changes saved under .superpowers/sdd/db-semantic-mcp-initial. Tasks 1-4 leave it unstaged. Final task independently regenerates a valid lock for renamed product and dependencies, preserving applicable resolutions.
- Oracle live tests opt-in only. Never echo secrets, inspect credential contents unnecessarily or run writes against unknown user DBs.

## Review Focus

- Parallel remote/local requests must not leak active source through global state; Task 4 tests context isolation.
- Quoted names, public/private synonym shadowing, chains/cycles and dotted DB links must not broaden scope; Task 3 tests them.
- Unknown remote write commit/rollback must not encourage duplicate retry; Task 3 tests failures.
- Credential/Client migration must preserve newer concurrent edits and original secrets; Task 4 tests rollback/dry-run/conflict.
- Bounded LOB reads and optional driver import must hold before connection close and in driver-less installs; Task 2/5 test these.

---

### Task 1: Rename product and neutralize existing runtime

**Files:** Rename sqlserver_semantic_mcp/ to db_semantic_mcp/; modify pyproject.toml, tests imports/fixtures, .env.example, active README references as needed. Historical docs remain unchanged.

**Interfaces:** Produces the existing implementation under db_semantic_mcp, Config.server/database/user/password/port/windows_auth/encrypt and SEMANTIC_MCP_* neutral env names; existing services consume these directly. Keep build_pymssql_kwargs API intact. New product version 0.8.0 and sole new CLI; distribution lukestack-db-semantic-mcp avoids the unrelated existing PyPI project. Keep actual GitHub repository URLs until external rename. No Oracle behavior in this task.

- [x] Update CLI/import assertions first and demonstrate missing new module failure.
- [x] Mechanically rename namespace, distribution strings, credential service/config root/server name, runtime config fields and test usages. Do not accidentally rename pymssql library or backend-specific function names. Old product names only in migration/history contexts.
- [x] Run the complete existing unit suite through .venv/bin/python -m pytest to avoid lock changes; fix genuine integration regressions.
- [x] Verify pyproject entry point, version, CLI help and test baseline. Report exact commands/results.
- [x] Commit the coherent rename excluding uv.lock; write task report.

### Task 2: Backend boundary, Oracle configuration and catalog

**Files:** db_semantic_mcp/config.py, profiles.py, infrastructure/connection.py, new infrastructure/backends/{__init__,base,sqlserver,oracle,oracle_catalog}.py (split further if needed), infrastructure/cache/{structural,probe}.py, services/metadata_service.py, pyproject.toml; tests/unit/test_oracle_backend.py and test_oracle_catalog.py plus config/profile tests.

**Consumes:** Task 1 neutral Config and namespace; existing StructuralSnapshot (12 column fields, 11 FK fields, 8 index fields) and SchemaFingerprints. Preserve these contracts for SQL Server.
**Produces:** Config.database_type='sqlserver'|'oracle', Config.oracle (validated OracleOptions), Config.sources mapping source ids to RemoteSource(db_link,readonly), Config.source_id='local', Config.source_dblink=None and Config.for_source(source_id) isolated cache clone. Profile v2 with same common fields and oracle/sources; read_profile rejects v1 with migrate hint (Task 4 migration parses legacy explicitly). backend_for(cfg) exposing connect, fetch_snapshot, fetch_fingerprints, get_object_definition, get_object_dependencies, describe_result_schema and capabilities. open_connection/fetch helpers dispatch by backend. Native Oracle type information retained. Oracle driver optional extra oracledb>=3.4,<27, lazy import; SQL Server remains installed by default for backward usability. Both driver modes use bounded sync workers.

- [x] Test Oracle config exclusivity/default ports/DSN/mode, fixed profile env isolation and unknown-field rejection; source clones do not share cache paths.
- [x] Implement backend selection without eager Oracle import. Thin connect uses oracledb.connect; explicit Thick initializes once consistently and never silently changes mode. Connect timeout and per-call timeout configured with truthful semantics.
- [x] Implement ALL_* catalog queries with bind variables; source db_link interpolation only from validated identifier grammar AND catalog-confirmed configured route. Include comments, composite constraints, indexes, objects/packages/synonyms, static definitions/deps and snapshot fingerprints. No DBA grants required by default; remote limitations explicit.
- [x] Normalize constraint/index evidence conservatively (invalid/not validated/deferrable/function-based cannot prove trustworthy unique cardinality). Preserve Oracle native type, precision/scale/char semantics and timezone. Keep SQL Server snapshot schema and all baseline tests intact.
- [x] Backend value conversion reads bounded LOB prefix while connection remains open; Decimal preserved, never floats. SELECT-only result description must not parse DDL. Expose provider capabilities for tool integration.
- [x] Test fake-driver calls, bindings, catalog tuple shape, native types, fingerprints changing on comments/constraints, unsupported remote metadata, optional imports and LOB caps. Run complete unit suite.
- [x] Commit task files excluding uv.lock and write report including exact backend signatures and OracleOptions fields for Task 3/4.

### Task 3: Strict Oracle policy, synonym resolution and query execution

**Files:** policy/{models,scope,enforcer,readonly_guard}.py, policy/intents/{oracle_analyzer,ast_analyzer,router}.py, services/{policy_service,query_service,object_service}.py, backend Oracle resolution helpers; tests/unit/test_oracle_policy.py, test_oracle_resolution.py, test_oracle_query.py.

**Consumes:** Task 2 Config/backend interfaces; existing SQL Server AST remains independently strict.
**Produces:** Provider-selected authorization analyzer; ObjectReference extended with source identity without breaking local SQL Server. PolicyScope remote source scope map (typed, extra=forbid). PolicyService resolves each physical reference to configured source and actual synonym targets using same DB session as execution where possible; remote routes explicitly authorized. Oracle object service uses catalog definitions/static deps, not T-SQL regex. All denial hints dialect-appropriate.

- [x] Add adversarial tests: quoted/unquoted Oracle names; nested CTEs; DB link full/dotted names and aliases; public/private synonym shadowing, chains, inaccessible target/cycles/retargeting; scoped functions; q-quoted literals; NEXTVAL, FOR UPDATE, PL/SQL, RETURNING, multiple statements and unknown syntax denied.
- [x] Implement strict Oracle AST authorization, identifier normalization preserving quote semantics, SQL keyword guard that understands Oracle literals, source authorization and synonym resolver with bounded depth/cycle detection. Remote source definitions alone do not grant permission. Enforce alias/target denials and operation permissions for every touched source. Never use stale snapshot alone to approve synonym-based SQL.
- [x] Keep simple INSERT VALUES / UPDATE WHERE including remote targets; reject custom/remote functions or unproven sources even in otherwise SELECT SQL. Restrict builtins explicitly; do not claim complete PL/SQL analysis.
- [x] Route result preflight/type conversion through backend; perform authorization before driver execution or parse. Never rewrite caller SQL or auto-append @link. Use explicit transaction ownership and existing strict affected-row cap. Return transaction_state unknown and automatic_retry_allowed false for ambiguous remote writes or commit/rollback errors. Redact passwords and wallet secrets.
- [x] Implement Oracle describe_object for VIEW/PROCEDURE/FUNCTION/PACKAGE/PACKAGE BODY/SYNONYM; keep truthful static dependency/IO completeness. Always withhold opaque PL/SQL definitions because unknown remote dependencies cannot be authorized; retain scoped static dependencies and IO unknown. Reveal VIEW definitions only after live proof; withhold unproven MATERIALIZED VIEW definitions. Never parse PL/SQL with TSQL analyzer.
- [x] Run focused security tests and full unit suite. Commit task files; write report with source-policy JSON schema and resolution APIs for Task 4.

### Task 4: Source-aware tools, setup, doctor and migration

**Files:** server/app.py, server/tools/{metadata,semantic,object_tool,relationship,cache,policy,workflow}.py, server/resources/schema.py, setup/{wizard,checks,clients}.py, new setup/migration.py, main.py, credentials.py, profiles.py, config.py; tests/unit/test_source_context.py, test_migration.py and existing setup tests.

**Consumes:** Task 2 source clones/backends and Task 3 source policy/resolution APIs.
**Produces:** Shared discovery tools optionally select source; request-safe source-specific Context; remote lazy single-flight init; source metadata/capabilities in responses; provider-aware setup/doctor; db-semantic-mcp migrate CLI with dry-run and legacy source dir/client path arguments. New runtime v2 only, migration reads v1 explicitly.

- [x] Add concurrent local/two-remote context tests, source schema advertisement/default local, unknown source rejection, no SQL rewriting and resources respecting source scope.
- [x] Implement ContextVar-bound effective config/context (never mutate process singleton for requests), source cache init/warmup single-flight, request-scoped policy, tool/profile output contracts and package/synonym type enums. SQL tools execute original SQL through base local session and resolve every source; source selector for exploration does not imply SQL routing. Metrics remain bounded/no secrets.
- [x] Adapt wizard to select database and provider-specific options, including explicit Thick paths, Oracle service/SID/TNS, TLS/wallet credentials and optional catalog-validated DB link source selection; retain original SQL Server wizard behavior and transactional registration safeguards. Fixed profile database_type cannot silently change on update.
- [x] Doctor validates actual DB/mode/catalog/configured sources, reports per-source capability failures without echoing driver secrets, never writes user DB data. MCP startup failures remain clean stdio.
- [x] Implement migration preview then explicit application through migrate command: preserve names, scopes, caps; copy legacy credentials using old service name only within migrator; new root/profile/credential refs; backup and ownership-aware rewrite only positively identified old Client entries. Do not delete old profiles/cache/credentials or overwrite unrelated entries/newer writes. Roll back owned writes on failure. Test dry-run/no secrets/concurrent conflicts/invalid policy/existing destination and credential failure.
- [x] Full unit suite and actual subprocess MCP handshake using fake/configured-no-DB fixture where appropriate. Commit task files; write report.

### Task 5: Deliverable documentation, live matrix and packaging validation

**Files:** README.md, README.zh-TW.md, README.en.md, CHANGELOG.md, .env.example, config/*.example.json, tests/integration/*, new docs/testing-oracle.md, pyproject.toml/uv.lock as needed. Fix residual integration/packaging issues across product with tests.

**Consumes:** Completed product; existing pre-task uv.lock snapshot. **Produces:** installable wheel and source distribution, valid reproducible lock, documented SQL Server/Oracle/MCP setup and migration. Optional live fixtures for local Oracle and separate Oracle DB link, opt-in write/rollback checks only against disposable DBs.

- [ ] Update all active docs to db-semantic-mcp and both providers, preserving actual feature limits. Include SQL Server and Oracle v2 examples with remote scope configuration, Thin/Thick, service/SID/TNS, native @link SQL, synonym authorization, static PL/SQL limitations, per-roundtrip timeout, LOB truncation, unknown transaction handling and migration command. Historical docs stay unchanged.
- [ ] Add opt-in Oracle read/catalog/native-type/LOB/synonym/DB link/live write cap tests and fixture SQL for disposable schemas/two databases. Without explicit environment, live tests must skip; report skips honestly. Never infer live validation from mocks.
- [ ] Independently regenerate lock from current pyproject (do not silently discard preexisting applicable dependency resolutions); compare original snapshot and record integration. Ensure base and [oracle] installs and driver-less CLI/help work with lazy imports.
- [ ] Run full pytest, ruff check and format check, uv lock --check, build wheel/sdist, inspect entry points and install wheel in clean temporary env. Smoke test CLI help/version and stdio initialization with isolated test fixture. Do not run live operations without explicit disposable opt-in.
- [ ] Commit only task changes. Write report with exact counts, skipped live matrix and remaining verified limitations. Mark plan completed only for actually delivered requirements; unresolved gaps must be fixed before final review or explicitly justified against the spec.
