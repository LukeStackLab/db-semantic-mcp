# Semantic MCP Hardening Implementation Plan

> **For agentic workers:** Execute this authorized plan task by task with scoped implementation agents and independent final review. Steps use checkboxes for progress.

**Goal:** Improve authorization, join accuracy, query usefulness, async responsiveness, and task-level context efficiency.

**Architecture:** TSQL AST and shared scope checks protect existing services. Constraint-aware metadata feeds safe join candidates. Bounded workers and versioned local caches support compact, typed MCP responses.

**Tech Stack:** Python 3.11+, SQLGlot TSQL, pymssql, asyncio, aiosqlite, MCP 1.x, pytest.

**Spec:** `docs/superpowers/specs/2026-10-13-semantic-hardening-design.md`.

## Global Constraints

- Exclude response byte/cell hard limits and TLS behavior changes.
- Preserve `.npp-md-preview.html` and all unrelated modifications.
- No live database connections, database grant changes, Git push, or external services for semantic search.
- Keep legacy configuration readable; migrate existing SQLite stores.
- Stage and commit only this task's changes after verification.

## Review Focus

- AST grammar acceptance must not permit unknown/remote/control SQL or lose CTE/DML targets.
- Compound keys and parallel relationship roles must remain distinct after cache migration.
- Cancelled async work must still consume a concurrency slot until its DB lifecycle ends.
- Cached or indirect metadata must not reveal objects outside the current policy.
- Structured output must preserve meaningful null/false and report failures to MCP clients.

### Task 1: SQL authorization and shared scope

**Files:** policy analyzer/enforcer/intents/loader; services/policy_service.py; pyproject.toml; uv.lock; policy tests.

**Interfaces:** `PolicyService.object_allowed(schema, name, database='') -> bool`; `authorize_object(...) -> None`; `policy.scope.object_allowed(scope, database, schema, name) -> bool`.

- [x] Add failing scope, batch, CTE, alias and malformed-policy regression tests.
- [x] Add a bounded SQLGlot dependency; implement strict TSQL authorization and shared object scope checks.
- [x] Run policy/readonly/profile tests; review accepted grammar against adversarial cases.

### Task 2: Constraint-aware metadata and graph

**Files:** metadata_queries.py; cache/store.py and structural.py; relationship_service.py; server/tools/relationship.py; relationship/cache tests.

**Interfaces:** FK rows append constraint ID/name/ordinal to legacy six columns; graph edges expose `column_pairs`, `constraint_name`, completeness; `find_join_paths(..., limit=5)` provides alternatives.

- [x] Add failing same-name-schema, compound, parallel-role, cardinality and old-cache tests.
- [x] Migrate FK/type metadata and implement grouped graph edges and bounded ambiguity-aware alternatives.
- [x] Run structural, relationship, semantic and joining tests.

### Task 3: Useful query results and bounded workers

**Files:** query_service.py; query_flow.py; server/tools/query.py; new infrastructure/workers.py; object_service.py; cache/revalidation.py; background.py; cache/semantic.py; runtime tests.

**Interfaces:** `run_db_work(cfg, callable, *args, **kwargs)` owns worker capacity; summary includes at most 3 result rows; explicit returned/total count fields; retry state in sem_table_analysis.

- [x] Add failing scalar/count, strict-cap, heartbeat, cancellation, single-flight and retry-fairness tests.
- [x] Implement bounded full DB lifecycles, honest result contracts and fair retry scheduling.
- [x] Add snapshot max-age freshness; run query/background/cache/object tests.

### Task 4: Authorized metadata and local discovery

**Files:** metadata_service.py; semantic_service.py; tools/metadata.py; resources/schema.py; discovery_flow.py; facade.py; bundle.py; shape.py; discovery/metadata/scope tests.

**Interfaces:** service entry points accept `policy=None` for trusted internal callers; all MCP entry points pass current policy. `describe_table(..., detail='full', columns=None, policy=None)` supports minimal SQL context.

- [x] Add failing denied-object, indirect-FK/resource, Chinese/alias/description and brief-query-count tests.
- [x] Apply shared scope consistently; add typed selected-column contexts and deterministic local search.
- [x] Run tool filters/shapes, resource, discovery and semantic tests.

### Task 5: Tool profiles, MCP contracts and measurement

**Files:** config.py; profiles.py; tools/__init__.py; prompts; app.py; metrics_service.py; tools/metrics.py; protocol/profile/metrics tests.

**Interfaces:** named `tool_profile` aliases; fixed profile selection; object output schema and CallToolResult; duration/error/effective-mode metrics.

- [x] Add failing named-profile, protocol structured-output, failure-metric and compact-value tests.
- [x] Implement profile-aware prompts, structured tool results and migrated metrics.
- [x] Run protocol and metrics tests; measure descriptor sizes and representative response changes.

### Task 6: Integration, documentation and review

**Files:** README.md, README.zh-TW.md, README.en.md; .env.example; test fixtures and optional integration cases.

- [x] Update behavior/security/compatibility guidance and state excluded work clearly.
- [x] Run full unit suite, compile check and diff whitespace check.
- [x] Independently review changed scope, fix findings and re-run covering tests.
- [x] Stage only task-owned paths, commit verified work, report short hash and live-test limits.

## Execution record

- Baseline: 287 unit tests. Initial integrated suite: 467 passed; subsequent fixes retain covering tests.
- Task 1: AST/scope implemented; independent review found hidden DELETE aliases, CREATE OR ALTER privilege checking and metadata-function scope. All fixed and scoped re-review clean.
- Task 2: constraint-aware graph implemented; independent review found disabled target unique-index assumptions. Fixed with five regression cases and scoped re-review clean.
- Task 3: workers/query/cache/object implemented; worker cancellation, shutdown, preflight, retry fairness and restricted definition cache have regression coverage.
- Root integration: independent review identified and fixed freshness diagnostics hiding committed writes, resource-only TTL, full/bundle compound-key evidence, fetched-row metric naming and old classifier-version invalidation.
- Ruling: summary/sample use a mode-specific fetch limit, rather than fetching 1,001 rows merely to report a bounded page count. This changes the legacy row_count interpretation for those modes; explicit scope/limits/completeness fields and README guidance prevent it being mistaken for a full count. Cost if wrong: clients relying on the old bounded count must use rows/count_only or explicit COUNT_BIG.
- Ruling: preserve allow-entry case and reject case variants in deny entries while DB collation is unknown. Cost if wrong: legitimate case-insensitive requests may be rejected and require exact configured spelling.
- Descriptors measured with MCP 1.27: all 12 tools 8,429 bytes; query 2 tools 1,870; explore 8 tools 6,247; maintenance 3 tools 1,271. These are JSON bytes, not model tokens. Full contracts add context; named profiles provide the reduction.
- Scalar summary probe: fetchmany request 1,001 -> 4, with the exact aggregate value retained. Warm brief metadata service now uses one SQLite connection and skips indexes/comments; dispatcher metrics/freshness use their own connections.
- Optional integration checks require SEMANTIC_MCP_DISPOSABLE_SQL_TESTS=1; no live SQL Server tests were run.

- Final whole-branch review found two Important issues: nested/cross-database module-target scope exceptions and manual/background snapshot concurrency. Both are fixed with regression tests. The independent scoped re-review passed 76 tests and found no remaining blocking issues.
- Transaction outcome review additionally preserves confirmed commits after cleanup failures and reports uncertain commit/rollback acknowledgements with retry prohibited.
- Manual full refresh now waits for an existing probe, coalesces concurrent callers, and survives waiter cancellation. Six regressions verify serialized warmup, one manual snapshot for eight callers, and preserved failure/freshness reporting.
- Final unit suites: Python 3.11, 488 passed in 3.47s; Python 3.13, 488 passed in 2.33s. Compile, uv lock consistency, whitespace checks, and wheel build passed. The wheel build emitted existing setuptools license metadata deprecation warnings.
- Fake-driver responsiveness probe with a 200 ms operation: maximum heartbeat gap decreased from 205.19 ms to 11.25 ms with bounded workers. This is a local scheduling measurement, not a SQL Server throughput benchmark.
- Completion commit contains only task-owned source, tests, configuration, lockfile, README files and these two design/plan documents. No Git push, live database test, or TLS change was performed.
