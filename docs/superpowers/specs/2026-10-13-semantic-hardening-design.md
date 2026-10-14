# Semantic MCP safety, accuracy, and execution design

## Authorized scope

Implement recommendations 1, 2, 3, 4, 6, 7, 9, 10, 11, and 12 from the repository review. The user explicitly excluded recommendation 5 (response byte/cell hard limits) and recommendation 8 (TLS certificate validation). Do not change TLS behavior or introduce response byte limits. Preserve unrelated `.npp-md-preview.html`. Do not contact a live SQL Server, change database grants, or push Git.

Baseline: v0.6.0, commit `31b34e5`, 287 unit tests passing. Existing configuration files remain readable; old SQLite caches migrate in place. Security fixes may reject SQL that previously passed incomplete validation. No claim that AST parsing or policy approval proves business correctness.

## SQL authorization

Use SQLGlot's TSQL AST for authorization, independent of the optional regex routing analyzer. Inspect all statements and physical object references, including subqueries and write targets. Resolve CTE scope and DML aliases. Reject unparsed commands, transaction control, dynamic SQL, and references whose scope cannot be determined. Reject four-part/remote sources until supported explicitly. Check database, schema, and object names consistently, including bracketed and quoted identifiers. Do not rewrite caller SQL for execution. Statement count and operation constraints apply to every statement. Fixed write profiles retain their simple INSERT VALUES / UPDATE restrictions.

Shared scope helpers govern SQL, metadata tools, resources, graph traversals, definitions, and dependencies. Database credentials remain the final enforcement boundary. Explicit legacy policy load failures must fail closed; an omitted policy may use built-in readonly. Callers cannot disable a configured strict affected-row cap.

## Relationship and metadata correctness

Read foreign keys through SQL Server object IDs, preserving constraint ID/name and column ordinal. Store each constraint as one graph edge containing all column pairs. Legacy rows without constraint identity are not sufficient evidence for a complete compound join and must be marked as such until refresh. Return bounded alternative paths and ambiguity for parallel FK roles; never recommend execution from a heuristic score alone. Infer cardinality only with uniqueness/nullability evidence.

Preserve numeric precision/scale, datetime precision, and collation. Allow describe_table to return only requested columns while retaining necessary key evidence. Provide explicit omission markers. Brief metadata should avoid index/comment reads and reuse structure for classification in one SQLite connection.

## Query result contract

Keep `row_count` as an observed retained-count alias, explicitly marked `row_count_scope="fetched"`. Add `returned_row_count` for rows actually included in the payload and `total_row_count` (null when unknown); never report a bounded count as an exact total. Summary fetches up to 4 rows and retains at most 3; larger results include completeness/omission markers. `truncated` means additional results remain beyond the mode-specific fetch limit, which is returned separately from the policy max_rows limit. Exact counts come from explicit COUNT/COUNT_BIG SQL, not automatic expensive rewrites. Sample fetches respect its requested sample size; row limits remain distinct from database execution cost.

Optionally validate result metadata with SQL Server's first-result-set describe function after AST/policy validation. Expose this as an explicit opt-in query option, with parameters passed separately. Do not execute user EXEC to implement it and do not treat it as proof of business semantics.

Preserve a confirmed commit even if cursor or connection cleanup fails. If commit or rollback acknowledgement is uncertain, return `transaction_state="unknown"`, `automatic_retry_allowed=false`, and `next_action="verify_transaction"`; do not claim rollback or recommend automatic retry.

## Async execution and cache

Execute complete synchronous connection lifecycles in bounded workers. Never share a pymssql connection between workers. Concurrency defaults to 4 per process and remains configurable. Cancellation must not release a worker slot before its synchronous operation finishes. Keep existing per-profile process isolation and timeout semantics.

Maintain one in-flight schema revalidation per cache/database. Track comment freshness with a full-snapshot maximum age of 300 seconds, configurable. Responses expose capture time and stale/refreshing state. Background classification records attempts and next retry time; permanently failing items cannot starve the rest. No live SQL benchmark is implied by fake-driver tests.

Manual full refresh shares the same coordination: wait for an existing probe, coalesce simultaneous manual callers, then take one full snapshot. Cancelling a waiting caller must not cancel the shared task. A failed manual snapshot remains an explicit error and does not advance its capture time.

## Discovery and tool surface

Use deterministic local search over names, descriptions, and column metadata. Deduplicate tokens, prioritize qualified exact matches, support configurable business aliases plus common Traditional Chinese vocabulary. Do not send metadata to an external model or embedding API. Return a small candidate set with actual hit evidence. Index/cache search documents by metadata version when helpful.

Add named tool profiles (`all`, `query`, `explore`, `maintenance`) while retaining comma-separated groups. Fixed ConnectionProfile gains a validated `tool_profile` field, default `all`; setup registration retains existing behavior. Prompts must only recommend registered tools. Small successful query responses avoid redundant validation/route descriptions while detailed validation remains available.

## MCP and measurement

Advertise object-shaped output schemas; return structuredContent plus compact compatible JSON text. Tool execution failures use isError; preserve null/false values needed for result semantics. Tool annotations describe actual mutability and do not grant authorization.

Record elapsed duration, worker wait/DB timing where available, fetched rows, cache hit where observable, effective mode/budget, and failure category without recording SQL text, rows, passwords, or business question text. Metrics migration belongs at store initialization; avoid schema checks on every recorded call. Aggregate p50/p95 and errors; retain bytes as bytes, never call them exact tokens.

## Acceptance

Regression cases cover cross-database references, quoted identifiers, comma joins, CTE shadowing, alias UPDATE, hidden DML and transaction control; same-named constraints across schemas; compound and parallel foreign keys; tiny aggregate and truncated counts; cancellation and worker limits; failed-background-item fairness; comments-only freshness; scope parity across tools/resources; Chinese discovery; fixed-profile tool selection; structured outputs and failure metrics. Run all unit tests and hermetic protocol tests. Add optional live integration tests but do not run them without a disposable SQL Server.
