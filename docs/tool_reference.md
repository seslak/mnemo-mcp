# Mnemo Tool Reference

Mnemo exposes one public MCP gateway tool:

```text
mnemo
```

Every call uses this shape:

```json
{"action":"search","params":{"query":"release checklist"}}
```

`action` is required. `params` is optional and defaults to an empty object.

## Actions

### `doctor`

Returns server, storage, schema, FTS, signature, export, salience, event-history, and IDF diagnostics.

```json
{"action":"doctor"}
```

### `record`

Records a project memory.

Common params:

- `kind`: `decision`, `invariant`, `failed_approach`, `test_result`, `command`, `path`, `note`, `interaction_log`, `context_block`, `hippocampus_entry`, or `agent_feedback`
- `text`
- `summary` for `interaction_log`
- `body` for `context_block`
- `title`
- `source`
- `tags`
- `linked_ids`
- `agent_id`
- `role`
- `domain`
- `scope`
- `authority`
- `retention`
- `confidence`
- `source_run_id`
- `metadata`
- `touched_files`: optional array of workspace paths touched during the turn (used for git-aware freshness tracking)
- `namespace`: optional, defaults to `local`
- `origin`: optional, defaults to `local`

Example:

```json
{"action":"record","params":{"kind":"decision","text":"Use SQLite as the default Mnemo store."}}
```

Record-time duplicate behavior:

1. exact `content_hash` duplicate short-circuits
2. exact `normalized_hash` duplicate short-circuits
3. shingle-overlap survivors get full salience/fallback scoring
4. no automatic delete/merge occurs

Git-aware write metadata:

- each new row attempts to stamp `git_sha`, `git_branch`, and `git_dirty`
- when `touched_files` is present, Mnemo stores per-path digests in `memory_files`
- non-git roots and git failures are non-fatal; writes still succeed

### `alias_hint`

Records explicit alias evidence from failed wording to successful canonical wording.

```json
{"action":"alias_hint","params":{"domain":"agentic","canonical":"memory recall pipeline","candidate_alias":"hippocampus bridge","original_query":"hippocampus bridge","successful_query":"memory recall pipeline","confidence":"high","include_in_salience":true}}
```

Notes:

- `confidence` must be `low`, `medium`, or `high`
- this records evidence only; no alias activation occurs

### `topic_add`

Adds one topic row to one memory.

```json
{"action":"topic_add","params":{"memory_id":"mem_123","topic":"auth","source":"operator"}}
```

Notes:

- `topic` must be non-empty
- duplicate `(memory_id, topic)` rows are ignored
- `source` defaults to `agent`

### `topic_remove`

Removes one topic row from one memory.

```json
{"action":"topic_remove","params":{"memory_id":"mem_123","topic":"auth"}}
```

### `topic_list`

Lists topic metadata.

All topics with counts:

```json
{"action":"topic_list","params":{"scope":"all"}}
```

Topics for one memory:

```json
{"action":"topic_list","params":{"scope":"memory","memory_id":"mem_123"}}
```

### `pack_preview`

Read-only preview for future memory-pack selection.

```json
{"action":"pack_preview","params":{"topics":["auth"],"include_imported":true,"limit":100}}
```

Key params:

- `topics`: topic filter via `memory_topics` joins
- `kinds`: defaults to `["context_block","hippocampus_entry"]`
- `namespace` or `namespaces` (mutually exclusive)
- `include_imported`: adds trusted imported namespaces
- `include_quarantine`: adds quarantine namespaces
- `origin` or `origins` (mutually exclusive; applied only when explicit)
- `created_after` / `created_before`: ISO-8601 UTC
- `touched_paths`: exact/normalized repo-relative path filter via `memory_files`
- `limit`: defaults to `100`
- `sample_per_kind`: defaults to `3`
- `include_samples`: defaults to `true`

Read-only behavior:

- does not create `exported_packs` rows
- does not write pack files
- does not run export/import/redaction/signing/trust/promotion flows

### `pack_redaction_preview`

Read-only baseline redaction dry-run over the same selection engine used by `pack_preview`.

```json
{"action":"pack_redaction_preview","params":{"topics":["auth"],"include_imported":true,"limit":100}}
```

Selection params match `pack_preview`, including:

- `topics`
- `kinds`
- `namespace` / `namespaces`
- `include_imported` / `include_quarantine`
- `origin` / `origins`
- `created_after` / `created_before`
- `touched_paths`
- `limit`

Redaction-specific params:

- `include_redacted_samples` (default `true`)
- `max_redacted_samples` (default `10`, capped to `50`)

Baseline redaction categories:

- `private_key_header`
- `jwt`
- `aws_access_key`
- `email`
- `user_path`
- `ipv4`

Read-only behavior:

- no `exported_packs` mutations
- no pack file writes
- no ZIP export/import, signing, trust, or promotion flows
- dry-run only; output is diagnostic counts plus bounded redacted previews

### `pack_export`

Writes a local unsigned development pack ZIP using the same selection semantics as `pack_preview`.

```json
{"action":"pack_export","params":{"pack_name":"auth_memory_pack","allow_unsigned":true}}
```

Key params:

- `pack_name` (required, sanitized for filesystem safety)
- `allow_unsigned` (required in Phase 2c; must be `true`)
- `output_dir` (optional; defaults to `state/mnemo/packs/exports/`)
- selection filters shared with `pack_preview`:
  - `topics`
  - `kinds`
  - `namespace` / `namespaces`
  - `include_imported` / `include_quarantine`
  - `origin` / `origins`
  - `created_after` / `created_before`
  - `touched_paths`
  - `limit`
- `allow_limited_export` (default `false`)

Policy constraints in Phase 2c:

- signing is not implemented yet; export fails unless `allow_unsigned=true`
- exportable kinds are strict:
  - `context_block`
  - `hippocampus_entry`
- preview-only kinds (`interaction_log`, `agent_feedback`) are rejected for export

Redaction behavior:

- redaction is mandatory during export
- baseline-v1 categories:
  - `private_key_header`
  - `jwt`
  - `aws_access_key`
  - `email`
  - `user_path`
  - `ipv4`
- baseline-v1 is intentionally incomplete and not a full DLP system

Pack format notes:

- required ZIP members:
  - `manifest.json`
  - `content/memories.jsonl`
  - `content/topics.json`
  - `content/file_fingerprints.json`
  - `provenance/origin.json`
  - `provenance/redactions.json`
- rows in `content/memories.jsonl` use pack-local row IDs (`ctx_###`, `hip_###`), not source DB `mem_*` IDs
- `manifest.json` includes SHA-256 `content_hash` over covered content/provenance members
- successful export writes one `exported_packs` audit row

### `search`

Searches project memories relevant to a query.

```json
{"action":"search","params":{"query":"SQLite signature backfill","limit":5}}
```

Namespace/origin scope params:

- `namespace` or `namespaces` (mutually exclusive)
- `include_imported` (adds trusted imported namespaces)
- `include_quarantine` (adds quarantine namespaces)
- `origin` or `origins` (origin filters applied only when explicitly supplied)

Default namespace scope when omitted:

- `['local']`

Miss tagging:

- search-like events include typed `result_count`, `success`, and `top_score`
- miss is recorded when `result_count == 0` or `top_score < MNEMO_MISS_TOP_SCORE_THRESHOLD` (default `0.15`)

Git-aware freshness weighting (SQLite mode):

- base ranking math is unchanged, then multiplied by freshness
- legacy rows / `git_sha=NULL`: `1.0`
- touched files unchanged: `1.0`
- any touched file changed: `0.7`
- any touched file missing/deleted: `0.3`
- mixed states use the minimum multiplier

No backfill is performed for legacy rows.

### `recall`

Returns bounded startup or agent-context bundles.

```json
{"action":"recall","params":{"mode":"startup","role":"coordinator","recent_logs":20}}
```

```json
{"action":"recall","params":{"mode":"agent","agent_id":"spec_auth","domain":"auth","task":"review middleware"}}
```

Recall supports the same namespace/origin scope params as `search` and defaults to `namespace='local'`.

When `query`/`task` is present, recall writes miss-aware query events using returned score evidence where available.

### `get`

Retrieves one memory by id.

```json
{"action":"get","params":{"id":"mem_123","full":true}}
```

### `link`

Links one memory to another.

```json
{"action":"link","params":{"source_id":"mem_a","target_id":"mem_b","relation":"expands","bidirectional":true}}
```

### `export`

Exports memories to local readable files.

Formats include:

- `jsonl`
- `json`
- `markdown`
- `hippocampus_markdown`
- `agent_feedback_markdown`
- `startup_context_markdown`

```json
{"action":"export","params":{"format":"jsonl"}}
```

Default exports go under `state/mnemo/exports/`.

### `compact_context`

Builds a prompt-ready context block grouped by memory kind.

```json
{"action":"compact_context","params":{"query":"auth middleware changes","limit":8,"max_tokens":2000}}
```

`compact_context` supports the same namespace/origin scope params as `search`.

### `salience_check`

Runs optional Agent Salience diagnostics when `agent-salience` is importable.

```json
{"action":"salience_check","params":{"text":"auth middleware decisions","candidate_limit":500,"max_scored":100}}
```

`salience_check` supports the same namespace/origin scope params as `search`.

The action is candidate-limited. In SQLite mode it uses FTS when available, then signature overlap, then scores bounded survivors.

Event tagging:

- when no match triggers the supplied threshold, `success=0` is recorded
- `top_score` stores the best scored candidate (or `0.0`)

When local IDF profiles are active, `salience_check` passes `mode="auto"` and the active project/domain `idf_profile` into Agent Salience scoring.

Active-IDF scoring uses an IDF-dominant mix:

- `idf_cosine: 0.55`
- `idf_jaccard: 0.35`
- `cosine: 0.05`
- `jaccard: 0.05`

This adds weighted Jaccard/Tanimoto (`idf_jaccard`) to reduce common-word overlap false positives.

Diagnostics include:

- `idf_used`
- `idf_scope_used`
- `idf_profile_status`
- `score_breakdown.cosine`
- `score_breakdown.jaccard`
- `score_breakdown.idf_cosine`
- `score_breakdown.idf_jaccard`
- `score_weights`

When IDF is cold, disabled, or unavailable, lexical scoring remains active and `idf_used` stays false.

### `maintenance`

Runs maintenance sub-actions.

#### `compact_logs`

```json
{"action":"maintenance","params":{"action":"compact_logs","older_than_count":20,"max_logs":50,"dry_run":true}}
```

#### `consolidate`

Candidate-based consolidation. This is the default safe path.

```json
{"action":"maintenance","params":{"action":"consolidate","dry_run":true,"max_candidates_per_memory":100}}
```

Default consolidation checks exact hashes globally, then uses bounded candidate retrieval and shingle overlap before full similarity.

#### `consolidate_full`

Explicit O(n²) full scan. Requires confirmation.

```json
{"action":"maintenance","params":{"action":"consolidate_full","confirm_full_scan":true,"dry_run":true}}
```

Without `confirm_full_scan:true`, this returns `full_scan_confirmation_required` with an estimated pair count.

#### `backfill_signatures`

Backfills missing or outdated v0.12.0 signatures.

```json
{"action":"maintenance","params":{"action":"backfill_signatures","dry_run":true}}
```

```json
{"action":"maintenance","params":{"action":"backfill_signatures","dry_run":false}}
```

`doctor` warns when more than 10% of active records are unsigned/outdated.

#### `propose_aliases`

Generates structured alias proposals from recent miss evidence and optional `alias_hint` events.

```json
{"action":"maintenance","params":{"action":"propose_aliases","window_days":30,"domain":"agentic","min_recurrence":3,"limit":20,"dry_run":true,"include_hints":true,"min_loose_score":0.20,"max_candidates_per_cluster":5}}
```

Output status values:

- `ok`
- `idf_cold`
- `no_misses`
- `no_proposals`

Persistence behavior:

- `dry_run=true`: returns proposals without writing
- `dry_run=false`: persists pending proposals in SQLite

#### `list_alias_proposals`

Lists proposal rows from `alias_proposals`.

```json
{"action":"maintenance","params":{"action":"list_alias_proposals","status":"pending","domain":"agentic","limit":50}}
```

#### `approve_alias`

Creates/updates `alias_concepts` + `alias_terms` and marks proposal approved when `proposal_id` is supplied.

```json
{"action":"maintenance","params":{"action":"approve_alias","proposal_id":"alias-prop-...","approved_by":"coordinator"}}
```

#### `reject_alias_proposal`

Marks a proposal rejected without deleting evidence.

```json
{"action":"maintenance","params":{"action":"reject_alias_proposal","proposal_id":"alias-prop-...","reason":"generic wording"}}
```

#### `list_aliases`

Lists active vocabulary rows.

```json
{"action":"maintenance","params":{"action":"list_aliases","domain":"agentic","language":"en","status":"active","limit":200}}
```

#### `disable_alias`

Disables one alias term without deleting it.

```json
{"action":"maintenance","params":{"action":"disable_alias","alias_id":"alias-term-...","reason":"deprecated"}}
```

#### `disable_alias_concept`

Disables one alias concept; runtime ignores disabled concepts.

```json
{"action":"maintenance","params":{"action":"disable_alias_concept","concept_id":"alias-concept-...","reason":"deprecated"}}
```

#### `import_json`

Imports JSON memories and computes signatures during import.

```json
{"action":"maintenance","params":{"action":"import_json","path":"state/mnemo/memory.json","dry_run":true}}
```

### Top-level maintenance aliases

For schema discoverability, these are also accepted as top-level gateway actions:

```json
{"action":"backfill_signatures","params":{"dry_run":false}}
```

```json
{"action":"consolidate_full","params":{"confirm_full_scan":true,"dry_run":true}}
```

### `inspect`

Reads lifecycle history and/or related-memory graph.

```json
{"action":"inspect","params":{"id":"mem_123","mode":"both","include_archive":true}}
```

### `recent`

Returns the most recently recorded memories.

```json
{"action":"recent","params":{"limit":10}}
```

### `recent_events`

Returns newest event rows first.

Typed query metadata includes `result_count`, `success`, and `top_score` when available.

```json
{"action":"recent_events","params":{"limit":20,"action":"optional","kind":"optional","domain":"optional"}}
```

### `search_events`

Searches event history across action, memory id, query text, summaries, salience text, and identity fields.

```json
{"action":"search_events","params":{"query":"IBAN validation","limit":20,"action":"optional","domain":"optional"}}
```

### `get_event`

Returns one event by id.

```json
{"action":"get_event","params":{"event_id":"evt_..."}}
```

### `memory_events`

Returns events related to one memory id.

```json
{"action":"memory_events","params":{"memory_id":"mem_...","limit":50}}
```

### `update`

Updates fields on an existing memory.

```json
{"action":"update","params":{"id":"mem_123","tags":["release"]}}
```

### `delete`

Soft-deletes an existing memory.

```json
{"action":"delete","params":{"id":"mem_123","reason":"obsolete"}}
```

### `lookup_symbol`

Finds likely source definition locations under `MNEMO_WORKSPACE_ROOT`.

```json
{"action":"lookup_symbol","params":{"name":"authenticate","limit":10}}
```

## Signature fields

SQLite rows include deterministic signatures:

- `content_hash`
- `normalized_hash`
- `token_count`
- `unique_token_count`
- `top_terms_json`
- `shingle_hashes_json`
- `signature_version`
- `normalizer_version`
- `signature_updated_at`

`content_hash` uses full raw text after stable line-ending normalization. Token-based signatures are capped at 50,000 characters.

## Schema compatibility

Mnemo exports a conservative MCP schema for Copilot-style clients. Advanced JSON Schema features are intentionally avoided; handlers enforce ranges, defaults, and validation.
