# Privacy and Local Data

Mnemo is local-first. It does not require a cloud service, hosted database, vector database, or network call.

## Files Mnemo may create

In SQLite mode, Mnemo may create:

- `state/mnemo/mnemo.sqlite`
- `state/mnemo/memory.json` when exporting or importing legacy memory
- `state/mnemo/exports/memory.jsonl`
- `state/mnemo/exports/hippocampus.md`
- `state/mnemo/exports/agent_feedback.md`
- `state/mnemo/exports/startup_context_latest.md`
- temporary lock files

In legacy JSON mode, Mnemo may also create:

- `memory.json`
- `memory.json.tmp`
- `memory.archive.jsonl`
- `queries.jsonl`
- `queries.1.jsonl`
- `queries.archive.jsonl`
- `events.jsonl`
- `events.1.jsonl`
- `events.archive.jsonl`

These are gitignored by default.

## Sensitive content

Memory files and exports may contain:

- project decisions
- code facts
- local file paths
- commands and test outputs
- agent summaries
- task notes
- prompts or conversation summaries
- regulatory or business context entered by users

Review local memory before sharing a repository, ZIP, screenshot, or exported Markdown/JSONL file.

## Workspace access

`mnemo_lookup_symbol` reads files under `MNEMO_WORKSPACE_ROOT` and applies file-count and byte-count limits. Keep the workspace root narrow when possible.

## Optional Agent Salience

Agent Salience integration is optional and local. Mnemo remains functional without it.
