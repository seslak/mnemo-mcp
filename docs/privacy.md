# Privacy and Local Data

Mnemo is local-first. It does not need a cloud service, database, or network call.

## Files Mnemo may create

Depending on configuration, Mnemo may create:

- `memory.json`
- `memory.json.tmp`
- `memory.archive.jsonl`
- `queries.jsonl`
- `queries.1.jsonl`
- `queries.archive.jsonl`
- `events.jsonl`
- `events.1.jsonl`
- `events.archive.jsonl`
- lock files

These are gitignored by default.

## Sensitive content

Memory and log files may contain:

- project decisions
- code facts
- file paths
- commands
- task notes
- prompts or agent summaries

Review local memory before sharing a repository.

## Workspace access

`lookup_symbol` reads files under `MNEMO_WORKSPACE_ROOT` and applies file-count and byte-count limits. Keep the workspace root narrow when possible.
