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

Returns server, storage, schema, FTS, signature, export, and salience diagnostics.

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

### `search`

Searches project memories relevant to a query.

```json
{"action":"search","params":{"query":"SQLite signature backfill","limit":5}}
```

### `recall`

Returns bounded startup or agent-context bundles.

```json
{"action":"recall","params":{"mode":"startup","role":"coordinator","recent_logs":20}}
```

```json
{"action":"recall","params":{"mode":"agent","agent_id":"spec_auth","domain":"auth","task":"review middleware"}}
```

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
