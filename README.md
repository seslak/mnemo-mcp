# Mnemo MCP

Local-first project memory for coding agents.

Mnemo is a small stdio MCP server that gives an MCP-capable coding agent a durable, project-scoped memory file. It helps agents remember decisions, invariants, commands, failed approaches, important paths, test results, and useful notes across sessions.

> Mnemo remembers what matters about a project.

## Why

Coding agents often lose context between sessions. They reread the same files, repeat old decisions, forget project constraints, and ask the same questions again.

Mnemo gives them a simple local memory layer:

- record durable project memories
- search memories by task or query
- pin important invariants
- supersede outdated memories
- inspect recent memory and memory history
- build compact prompt context
- detect drift and near-duplicate memories
- look up source symbols in the workspace
- optionally run deterministic Agent Salience diagnostics

Mnemo is intentionally boring: one local Python server, JSON files, no database, no network dependency, and no required package install.

## Status

Current version: **0.7.0**

Runtime requirements:

- Python **3.10+**
- Standard library only
- Optional: `agent-salience` via `AGENT_SALIENCE_HOME` or normal Python import

## Repository layout

```text
mnemo/
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

Copy the `mnemo/` folder into a repository and point your MCP client at `mnemo/server.py`.

Example VS Code MCP config:

```json
{
  "servers": {
    "mnemo": {
      "type": "stdio",
      "command": "python",
      "args": ["${workspaceFolder}/mnemo/server.py"],
      "env": {
        "MNEMO_FILE": "${workspaceFolder}/mnemo/memory.json",
        "MNEMO_WORKSPACE_ROOT": "${workspaceFolder}"
      }
    }
  }
}
```

The same examples are available under [`examples/`](examples/).

## How it stores memory

Mnemo stores memory in a local JSON file.

By default:

```text
mnemo/memory.json
```

You can override this with:

```text
MNEMO_FILE=/path/to/memory.json
```

`memory.json` is gitignored by default. Team seed memories can be committed in:

```text
memory.example.json
```

On first start, if `memory.json` does not exist and `memory.example.json` does, Mnemo bootstraps the local memory file from the example.

## Tools

### `memory_record`

Record a durable project memory.

```json
{
  "kind": "decision",
  "text": "Run validation commands before handoff.",
  "source": "team note",
  "tags": ["validation"],
  "pinned": true
}
```

Supported kinds:

- `invariant`
- `decision`
- `failed_approach`
- `test_result`
- `command`
- `path`
- `note`

### `memory_search`

Search memories relevant to a query.

```json
{
  "query": "validation commands before handoff",
  "limit": 5,
  "phase": "implementation",
  "max_tokens": 2000
}
```

### `memory_recent`

Return recently recorded memories.

```json
{"limit": 10}
```

### `memory_update`

Patch an existing memory.

```json
{
  "id": "mem_123",
  "tags": ["validation", "handoff"],
  "pinned": true
}
```

### `memory_delete`

Soft-delete a memory.

```json
{
  "id": "mem_123",
  "reason": "outdated"
}
```

### `memory_compact_context`

Build a prompt-ready memory context block.

```json
{
  "query": "change the auth flow",
  "limit": 8,
  "phase": "implementation",
  "max_tokens": 2000
}
```

### `memory_history`

Inspect lifecycle events for one memory.

```json
{
  "id": "mem_123",
  "limit": 50,
  "include_archive": true
}
```

### `memory_related`

Walk references between memories.

```json
{
  "id": "mem_123",
  "depth": 2
}
```

### `memory_drift`

Compare recent memory vocabulary against older memory vocabulary.

```json
{
  "recent_count": 50,
  "older_count": 50
}
```

### `memory_consolidate`

Find or retire near-duplicate memories.

```json
{
  "threshold": 0.7,
  "dry_run": true
}
```

### `lookup_symbol`

Find likely source definition locations in the workspace.

```json
{
  "name": "authenticate",
  "limit": 10
}
```

### `memory_salience_check`

Optional deterministic diagnostics using Agent Salience.

```json
{
  "text": "auth middleware decisions",
  "limit": 5,
  "threshold": 0.7
}
```

Mnemo works without Agent Salience. If unavailable, this tool returns a clear `agent_salience_unavailable` response instead of breaking the server.

## Superseding memories

`memory_record` accepts `supersedes`. The old memory is linked to the new one and hidden from normal search results by default.

```json
{
  "kind": "decision",
  "text": "Use the new auth middleware.",
  "supersedes": "mem_old"
}
```

Deleted and superseded states are separate. Use `include_deleted` and `include_superseded` when you need historical results.

## Pinning and decay

Pinned memories are protected from size-cap cleanup and receive a scoring bonus.

```json
{
  "id": "mem_123",
  "pinned": true
}
```

Scores decay by memory kind unless `MNEMO_DECAY=0`. Pinned memories and invariants do not decay.

| Kind | Half-life |
| --- | --- |
| `invariant` | no decay |
| `decision` | 180 days |
| `command` | 90 days |
| `path` | 90 days |
| `failed_approach` | 60 days |
| `test_result` | 30 days |
| `note` | 30 days |

## Phase-aware retrieval

`memory_search` and `memory_compact_context` accept an optional `phase`:

- `exploration`
- `implementation`
- `debugging`
- `none`

If omitted, Mnemo tries to infer the phase from the query.

```json
{
  "query": "the test is failing with a traceback",
  "phase": "debugging"
}
```

## Optional Agent Salience diagnostics

Mnemo works without Agent Salience.

When `AGENT_SALIENCE_HOME` is configured, Mnemo can run optional salience diagnostics through `memory_salience_check`.

PowerShell:

```powershell
$env:AGENT_SALIENCE_HOME="C:\path\to\agent-salience"
```

bash/zsh:

```bash
export AGENT_SALIENCE_HOME="/path/to/agent-salience"
```

Diagnostics can help identify related memories, duplicate-like memories, and drift from pinned or invariant anchors. Scoring is local, lexical, deterministic, and explainable. It is not embeddings and not a vector database.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `MNEMO_FILE` | `memory.json` next to `server.py` | Local memory file |
| `MNEMO_MAX_MEMORIES` | `5000` | Total memory cap including retired entries |
| `MNEMO_LOG_QUERIES` | `1` | Enable `queries.jsonl` recall audit log |
| `MNEMO_WORKSPACE_ROOT` | parent of memory directory | Root for `lookup_symbol` |
| `MNEMO_SYMBOL_TTL_SECONDS` | `5` | Symbol-index walk cache TTL |
| `MNEMO_DECAY` | `1` | Enable time-decay scoring |
| `MNEMO_LOG_EVENTS` | `1` | Enable `events.jsonl` lifecycle log |
| `MNEMO_LOG_ARCHIVE` | `1` | Preserve rotated query/event logs in archive files |
| `MNEMO_CONSOLIDATE_THRESHOLD` | `0.7` | Default near-duplicate threshold |
| `MNEMO_MAX_SEARCH_RESULTS` | `20` | Server-side search result cap |
| `MNEMO_MAX_RECENT_RESULTS` | `50` | Server-side recent result cap |
| `MNEMO_MAX_FILES_SCANNED` | `5000` | Symbol lookup scan file cap |
| `MNEMO_MAX_TOTAL_BYTES` | `52428800` | Symbol lookup total byte cap |
| `MNEMO_MAX_FILE_BYTES` | `1048576` | Symbol lookup single-file byte cap |
| `AGENT_SALIENCE_HOME` | unset | Optional local Agent Salience checkout |

## Privacy and safety

Mnemo stores local project memory. Memory content may include project decisions, code facts, task notes, file paths, commands, and user or agent prompts.

Treat memory files as private project artifacts. Review local memory before sharing a repo or copying a Mnemo folder elsewhere.

The provided `.gitignore` excludes local memory and log artifacts by default.

`lookup_symbol` stays under `MNEMO_WORKSPACE_ROOT`, skips common generated folders, and applies file-count and byte-count scan limits.

More details: [`docs/privacy.md`](docs/privacy.md)

## Validate

From inside `mnemo/`:

```bash
python -m compileall .
python smoke_test.py
python -m unittest discover -s . -p "test*.py"
```

## Design principles

Mnemo should stay:

- local-first
- transparent
- dependency-light
- easy to inspect
- safe by default
- useful for real coding agents

It is not intended to be a hosted memory platform, vector database, or full agent framework.

## When to outgrow this

Mnemo search is lexical by design. It splits identifiers, applies simple suffix stripping, weights fields, and favors short on-target memories.

When lexical recall is no longer enough, replace the scoring internals with embeddings or a vector index while keeping the same MCP tool names and response shapes.

## License

MIT
