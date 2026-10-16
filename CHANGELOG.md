# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.0] - 2026-10-16

- Rename the product/CLI to db-semantic-mcp, module to db_semantic_mcp, and distribution to lukestack-db-semantic-mcp. This release is source/wheel only; no PyPI publication.
- Add optional Oracle Thin/Thick backend, native precision/LOB handling, scoped DB-link sources, synonym proof and static catalog metadata. Preserve SQL Server support.
- Add v2 provider profiles, Oracle-aware setup/doctor, mandatory TCPS DN validation, policy snapshots and ownership-aware v1 migration.
- Keep opaque PL/SQL definitions withheld; unknown transactions require outcome verification before manual retry. Oracle timeout is per round trip.
- Keep response provenance compact; full capabilities are available in get_execution_policy/doctor. Add CLI --version and actionable optional-driver diagnostics.
- Add explicit disposable live fixtures/tests, packaging validation and documented compatibility limits. See docs/validation-0.8.0.md.

## [Unreleased]

## [0.7.0]

These changes are prepared for 0.7.0; no publication date has been assigned.

### Added

- Explicit SELECT output limits: `max_rows_returned`, `max_response_bytes`
  for serialized column names and retained rows, and `max_cell_chars` per value.
  Truncated results retain `truncated`, `truncation_reason` and applicable
  `truncation_hint` fields; incomplete totals remain unknown.
- Structured denial diagnostics with `layer`, `rule`, `message` and `hint`.
  Keyword denials include bounded `matches` with line and column positions.
- Scalar `DECLARE` and `SET @variable` preambles before one SELECT statement.
  Object-reading initializers, table variables, dynamic SQL and multiple
  executable statements remain unsupported.

### Changed

- Non-fixed configurations default to reporting affected-row overshoots and
  committing. Fixed connection profiles always enforce strict rollback.
  `SEMANTIC_MCP_STRICT_ROWS_AFFECTED_CAP=true` or
  `affected_rows_policy="strict"` enables strict rollback for other profiles;
  a caller cannot lower a server-enforced strict cap.
- Confirmed write outcomes survive cleanup failures. Uncertain commit or
  rollback acknowledgements report `transaction_state="unknown"` and
  `automatic_retry_allowed=false`.
- Shorter tool and input-field descriptions. Tool names, remaining parameters,
  input constraints and full advertised `outputSchema` contracts are preserved.
- AST routing reuses its authorization analysis. Regex routing still requires
  independent strict TSQL AST authorization. Keyword validation shares one
  literal-masking pass and one positioned-keyword scan per batch.
- Schema filters and cache freshness calculations use shared helpers.
  JSON serialization still performs its normalization roundtrip.
- Split setup, snapshot persistence, query execution, FK graph loading and
  object-definition authorization into smaller functions.
- Adopt Ruff formatting and linting with Python 3.11 syntax, a 120-character
  line length, and `E`, `F`, `I`, `UP`, `B` and `BLE` checks. Broad exception
  boundaries retain individual explanations. Tracked cache code is checked
  even when a global Git ignore pattern matches its directory.
- Updated all README translations and environment examples for output limits,
  migration, denial diagnostics and strict rollback behavior.

### Removed

- `response_mode`, `return_mode`, `detail` and `token_budget_hint` parameters
  and their default configuration fields. SELECT execution returns bounded
  columns and rows without response modes or detail tiers.
- Active use of `budget`. Existing connection profiles still accept this
  retired field and ignore it; explicit output limits replace budget tiers.
- Unused `_write_target`, `_from_join_sources`, `_WRITE_OPS`,
  `mark_objects_dirty`, the cache store `connection` helper, the internal
  `domain/models` package, and `TableType`, `ObjectType`, `CacheStatus` enums.
- Seven tests specific to the removed domain models. Required Pydantic
  validators and MCP callbacks remain intact.
- Historical version-label comments that did not explain current behavior.
