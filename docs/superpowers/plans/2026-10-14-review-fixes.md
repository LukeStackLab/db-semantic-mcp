# Second Review Corrections Implementation Plan

> **For agentic workers:** Use subagent-driven-development with scoped implementers, independent review and one final integration commit. Steps use checkbox syntax for tracking.

**Goal:** Resolve all eight confirmed findings from the second review of `1fe0580`.

**Architecture:** Tighten AST checks without adding source resolvers. Preserve snapshot identity and invalidate module caches atomically. Compute restricted classifications locally and keep exact-match ranking independent of score saturation.

**Tech Stack:** Python 3.11+, SQLGlot TSQL, asyncio, aiosqlite, MCP 1.x, pytest.

**Spec:** `docs/superpowers/specs/2026-10-14-review-fixes-design.md`.

## Global Constraints

- No response byte/cell hard limits and no TLS changes.
- No live SQL Server connections, database grant changes, Git push or external semantic services.
- Preserve unrelated modifications; only task-owned changes may be committed.
- Keep existing synchronous service and MCP APIs. Root owns the final commit; workers do not stage or commit.
- Base is `1fe0580` on the authorized feature branch `feat/semantic-safety-performance`.

## Review Focus

- SQLGlot exposes temporal/external properties even when root kind looks ordinary; reject opaque physical sources.
- Restricted definitions and queries must apply the same metadata-function classification.
- Concurrent refresh after a metadata read must not relabel old content as a new snapshot.
- Updating a module fingerprint baseline must not leave its cached definition ready with the old body.
- Classification cache reuse and saturated scores must not make responses depend on unrelated prior calls or lexical table ordering.

### Task 1: Reject opaque sources and missing metadata functions

**Files:** `policy/intents/ast_analyzer.py`, `policy/scope.py` if needed, `services/object_service.py` if needed; `tests/unit/test_policy_ast.py`, `tests/unit/test_object_service.py`.

**Interfaces:** Preserve `PolicyService.validate(sql, database='')` and the shared metadata function classification used by `_definition_allowed`.

- [x] Add regressions for readonly Users allow / UsersHistory deny with FOR SYSTEM_TIME ALL and AS OF; assert denial and no execution.
- [x] Add external TABLE/VIEW DDL cases with permitted CREATE and local target; assert denial despite matching target scope.
- [x] Add FileTableRootPath/STATS_DATE query and restricted definition cases; assert denial/withholding and preserve safe scalar queries.
- [x] Run new cases before implementation and capture failures, then implement conservative AST rejection and missing function classifications.
- [x] Run policy and object covering suites; write implementation report and request independent review.

### Task 2: Snapshot identity and module invalidation

**Files:** `infrastructure/cache/revalidation.py`, `infrastructure/cache/structural.py`, `server/app.py`, `server/resources/schema.py`; `tests/unit/test_revalidation.py`, `tests/unit/test_structural_cache.py`, `tests/unit/test_structured_protocol.py` as needed.

**Interfaces:** Preserve `refresh_now(cfg)`, `warmup_structural_cache(cfg, fingerprints=None)` and snapshot return hashes. Share cache-metadata merge logic between tools and resources.

- [x] Add an interleaving test where old-column metadata is read before a new snapshot is written; assert the response retains its old capture time, stale flag and correct age.
- [x] Add manual refresh tests where a same-name VIEW/PROC body changes; assert definition becomes dirty before the baseline advances and the subsequent describe refetches it.
- [x] Add startup/full-warmup and transaction rollback cases to prevent baseline/status divergence; retain single-flight and cancellation behavior.
- [x] Run the failing cases, implement one shared identity-preserving merge and transactional fingerprint invalidation for all full snapshots.
- [x] Run cache, resources, protocol and transaction covering suites; write report and request independent review.

### Task 3: Consistent scoped classification, index metadata and ranking

**Files:** `services/semantic_service.py`, `services/metadata_service.py`, `workflows/discovery_flow.py`, `infrastructure/queries/metadata_queries.py`; `tests/unit/test_semantic_service.py`, `tests/unit/test_metadata_contract.py`, `tests/unit/test_discovery_search.py`, `tests/unit/test_constraint_relationships.py`.

**Interfaces:** Preserve classification schemas, full describe index output and `discover_relevant_tables(..., limit=10, policy=None)`.

- [x] Add a scope-order regression: T with two denied FK targets remains identically classified before/after classification resource calls; assert no cross-scope cached result reuse or poisoning.
- [x] Cover summarize_for_joining, detect_lookup_tables and JOIN scoring with the same authorized FK subset, and unrestricted cache reuse.
- [x] Preserve XML/spatial indexes with key_ordinal=0 while keeping uniqueness evidence limited to actual keys.
- [x] Add saturated-score discovery with goal `sales.Orders customer status total amount`, limit=1; assert sales.Orders wins over aaa.CustomerStatus.
- [x] Run failing cases, implement shared scope-aware classification and a separate exact-match sort tier, then run covering suites and report for review.

### Task 4: Independent acceptance and final commit

**Files:** README language variants; the two current design/plan documents.

- [x] Independently review all three task diffs; route any finding back to its owner and re-review only fixes.
- [x] Update README compatibility notes for conservative temporal/external handling and corrected cache/index/discovery behavior.
- [x] Run Python 3.11 and 3.13 full unit suites, compilation, lock check, wheel build and whitespace checks.
- [x] Complete final independent review, stage only task-owned paths and commit without pushing.

## Execution record

- Prior review reproduced eight findings; initial working tree was clean at `1fe0580`.
- Authorization to execute the eight proposed fixes comes from the user's explicit request, "請修正！"; no further design permission is needed.

- Task 1: RED 18 failures; GREEN 208 covering tests. Independent review approved, including extra opaque-source/scalar cases.
- Task 2: RED 7 failures; GREEN 92 covering tests. Independent review approved; extra rollback/None/False and dependency checks passed.
- Task 3: initial GREEN 92 tests; independent review found a policy-reload cache write race. The fix adds fresh scope guards and six reload regressions; GREEN 98 tests and scoped re-review approved.

- Integrated checkpoint before final corrections: Python 3.11 and 3.13 each passed 546 tests. The final test record below supersedes this checkpoint.
- No live SQL Server, database grants, TLS changes, response byte/cell hard limits or Git push were used.

- Final independent review identified an acceptance gap: classification/list responses lacked snapshot identity and still inherited latest freshness. The final fix wave marks unknown content snapshots and reports latest cache state separately; seven interleaving/failure regressions passed and scoped re-review approved.

- Source authorization is rechecked after classification/column SQLite awaits. Ten additional database-only revocation regressions prevent shared-cache poisoning while preserving authorized database-only cache reuse.
- Eight VIEW/PROC empty/partial baseline commit/rollback cases cover legacy definitions without fingerprints. Added module fingerprints invalidate existing unverified definitions in the same transaction; unchanged modules retain their cache.
- Final scoped re-review approved all three supplemental findings with no new blocking issues.
- Final verification: Python 3.11, 571 passed in 5.05s; Python 3.13, 571 passed in 3.34s; compile, lock consistency, whitespace and wheel build passed. Existing setuptools license metadata deprecation warnings remain outside scope.
- Completion commit contains only the 24 reviewed task-owned source/test/README/design/plan files; no push or live SQL verification was performed.
