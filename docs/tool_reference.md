# Mnemo Tool Reference

Mnemo exposes a local stdio MCP server with project-memory tools. Tool availability depends on `MNEMO_MCP_PROFILE`.

## Core profile tools

### `mnemo_doctor`

Returns diagnostics for the active store, profile, tool count, SQLite file, memory counts, export files, search backend, warnings, and recommendations.

Input:

```json
{}
```

### `mnemo_search`

Search project memories relevant to a task, query, file, command, or decision.

Common inputs:

- `query` required
- `kind`
- `limit`
- `role`
- `agent_id`
- `domain`
- `scope`
- `authority`
- `retention`
- `source_run_id`
- `phase`
- `max_tokens`

Example:

```json
{"query":"validation commands before handoff","limit":5,"domain":"release"}
```

### `mnemo_record`

Record a project memory. Use `kind` to select the memory layer.

Supported kinds include:

- `decision`
- `invariant`
- `failed_approach`
- `test_result`
- `command`
- `path`
- `note`
- `interaction_log`
- `context_block`
- `hippocampus_entry`
- `agent_feedback`

Examples:

```json
{"kind":"decision","text":"Use SQLite as Mnemo's default store.","tags":["storage"]}
```

```json
{"kind":"interaction_log","summary":"Discussed Mnemo storage migration and SQLite exports.","role":"coordinator"}
```

```json
{"kind":"context_block","title":"Storage migration notes","body":"SQLite is the primary local store; JSONL/Markdown are exports."}
```

```json
{"kind":"hippocampus_entry","text":"Hippocampus entries are not stored in a separate database; they use kind=hippocampus_entry.","domain":"mnemo/storage","authority":"high"}
```

```json
{"kind":"agent_feedback","text":"Use mnemo_get for full bodies after bounded recall.","role":"coordinator","domain":"memory"}
```

### `mnemo_link`

Link two memories.

```json
{"source_id":"mem_a","target_id":"mem_b","relation":"evidence_for","bidirectional":false}
```

### `mnemo_recall`

Return a bounded recall bundle.

Startup mode:

```json
{"mode":"startup","role":"coordinator","query":"current Mnemo storage work"}
```

Agent mode:

```json
{"mode":"agent","agent_id":"storage-specialist","domain":"mnemo/storage","task":"review export behavior"}
```

### `mnemo_get`

Retrieve a single memory by id.

```json
{"id":"mem_123","full":false}
```

Use `full=true` only when the complete text is needed.

### `mnemo_export`

Export memories to local readable files.

Common formats:

- `jsonl`
- `json`
- `markdown`
- `hippocampus_markdown`
- `agent_feedback_markdown`
- `startup_context_markdown`

Example:

```json
{"format":"hippocampus_markdown"}
```

### `mnemo_compact_context`

Build a prompt-ready context brief from memory.

```json
{"query":"release handoff","limit":8,"max_tokens":2000}
```

### `mnemo_lookup_symbol`

Find likely source definition locations under `MNEMO_WORKSPACE_ROOT`.

```json
{"name":"authenticate","limit":10}
```

## Full profile tools

The full profile adds maintenance and inspection tools.

### `mnemo_salience_check`

Optional salience diagnostics when Agent Salience is available.

### `mnemo_update`

Patch an existing memory by id.

### `mnemo_delete`

Soft-delete an existing memory.

### `mnemo_recent`

Return recent memories.

### `mnemo_inspect`

Inspect lifecycle history and related-memory graph.

```json
{"id":"mem_123","mode":"both","limit":50,"depth":2}
```

### `mnemo_maintenance`

Run maintenance actions.

Actions include:

- `compact_logs`
- `consolidate`
- `import_json`

Examples:

```json
{"action":"compact_logs","dry_run":true,"older_than_count":20}
```

```json
{"action":"import_json","path":"state/mnemo/memory.json","dry_run":true}
```

## Copilot-safe schemas

Mnemo intentionally exports conservative input schemas. Defaults, bounds, optional-null handling, and validation are implemented in Python handlers rather than relying on JSON Schema keywords that some MCP clients reject.
