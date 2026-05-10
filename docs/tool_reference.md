# Mnemo Tool Reference

Mnemo exposes a stdio MCP server with project-memory tools.

## memory_record

Records a durable memory.

Required:

- `text`

Optional:

- `kind`
- `source`
- `tags`
- `references`
- `supersedes`
- `pinned`

## memory_search

Searches memories relevant to a query.

Required:

- `query`

Optional:

- `kind`
- `limit`
- `include_deleted`
- `include_superseded`
- `pinned`
- `phase`
- `max_tokens`

## memory_salience_check

Optional read-only diagnostics. Requires Agent Salience to be installed or available through `AGENT_SALIENCE_HOME`.

Required:

- `text`

Optional:

- `limit`
- `include_deleted`
- `include_superseded`
- `threshold`

## memory_recent

Returns recent memories.

## memory_update

Updates an existing memory by id.

## memory_delete

Soft-deletes a memory.

## memory_compact_context

Builds a prompt-ready memory context block.

## memory_history

Reads lifecycle events for a memory.

## memory_related

Walks references between memories.

## memory_drift

Compares recent and older memory vocabulary.

## memory_consolidate

Finds or retires near-duplicate memories.

## lookup_symbol

Finds likely source definition locations under `MNEMO_WORKSPACE_ROOT`.
