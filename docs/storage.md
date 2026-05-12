# Mnemo Storage (0.10.0)

## Backend selection

- `MNEMO_STORE=sqlite` (default): SQLite primary store.
- `MNEMO_STORE=json`: legacy JSON store.

SQLite path resolution:

- `MNEMO_SQLITE_FILE` when set.
- otherwise `<workspace>/state/mnemo/mnemo.sqlite`.

Compatibility JSON path resolution:

- `MNEMO_FILE` when set.
- otherwise `<workspace>/state/mnemo/memory.json`.

## SQLite schema

Main tables:

- `memories`
- `links`
- `events`
- `meta`

`hippocampus_entry` remains a memory kind in `memories` (not a separate DB).

`references` is treated as an API compatibility alias of `linked_ids` in Python logic.

## Bootstrap/import behavior

On first SQLite startup when the DB has no memories:

1. Import from `memory.json` when present.
2. If missing, import from sibling `memory.example.json` when present.
3. Import `memory.archive.jsonl` records (skip duplicate ids/hashes).
4. Ingest `events*.jsonl` and `queries*.jsonl` rows into SQLite `events`.

Legacy files are left on disk; they are not deleted.

## Event/query authority

In SQLite mode, new lifecycle/query events are written to SQLite `events`.
Mnemo no longer writes new `events.jsonl` / `queries.jsonl` in SQLite mode.

## Exports

Use `mnemo_export`:

- `jsonl` -> `state/mnemo/exports/memory.jsonl`
- `json` -> `state/mnemo/exports/memory.json`
- `hippocampus_markdown` -> `state/mnemo/exports/hippocampus.md`
- `agent_feedback_markdown` -> `state/mnemo/exports/agent_feedback.md`
- `startup_context_markdown` -> `state/mnemo/exports/startup_context_latest.md`

