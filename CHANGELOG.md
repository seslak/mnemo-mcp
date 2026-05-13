# Changelog

## 0.11.0

### Changed

- Consolidated the public MCP surface to one gateway tool: `mnemo`.
- All previous Mnemo operations are now available as gateway actions such as `record`, `search`, `recall`, `get`, `export`, `maintenance`, and `lookup_symbol`.
- Removed profile-based public tool exposure; `MNEMO_MCP_PROFILE` is now ignored for tools/list output.
- Kept SQLite storage, readable exports, bounded recall, and Copilot-safe schemas.

### Compatibility

- Storage format is unchanged from 0.10.0.
- Existing memory data remains usable.
- Public MCP tool names changed intentionally before 1.0.0 to reduce Copilot tool-inventory pressure.

## 0.10.0

### Added

- SQLite primary local store using stdlib `sqlite3`.
- Automatic import from legacy `memory.json`.
- Bootstrap from `memory.example.json` in SQLite mode.
- Legacy `events.jsonl` / `queries.jsonl` ingestion into SQLite `events` table.
- JSONL and Markdown exports via `mnemo_export`.
- `mnemo_get` for full single-memory retrieval by id.
- Extended `mnemo_doctor` health diagnostics.
- Output caps for search/recall.
- SQLite-backed `compact_logs` maintenance.
- Optional FTS5 detection with lexical fallback search.

### Changed

- SQLite is the default backend (`MNEMO_STORE=sqlite`).
- `memory.json` is a compatibility/import/export format in SQLite mode.
- SQLite `events` table is authoritative for new lifecycle/query events in SQLite mode.
- Recall/search return bounded previews by default; full bodies are loaded with `mnemo_get`.
- `mnemo_maintenance` remains the maintenance surface for compaction/import.

### Compatibility

- Existing `memory.json` files remain importable.
- Existing `memory.example.json` first-run bootstrap is preserved.
- Existing JSONL logs are imported and left on disk as legacy artifacts.
- No external dependencies added.
- Hippocampus entries remain in the same store as other memories.

## 0.9.4

- Extended `mnemo_doctor` diagnostic payload: returns memory file path, size, memory count, per-kind breakdown, last write timestamp and id, events log status, archive status, drift scalar with interpretation, salience loader status, and active warnings. Agents no longer need shell checks to verify Mnemo state.
- Further tool surface consolidation from 16 to 13 tools.
  - Removed `mnemo_drift`; drift is reported by `mnemo_doctor`.
  - Removed `mnemo_history` and `mnemo_related`; consolidated into `mnemo_inspect(id, mode='history'|'related'|'both')`.
  - Removed `mnemo_compact_interaction_logs` and `mnemo_consolidate`; consolidated into `mnemo_maintenance(action='compact_logs'|'consolidate', dry_run=...)`.
- All v0.7.0/v0.8.0/v0.9.x features remain intact.
- Memory schema and storage formats unchanged.

## 0.9.3

- Added stricter Copilot-safe schema compatibility checks for `tools/list` export (forbidden keyword removal, supported-key-only subset, and no nullable type arrays in exported schemas).
- Added `MNEMO_MCP_PROFILE=core|full` tool exposure profiles (`full` default) to reduce Copilot inventory pressure in constrained environments.
- Kept runtime validation/default/clamping behavior in Python handlers; storage format and memory schema remain unchanged.

## 0.9.2

- Consolidated tool surface from 21 to 16 tools for GitHub Copilot MCP client inventory cap compatibility.
  - Removed `mnemo_record_interaction_log`, `mnemo_record_context_block`, `mnemo_record_hippocampus_entry`, and `mnemo_record_agent_feedback`.
    Use `mnemo_record(kind=...)` instead. Aliases `summary`/`body`/`title`/`evidence_ids`/`feedback_type` are accepted by
    the generic tool and routed to the appropriate underlying fields per kind.
  - Removed `mnemo_recall_startup_context` and `mnemo_recall_agent_context`.
    Use `mnemo_recall(mode='startup' or 'agent', ...)` instead.
- All memory data formats unchanged; existing `memory.json` files remain readable.
- All v0.7.0/v0.8.0 features (resonance gating, decay, references, events log, archive, salience integration, structured memory layers) remain intact.

## 0.9.1

- Stripped JSON Schema keywords (`minimum`, `maximum`, `default`, `minItems`, `maxItems`, `minLength`, `maxLength`, `pattern`) from tool `inputSchema` responses for GitHub Copilot MCP client compatibility.
  Constraints are documented in description fields and enforced at runtime in handlers. Other MCP clients are unaffected.

## 0.9.0

### Changed

- Renamed public MCP tool surface from `memory_*` to `mnemo_*` to avoid confusion with native editor/assistant memory tools.
- Mnemo project memory is now clearly separated from Copilot native memory.
- Updated prompts/docs to use Mnemo-branded tool names.

### Added

- `mnemo_doctor` diagnostic tool.

### Compatibility

- Existing memory files remain readable.
- Storage schema is unchanged.
- Public MCP tool names changed intentionally before external release.

## 0.8.0

### Added

- Structured memory layers:
  - interaction logs
  - context blocks
  - hippocampus entries
  - agent feedback
- Startup context recall for coordinator/front-facing agents.
- Agent/specialist context recall.
- Memory linking support.
- Interaction-log compaction support.
- New filters for structured memory recall.

### Changed

- Extended memory schema with optional metadata fields such as role, agent_id, domain, scope, authority, retention, confidence, linked_ids, parent_id, and source_run_id.
- Improved Mnemo documentation around reusable agent roles and memory layers.

### Compatibility

- Existing memory records remain readable.
- Existing MCP tools remain backward-compatible.
- No hardcoded personal agent names are introduced.

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
