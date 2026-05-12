# Mnemo MCP

Local-first project memory for MCP-capable coding agents.

Mnemo is a small stdio MCP server that gives coding agents a durable, project-scoped memory substrate. It stores decisions, interaction logs, context blocks, durable project knowledge, specialist feedback, useful commands, paths, failed approaches, and test results across sessions.

> Mnemo is a local project hippocampus: store broadly, index locally, recall narrowly, export readably, compact aggressively.

## Status

Current version: **0.10.0**

Runtime requirements:

- Python **3.10+**
- Standard library only
- Optional: [`agent-salience`](https://github.com/seslak/agent-salience) via `AGENT_SALIENCE_HOME` or normal Python import

Mnemo is local-first. It does not require a cloud service, external database, vector database, or package install.

## What Mnemo provides

- Local SQLite project memory by default
- JSON import/export compatibility for older `memory.json` files
- JSONL and Markdown exports for human inspection
- Bounded search and recall so memory growth does not automatically become token growth
- Structured memory layers for agentic systems
- Maintenance tools for compaction, consolidation, and import
- Copilot-safe MCP schemas and core/full tool profiles
- Optional deterministic salience diagnostics
- Lightweight symbol lookup under a configured workspace root

## Non-goals

Mnemo is not:

- an agent framework
- a hosted memory service
- a vector database
- a secrets manager
- a replacement for tests or source control

## Repository layout

```text
mnemo-mcp/
├── server.py
├── salience_loader.py
├── memory.example.json
├── smoke_test.py
├── test_server.py
├── examples/
├── docs/
├── CHANGELOG.md
└── README.md
```

## Quick start

Point your MCP client at `server.py`.

Example VS Code MCP config when Mnemo is checked out as `mnemo-mcp/` inside your workspace:

```json
{
  "servers": {
    "mnemo": {
      "type": "stdio",
      "command": "python",
      "args": ["${workspaceFolder}/mnemo-mcp/server.py"],
      "env": {
        "MNEMO_STORE": "sqlite",
        "MNEMO_MCP_PROFILE": "core",
        "MNEMO_WORKSPACE_ROOT": "${workspaceFolder}",
        "MNEMO_FILE": "${workspaceFolder}/state/mnemo/memory.json",
        "MNEMO_SQLITE_FILE": "${workspaceFolder}/state/mnemo/mnemo.sqlite"
      }
    }
  }
}
```

If you copy this repository into your project as a folder named `mnemo/`, use:

```json
"args": ["${workspaceFolder}/mnemo/server.py"]
```

More example configs are in [`examples/`](examples/).

## Local storage

Mnemo `0.10.0` uses SQLite by default.

```text
state/mnemo/
├── mnemo.sqlite              # primary store in SQLite mode
├── memory.json               # legacy/import/export compatibility path
└── exports/
    ├── memory.jsonl
    ├── hippocampus.md
    ├── agent_feedback.md
    └── startup_context_latest.md
```

SQLite remains copy/paste friendly because it is a single local file. You can copy `state/mnemo/` to move the memory with the project.

`memory.json` is no longer the primary store when `MNEMO_STORE=sqlite`; it is retained as an import/export compatibility format.

## Environment variables

| Variable | Description |
|---|---|
| `MNEMO_STORE` | Storage backend: `sqlite` or `json`. Default: `sqlite`. |
| `MNEMO_FILE` | Compatibility/import/export path for `memory.json`. Default: `<workspace>/state/mnemo/memory.json`. |
| `MNEMO_SQLITE_FILE` | SQLite path when `MNEMO_STORE=sqlite`. Default: `<workspace>/state/mnemo/mnemo.sqlite`. |
| `MNEMO_MCP_PROFILE` | Tool exposure profile: `core` or `full`. Default: `full`. Use `core` for Copilot-like clients with tool limits. |
| `MNEMO_WORKSPACE_ROOT` | Workspace root for `mnemo_lookup_symbol`. Default: current working directory. |
| `MNEMO_MAX_MEMORIES` | Total memory cap including retired entries. Default: `5000`. |
| `MNEMO_MAX_SEARCH_RESULTS` | Server-side cap for search results. Default: `20`. |
| `MNEMO_MAX_RECENT_RESULTS` | Server-side cap for recent results. Default: `50`. |
| `MNEMO_MAX_CHARS_PER_ITEM` | Per-item preview cap for search/recall/get preview mode. Default: `1200`. |
| `MNEMO_MAX_TOTAL_CHARS` | Total preview cap for bundled search/recall output. Default: `12000`. |
| `MNEMO_DECAY` | Set to `0` to disable time-decay scoring. Default: `1`. |
| `MNEMO_CONSOLIDATE_THRESHOLD` | Near-duplicate consolidation threshold. Default: `0.7`. |
| `MNEMO_MAX_FILES_SCANNED` | Max files scanned by `mnemo_lookup_symbol`. Default: `5000`. |
| `MNEMO_MAX_TOTAL_BYTES` | Max total bytes scanned by `mnemo_lookup_symbol`. Default: `52428800`. |
| `MNEMO_MAX_FILE_BYTES` | Max single file bytes read by `mnemo_lookup_symbol`. Default: `1048576`. |
| `AGENT_SALIENCE_HOME` | Optional path to local `agent-salience` checkout. |

In SQLite mode, lifecycle/query events are stored in the SQLite `events` table. Legacy JSONL event/query files are imported when present and left on disk as legacy artifacts.

## MCP profiles

`MNEMO_MCP_PROFILE=core` exposes a compact Copilot-friendly surface:

1. `mnemo_doctor`
2. `mnemo_search`
3. `mnemo_record`
4. `mnemo_link`
5. `mnemo_recall`
6. `mnemo_get`
7. `mnemo_export`
8. `mnemo_compact_context`
9. `mnemo_lookup_symbol`

`MNEMO_MCP_PROFILE=full` exposes the full maintenance surface:

- all core tools
- `mnemo_salience_check`
- `mnemo_update`
- `mnemo_delete`
- `mnemo_recent`
- `mnemo_inspect`
- `mnemo_maintenance`

## Tool overview

### `mnemo_doctor`

Returns storage, profile, schema, health, export, FTS, and salience diagnostics.

```json
{}
```

Use this to verify that the backend is SQLite, the SQLite file exists, and memory counts are visible.

### `mnemo_record`

Records a project memory of any supported kind.

```json
{"kind":"decision","text":"Run validation commands before handoff.","source":"team note","tags":["validation"],"pinned":true}
```

Structured memory aliases are accepted by the generic tool:

```json
{"kind":"interaction_log","summary":"Session handoff and active constraints.","role":"coordinator","agent_id":"coord_1"}
```

```json
{"kind":"context_block","body":"Expanded implementation context.","title":"Handoff block","linked_ids":["mem_log_id"]}
```

```json
{"kind":"hippocampus_entry","text":"Always run compile and unit tests before handoff.","evidence_ids":["mem_log_id"],"domain":"release"}
```

```json
{"kind":"agent_feedback","text":"Prefer middleware-first auth checks.","feedback_type":"good_pattern","agent_id":"spec_auth","domain":"auth"}
```

### `mnemo_search`

Searches project memory with bounded output.

```json
{"query":"validation commands before handoff","limit":5,"phase":"implementation","max_tokens":2000}
```

### `mnemo_recall`

Returns startup or specialist recall bundles.

```json
{"mode":"startup","role":"coordinator","agent_id":"coord_1","query":"release handoff","recent_logs":20}
```

```json
{"mode":"agent","agent_id":"spec_auth","role":"specialist","domain":"auth","task":"review auth middleware"}
```

### `mnemo_get`

Retrieves one memory by id. Use this when a search/recall preview is not enough.

```json
{"id":"mem_123","full":false}
```

```json
{"id":"mem_123","full":true}
```

### `mnemo_link`

Links two memory records.

```json
{"source_id":"mem_log","target_id":"mem_block","relation":"expands","bidirectional":true}
```

### `mnemo_export`

Writes readable exports.

```json
{"format":"jsonl"}
```

```json
{"format":"hippocampus_markdown"}
```

```json
{"format":"agent_feedback_markdown"}
```

### `mnemo_compact_context`

Builds a prompt-ready memory context block.

```json
{"query":"change the auth flow","limit":8,"phase":"implementation","max_tokens":2000}
```

### `mnemo_maintenance` full profile

Maintenance actions include `compact_logs`, `consolidate`, and `import_json`.

```json
{"action":"compact_logs","older_than_count":20,"max_logs":50,"dry_run":true}
```

```json
{"action":"consolidate","threshold":0.7,"dry_run":true}
```

```json
{"action":"import_json","path":"state/mnemo/memory.json","dry_run":true}
```

### `mnemo_inspect` full profile

Inspects history and related memories.

```json
{"id":"mem_123","mode":"both","limit":50,"depth":2,"include_archive":true}
```

### `mnemo_lookup_symbol`

Finds likely source definition locations under `MNEMO_WORKSPACE_ROOT`.

```json
{"name":"authenticate","limit":10}
```

### `mnemo_salience_check` full profile

Optional deterministic salience diagnostics when Agent Salience is available.

```json
{"text":"auth middleware decisions","limit":5,"threshold":0.7}
```

## Structured memory layers

Mnemo uses neutral, reusable memory kinds:

- `interaction_log`: short continuity notes from recent work
- `context_block`: larger linked memory artifacts
- `hippocampus_entry`: durable project/system knowledge
- `agent_feedback`: feedback scoped to an `agent_id`, `role`, or `domain`

Mnemo does not hardcode personal agent names. Use metadata fields such as:

- `agent_id`
- `role`
- `scope`
- `domain`
- `authority`
- `retention`
- `confidence`
- `linked_ids`
- `parent_id`
- `source_run_id`

## Memory growth and token cost

The store can grow locally without automatically increasing token usage. Mnemo controls token cost by returning bounded previews from search/recall and loading full memory bodies only by id through `mnemo_get`.

## Compaction

`mnemo_maintenance(action="compact_logs")` keeps recent interaction logs raw and can summarize older logs into a `context_block`. Source logs are retained.

## Hippocampus storage

Hippocampus is not a separate database. Durable project knowledge is stored in the same SQLite database with `kind="hippocampus_entry"`, plus fields such as `domain`, `scope`, `authority`, and `retention`.

## Copilot compatibility

Mnemo exports conservative MCP tool schemas for clients that reject full JSON Schema features. Constraints such as defaults, bounds, and enum handling are enforced in Python handlers and described in tool descriptions.

## Development

```bash
python -m compileall .
python smoke_test.py
python -m unittest discover -s . -p "test*.py"
```

## Privacy

Mnemo stores local project memory. Do not commit local state unless you intentionally want to share seed memory. See [`docs/privacy.md`](docs/privacy.md).

## License

MIT. See [`LICENSE`](LICENSE).
