# Semantic MCP second-review corrections

## Authorized scope

The user approved fixing all eight findings from the second review of commit `1fe0580`. Preserve the original exclusions: no response byte/cell hard limits and no TLS changes. Do not connect to a live SQL Server, change database grants, push Git, or touch unrelated modifications. Keep the existing synchronous service and MCP APIs.

## SQL authorization

- Reject temporal table versions until the physical history table can be resolved and authorized. The conservative restriction applies to SQL execution and restricted object-definition disclosure.
- Reject external DDL properties rather than authorizing them as ordinary local tables. No external source resolver is added.
- Classify `FileTableRootPath` and `STATS_DATE` as metadata functions. SQL and restricted definition checks share the same classification. Preserve ordinary scalar queries.

## Cache correctness

- When a response carries snapshot identity, its `captured_at`, `stale`, and age describe that content snapshot. Current refresh/failure state must not overwrite it. Responses without identity explicitly set `snapshot_unknown=true`; current persisted freshness is separately named `latest_snapshot`, not attached as the content capture time. Apply the same merge behavior to tools and resources, including arrays and freshness failures. This correction does not introduce read transactions across existing multi-SELECT service reads.
- Every full snapshot update compares persisted fingerprints before replacing their baseline. Changed module bodies and dependent definitions become dirty in the same SQLite update, including explicit/manual refresh and startup warmup. A failed update must leave the old baseline and definition status intact. When a module has an existing cached definition but no persisted fingerprint, its first fingerprint also invalidates that unverified definition.

## Metadata and discovery

- Classification uses the authorized FK subset consistently across describe, classification, joining summaries, lookup discovery and JOIN scores.
- A scope-dependent classification must not overwrite or reuse a classification from a different scope. Restricted callers may compute without persisting; unrestricted cached results remain reusable. Source authorization is rechecked after classification/column reads so a database-only policy revocation cannot write a scope-filtered result into shared analysis.
- Preserve XML/spatial index metadata that the preceding query returned. Uniqueness inference must use actual key evidence. Do not expand this correction into previously excluded columnstore display work.
- Qualified exact matches sort ahead of ordinary heuristic matches, even when scores saturate at 1.0 and candidate limits truncate the result.

## Acceptance

Regression tests reproduce each finding using fake drivers and temporary SQLite stores. Add interleaving snapshot/freshness tests, manual-refresh definition invalidation tests, scope-sensitive cache-order tests, and exact-match candidate truncation tests. Run covering tests, independent review, then all unit tests on Python 3.11 and 3.13. Check compilation, lock consistency, whitespace and wheel packaging. Commit only reviewed task files after completion.
