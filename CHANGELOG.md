# Changelog

## 0.7.0

- Added optional local Agent Salience integration via `AGENT_SALIENCE_HOME`.
- Added read-only `memory_salience_check` MCP tool for deterministic salience diagnostics across stored memories.
- Added optional pinned/invariant anchor drift warnings in salience checks (diagnostics only, no blocking).
- Added optional salience loader helpers and tests for unavailable and environment-based loading paths.
- Mnemo remains fully functional when Agent Salience is absent.

## 0.6.0

- Deterministic time-decay scoring support for tests; `score_memory` and
  decay handling can evaluate against a supplied `now` value.
- Superseded memories are no longer automatically marked deleted. Search
  flags now distinguish `include_superseded` from `include_deleted`, with
  compatibility handling for older superseded records that also carried
  `deleted_at`.
- `memory_record` accepts `pinned: true` at creation time and validates it
  as a strict boolean.
- Added conservative result and lookup scan limits:
  `MNEMO_MAX_SEARCH_RESULTS`, `MNEMO_MAX_RECENT_RESULTS`,
  `MNEMO_MAX_FILES_SCANNED`, `MNEMO_MAX_TOTAL_BYTES`, and
  `MNEMO_MAX_FILE_BYTES`.
- README: added local privacy and safety notes and documented the new
  safety environment variables.

## 0.5.1

- Permanent archive for rotated `queries.jsonl` and `events.jsonl` contents (`queries.archive.jsonl`, `events.archive.jsonl`). Disable with `MNEMO_LOG_ARCHIVE=0`.
- `memory_history` accepts `include_archive: true` to scan the events archive for deep historical lookups.

## 0.5.0

- `memory_drift` returns a vocabulary-drift scalar between recent and older memories.
- `memory_consolidate` surfaces near-duplicate clusters per kind and, with `dry_run=false`, retires duplicates via a supersede chain to the newest survivor.
- `memory_compact_context` and `memory_search` accept an optional `max_tokens` argument that caps the rendered text block at the estimated token budget. The full result set still appears in `structuredContent`.
- Private token estimator (`estimate_tokens`, ≈ chars / 3.7) added for budget enforcement; will be superseded by thrift later.

## 0.4.1

- `memory_compact_context` now marks pinned memories with `★` in the rendered prompt block.

## 0.4.0

- Pinning (`pinned: true` field; protected from archive; +0.3 score bonus).
- Time-decay scoring with kind-aware half-lives; disable with `MNEMO_DECAY=0`.
- References between memories (`references: [mem_id, ...]`).
- `memory_history` tool reads from a new `events.jsonl` log.
- `memory_related` tool walks the reference graph.
- Phase-aware retrieval on `memory_search` and `memory_compact_context` (auto-inferred or explicit).

## 0.3.0

- `MNEMO_MAX_MEMORIES` cap now applies to total memories; archiving retired entries restores headroom for new writes.
- `lookup_symbol` adds a TTL walk-cache (`MNEMO_SYMBOL_TTL_SECONDS`, default 5) so repeat calls within the TTL skip the workspace walk entirely.
- `lookup_symbol` indexing is now restricted to a code-extension allowlist; non-code files such as Markdown, JSON, and YAML are no longer scanned. Fallback indexing is capped at 256 KB per file.

## 0.2.1

- Suffix stripper now bridges common verb pairs such as validate, validating, and validates.
- Seed memory ships as `memory.example.json`; `memory.json` is bootstrapped locally on first server start and remains gitignored.
- Tightened `memory_update` JSON Schema.
- Declared `tools.listChanged` in initialize capabilities.
- README: added `memory_recent` example and rewrote seed-memory guidance.

## 0.2.0

- Folder rename to `mnemo`.
- Deduplication on `memory_record`.
- `memory_update`, `memory_delete`, and `supersedes` support.
- Cross-platform write lock for concurrent server processes.
- Size cap with retired-memory archive rotation.
- Query audit log in `queries.jsonl`.
- `lookup_symbol` source lookup tool.
- Expanded smoke and unit tests.
