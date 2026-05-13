# Mnemo Tool Reference

Mnemo exposes one public MCP tool:

```text
mnemo
```

All operations are selected with an `action` string and optional `params` object.

```json
{"action":"search","params":{"query":"release checklist"}}
```

The single gateway tool keeps the MCP surface small for Copilot-style clients while preserving the full Mnemo feature set.

## Gateway input

```json
{
  "action": "record",
  "params": {
    "kind": "decision",
    "text": "Run tests before publishing."
  }
}
```

- `action` is required.
- `params` is optional and defaults to an empty object.
- Unknown actions return a structured error with `available_actions`.

## Actions

### `doctor`

Returns server, storage, schema, and health diagnostics.

Useful for checking:

- Mnemo version
- SQLite/json backend
- memory count
- database/file paths
- available actions
- gateway status

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

Examples:

```json
{"action":"record","params":{"kind":"decision","text":"Use SQLite as the default Mnemo store."}}
```

```json
{"action":"record","params":{"kind":"hippocampus_entry","text":"Mnemo stores hippocampus entries in the same SQLite database as other memories.","domain":"mnemo/storage","authority":"high"}}
```

### `search`

Searches project memories relevant to a query.

Common params:

- `query`
- `kind`
- `limit`
- `role`
- `agent_id`
- `domain`
- `scope`
- `authority`
- `retention`
- `source_run_id`
- `include_deleted`
- `include_superseded`
- `max_tokens`

### `recall`

Returns bounded startup or agent-context bundles.

Common params:

- `mode`: `startup` or `agent`
- `query`
- `task`
- `agent_id`
- `role`
- `domain`
- `recent_logs`
- `max_blocks`
- `max_hippocampus`
- `max_feedback`
- `max_context_blocks`

### `get`

Retrieves one memory by id.

Common params:

- `id`
- `full`: true to return the complete memory text

### `link`

Links two memory records.

Common params:

- `source_id`
- `target_id`
- `relation`
- `bidirectional`

### `export`

Exports memories to local readable files.

Common params:

- `format`: `jsonl`, `json`, `markdown`, `hippocampus_markdown`, `agent_feedback_markdown`, or `startup_context_markdown`
- `path`
- `kind`
- `domain`
- `agent_id`
- `role`
- `include_deleted`
- `max_records`

Default exports go under `state/mnemo/exports/`.

### `compact_context`

Builds a prompt-ready compact context block for a query.

Common params:

- `query`
- `limit`
- `phase`
- `max_tokens`

### `lookup_symbol`

Finds likely definition locations for a symbol under `MNEMO_WORKSPACE_ROOT`.

Common params:

- `name`
- `limit`
- `case_sensitive`

### `salience_check`

Optional deterministic salience diagnostics when Agent Salience is available.

Common params:

- `text`
- `limit`
- `threshold`

### `update`

Updates an existing memory by id.

### `delete`

Soft-deletes an existing memory by id.

### `recent`

Returns recent project memories.

### `inspect`

Inspects memory history or related records.

Common params:

- `mode`: `history` or `related`
- `id`
- `depth`
- `limit`

### `maintenance`

Runs maintenance actions.

Common actions include:

- `compact_logs`
- `import_json`

Params are action-specific.

## Storage

SQLite is the default backend as of 0.10.0.

Default SQLite path:

```text
state/mnemo/mnemo.sqlite
```

`memory.json` remains a compatibility/import/export format.

## Schema compatibility

Mnemo exports a conservative MCP input schema for Copilot-style clients. Validation, defaults, and bounds are enforced inside Python handlers rather than relying on advanced JSON Schema keywords.
