# Privacy and Local Data

Mnemo is local-first. It does not require a cloud service, network call, external database, or vector database.

## Files Mnemo may create

Depending on configuration, Mnemo may create files under `state/mnemo/` or the paths you configure:

- `mnemo.sqlite`
- `memory.json`
- `memory.json.tmp`
- `memory.archive.jsonl`
- `queries.jsonl` and rotated/archive variants in legacy JSON mode
- `events.jsonl` and rotated/archive variants in legacy JSON mode
- `exports/memory.jsonl`
- `exports/hippocampus.md`
- `exports/agent_feedback.md`
- `exports/startup_context_latest.md`
- lock files

These are gitignored by default.

## SQLite mode

In SQLite mode, lifecycle and query events are stored in the SQLite database. Legacy JSONL files may still exist from older versions or explicit exports, but SQLite is the primary store.

## Sensitive content

Memory and export files may contain:

- project decisions
- code facts
- file paths
- commands
- task notes
- prompts or agent summaries
- agent feedback
- project-specific system knowledge

Review local memory and exports before sharing a repository or ZIP.

## Workspace access

`lookup_symbol` reads files under `MNEMO_WORKSPACE_ROOT` and applies file-count and byte-count limits. Keep the workspace root narrow when possible.

## Network behavior

Mnemo itself does not need network access. Optional Agent Salience integration is local-only when configured through `AGENT_SALIENCE_HOME` or normal Python imports.
