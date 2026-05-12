# Mnemo Memory Layers

Mnemo provides a neutral structured memory substrate for local agentic workflows.

## Layers

- `interaction_log`: short, frequent continuity notes from recent runs.
- `context_block`: larger linked artifacts that capture reusable context.
- `hippocampus_entry`: durable project/system knowledge.
- `agent_feedback`: scoped lessons for a specific `agent_id`, `role`, or `domain`.

## Neutral Metadata

Use metadata fields to model your own team/agent structure:

- `agent_id`
- `role`
- `scope`
- `domain`
- `authority` (`low|medium|high|pinned`)
- `retention` (`ephemeral|compressible|durable|pinned`)
- `confidence` (`low|medium|high`)
- `linked_ids`
- `parent_id`
- `source_run_id`

Mnemo does not require or hardcode personal agent names.

## Recall Bundles

- `mnemo_recall` with `mode="startup"`: startup bundle for coordinator/front-facing role.
- `mnemo_recall` with `mode="agent"`: scoped bundle for specialist role/agent/domain/task.

The recall tool returns bounded structured results and can optionally use salience diagnostics when available.
Use `mnemo_get` to load a full memory body by id when a recall preview is not enough.

## Recording Patterns

Use the single `mnemo_record` tool for all kinds:

- interaction log:
  - `{"kind":"interaction_log","summary":"...", ...}`
- context block:
  - `{"kind":"context_block","body":"...", "title":"...", ...}`
- hippocampus entry:
  - `{"kind":"hippocampus_entry","text":"...", "evidence_ids":[...], ...}`
- agent feedback:
  - `{"kind":"agent_feedback","text":"...", "feedback_type":"warning", ...}`

## Maintenance

Use `mnemo_maintenance` for housekeeping actions.

Compaction mode (`action="compact_logs"`) summarizes older interaction logs into a candidate `context_block`.

- `dry_run: true`: preview only.
- `dry_run: false`: write the new context block.

This version keeps source logs readable (no destructive deletion).

Consolidation mode (`action="consolidate"`) finds near-duplicate clusters and can retire duplicates by superseding to the newest survivor when `dry_run` is false.

Import mode (`action="import_json"`) imports legacy JSON memories into the active backend.
