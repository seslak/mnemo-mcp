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

Example:

```json
{"action":"record","params":{"kind":"decision","text":"Use SQLite as the default Mnemo store."}}
```

Record-time duplicate behavior:

1. exact `content_hash` duplicate short-circuits
2. exact `normalized_hash` duplicate short-circuits
3. shingle-overlap survivors get full salience/fallback scoring
4. no automatic delete/merge occurs

### `alias_hint`

Records explicit alias evidence from failed wording to successful canonical wording.

```json
{"action":"alias_hint","params":{"domain":"agentic","canonical":"memory recall pipeline","candidate_alias":"hippocampus bridge","original_query":"hippocampus bridge","successful_query":"memory recall pipeline","confidence":"high","include_in_salience":true}}
```

Notes:

- `confidence` must be `low`, `medium`, or `high`
- this records evidence only; no alias activation occurs

### `search`

Searches project memories relevant to a query.

```json
{"action":"search","params":{"query":"SQLite signature backfill","limit":5}}
```

Miss tagging:

- search-like events include typed `result_count`, `success`, and `top_score`
- miss is recorded when `result_count == 0` or `top_score < MNEMO_MISS_TOP_SCORE_THRESHOLD` (default `0.15`)

### `recall`

Returns bounded startup or agent-context bundles.

```json
{"action":"recall","params":{"mode":"startup","role":"coordinator","recent_logs":20}}
```

```json
{"action":"recall","params":{"mode":"agent","agent_id":"spec_auth","domain":"auth","task":"review middleware"}}
```

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

### `salience_check`

Runs optional Agent Salience diagnostics when `agent-salience` is importable.

```json
{"action":"salience_check","params":{"text":"auth middleware decisions","candidate_limit":500,"max_scored":100}}
```

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

## Git-aware write metadata (0.13.5)

`record` accepts an optional `touched_files` parameter when the caller knows which files the interaction or memory refers to:

```json
{
  "action": "record",
  "params": {
    "kind": "context_block",
    "text": "IBAN validation lives in payments/iban.py.",
    "touched_files": ["payments/iban.py"]
  }
}
```

When git context is available, Mnemo stamps the new memory with `git_sha`, `git_branch`, and `git_dirty`, and stores file fingerprints in `memory_files`. The write still succeeds if git is unavailable or a file fingerprint cannot be resolved.

Search and recall apply a freshness multiplier to already-scored candidates with file fingerprints:

- legacy row / no git metadata: `1.0`
- unchanged touched files: `1.0`
- changed touched file: `0.7`
- missing/deleted touched file: `0.3`

This is a post-score retrieval adjustment only. It does not change IDF scoring, alias scoring, or candidate generation.

