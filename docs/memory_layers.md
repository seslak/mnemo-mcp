# Mnemo memory layers

Mnemo stores all project memory in one local store. In SQLite mode the primary file is `state/mnemo/mnemo.sqlite`.

The public MCP surface is one gateway tool: `mnemo`. Use `action` plus optional `params`.

## Layers

- `interaction_log`: short recent continuity logs.
- `context_block`: larger linked artifacts that expand one or more logs/findings.
- `hippocampus_entry`: durable project/system knowledge.
- `agent_feedback`: feedback scoped to an agent, role, or domain.

Hippocampus entries are not stored in a separate database. They are normal memories with `kind="hippocampus_entry"` plus metadata such as `domain`, `scope`, `authority`, and `retention`.

## Examples

Record an interaction log:

```json
{"action":"record","params":{"kind":"interaction_log","summary":"User decided to keep Mnemo as SQLite-backed project memory.","role":"coordinator"}}
```

Record a linked context block:

```json
{"action":"record","params":{"kind":"context_block","title":"Mnemo storage decision","body":"SQLite is the primary store; JSONL/Markdown are exports.","linked_ids":["mem_log_id"]}}
```

Record a hippocampus entry:

```json
{"action":"record","params":{"kind":"hippocampus_entry","text":"Mnemo uses one SQLite store; hippocampus entries are distinguished by kind and metadata.","domain":"agentic/memory","authority":"high"}}
```

Record agent feedback:

```json
{"action":"record","params":{"kind":"agent_feedback","text":"When writing project memory, use Mnemo gateway actions, not native assistant memory.","role":"coordinator","domain":"agentic/memory"}}
```

Recall startup context:

```json
{"action":"recall","params":{"mode":"startup","role":"coordinator","query":"Mnemo storage and gateway design"}}
```

Recall specialist context:

```json
{"action":"recall","params":{"mode":"agent","agent_id":"memory-specialist","domain":"agentic/memory","task":"review memory persistence"}}
```

Load one full memory:

```json
{"action":"get","params":{"id":"mem_123","full":true}}
```
