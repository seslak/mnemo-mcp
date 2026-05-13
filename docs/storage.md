# Mnemo storage

Mnemo is local-first and copy/paste friendly.

## SQLite primary store

Default primary store:

```text
state/mnemo/mnemo.sqlite
```

SQLite is used through Python's standard-library `sqlite3` module. No external database is required.

## Compatibility files

`memory.json` remains available as a compatibility/import/export format, but it is not the primary store in SQLite mode.

Readable exports can be written with the gateway export action:

```json
{"action":"export","params":{"format":"jsonl"}}
```

Common exports:

```text
state/mnemo/exports/memory.jsonl
state/mnemo/exports/hippocampus.md
state/mnemo/exports/agent_feedback.md
state/mnemo/exports/startup_context_latest.md
```

## Events

In SQLite mode, lifecycle and query events are stored in the SQLite `events` table. Legacy `events.jsonl` and `queries.jsonl` files can be imported but are no longer authoritative.

In JSON mode, legacy JSON/JSONL behavior is still supported.

## Compaction

Recent interaction logs can stay raw while older logs are compacted into context blocks:

```json
{"action":"maintenance","params":{"action":"compact_logs","dry_run":true}}
```

Raw logs are retained. Compaction is deterministic and local.

## Health checks

Use:

```json
{"action":"doctor"}
```

Doctor reports backend, SQLite file, memory counts, export status, FTS availability, warnings, recommendations, and available gateway actions.
