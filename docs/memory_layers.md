# Mnemo Memory Layers

Mnemo uses one public MCP gateway tool:

```text
mnemo
```

All operations use `action` plus optional `params`.

## Core memory kinds

- `interaction_log`: short continuity notes from recent work
- `context_block`: larger linked memory artifacts
- `hippocampus_entry`: durable project/system knowledge
- `agent_feedback`: feedback scoped to an `agent_id`, `role`, or `domain`

These are stored in the same SQLite database as all other memories.

## Recall

Startup bundle:

```json
{"action":"recall","params":{"mode":"startup","role":"coordinator","recent_logs":20}}
```

Specialist bundle:

```json
{"action":"recall","params":{"mode":"agent","agent_id":"spec_auth","role":"specialist","domain":"auth","task":"review middleware"}}
```

Use `action="get"` to retrieve full memory bodies by id.

## Recording layer examples

Interaction log:

```json
{"action":"record","params":{"kind":"interaction_log","summary":"Short continuity note.","role":"coordinator"}}
```

Context block:

```json
{"action":"record","params":{"kind":"context_block","title":"Detailed handoff","body":"Longer explanation..."}}
```

Hippocampus entry:

```json
{"action":"record","params":{"kind":"hippocampus_entry","text":"Durable project fact.","domain":"release","authority":"medium"}}
```

Agent feedback:

```json
{"action":"record","params":{"kind":"agent_feedback","text":"Prefer bounded file windows.","agent_id":"spec_backend","domain":"backend"}}
```

## Maintenance

Compact logs:

```json
{"action":"maintenance","params":{"action":"compact_logs","dry_run":true}}
```

Backfill signatures:

```json
{"action":"maintenance","params":{"action":"backfill_signatures","dry_run":false}}
```

Candidate-based consolidation:

```json
{"action":"maintenance","params":{"action":"consolidate","dry_run":true}}
```

Full scan consolidation is gated:

```json
{"action":"maintenance","params":{"action":"consolidate_full","confirm_full_scan":true,"dry_run":true}}
```
