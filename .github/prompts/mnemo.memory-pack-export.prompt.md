---
description: "Unified Mnemo Memory Pack browse and export workflow"
agent: 'agent'
tools: ['nexus', 'vscode_askQuestions']
---
# Mnemo Memory-Pack Export

Use this prompt for the complete export UX.

If invoked with no input, browse exportable topics/groups first and then continue directly into preview, redaction preview, approval, and export in the same prompt.

If invoked with input, resolve that input deterministically and continue directly into preview, redaction preview, approval, and export.

Do not require a handoff object for normal use.

## Guardrails

- Use at most one `nexus.list_actions` capability check.
- Do not inspect source or docs during UX.
- Ask one question at a time.
- Keep tables short.
- Never use fuzzy query results as the final export selector.
- Do not call `mnemo.search`.
- Do not transfer raw `memory_ids` through chat or placeholders.
- Do not emit or rely on any angle-bracket memory-id placeholder.
- Use `group_id` and `scope` directly with `mnemo.pack_preview`, `mnemo.pack_redaction_preview`, and `mnemo.pack_export`.
- `mnemo.memory_group_preview` is optional only for extra summary or troubleshooting and should normally be skipped.
- Use `catalog.options` directly as the dynamic source options.
- Do not reconstruct option values.
- Do not create option values from labels.
- Do not invent group IDs.
- If `catalog.options` is non-empty, the agent must call `vscode_askQuestions` with those options.
- The agent must never say "No suitable list available" when `catalog.options` is non-empty.
- Run `mnemo.pack_preview` before `mnemo.pack_export`.
- Run `mnemo.pack_redaction_preview` before `mnemo.pack_export`.
- Require explicit approval before `mnemo.pack_export`.
- Never call `mnemo.pack_export` without `pack_name`.
- Never call `mnemo.pack_export` without either `allow_unsigned=true` or `sign_pack=true`.
- Never call `mnemo.pack_export` more than once unless the first export failed and the user approves retry.
- If `mnemo.pack_preview` shows `limited=true`, do not export until the operator either increases `limit` or explicitly approves `allow_limited_export=true`.
- The output extension is `.mem`.
- `.mem` is still a ZIP container internally.
- `content/file_fingerprints.json` contains touched-file paths and hashes, not file contents.
- If signing is used, remind the operator that local HMAC is not public-key identity and not non-repudiation.

## Capability Check

Call `nexus.list_actions` once:

```json
{
  "action": "nexus.list_actions",
  "params": {}
}
```

Required actions:
- `mnemo.memory_group_discover`
- `mnemo.pack_preview`
- `mnemo.pack_redaction_preview`
- `mnemo.pack_export`

## Known Tool Call Contract

All calls go through Nexus:

```json
{
  "action": "mnemo.<subaction>",
  "params": {}
}
```

## Mode A: No Input

When the user provides no input, treat it as browse-and-export.

Call catalog mode:

```json
{
  "action": "mnemo.memory_group_discover",
  "params": {
    "output_mode": "catalog",
    "catalog_for": "export",
    "limit_groups": 10,
    "include_raw_groups": false
  }
}
```

For Show more:

```json
{
  "action": "mnemo.memory_group_discover",
  "params": {
    "output_mode": "catalog",
    "catalog_for": "export",
    "limit_groups": 50,
    "include_raw_groups": false
  }
}
```

Question options:
- Dynamic source options are exactly `catalog.options`
- Append fixed options:
  - `Show more sources`
  - `Search by phrase`
  - `Cancel`

Use `catalog.options` directly in `vscode_askQuestions`.

If the user selects a dynamic option:
- selected value is the exact `group_id`
- do not transform it
- ask for scope if needed:
  - `core`
  - `core_plus_related`
  - `full_tree`

If the user selects `Search by phrase`:
- ask for a freeform phrase through `vscode_askQuestions`
- filter the latest `catalog.options` only by:
  - case-insensitive label contains phrase
  - case-insensitive phrase contains label
  - case-insensitive value/group_id contains phrase
- if no match exists, say no catalog match was found and offer `Show more sources` or `Cancel`
- do not call `mnemo.search`

## Mode B: Input Resolution

Resolve input in this order:

1. exact topic
2. exact group_id
3. exact or near group label from catalog options
4. explicit memory_ids advanced mode

Accepted inputs:
- exact topic string
- exact group_id
- group label
- close topic/group label phrase
- explicit memory_ids list

Rules:
- exact topic wins before group label matching
- if input is exact group_id, use that exact value directly with pack actions
- if input matches one catalog option label/value/group_id, use that exact option value
- if multiple catalog matches exist, show a short numbered choice using those exact catalog options
- do not invent memory_ids
- do not create a temporary export topic unless the user explicitly asks for a reusable topic

## Preview and Redaction

For exact topic:

```json
{
  "action": "mnemo.pack_preview",
  "params": {
    "topics": ["<topic>"],
    "kinds": ["context_block", "hippocampus_entry"],
    "include_samples": true,
    "sample_per_kind": 3,
    "limit": 200
  }
}
```

For exact group_id:

```json
{
  "action": "mnemo.pack_preview",
  "params": {
    "group_id": "<exact-group-id-from-catalog>",
    "scope": "core_plus_related",
    "kinds": ["context_block", "hippocampus_entry"],
    "include_samples": true,
    "sample_per_kind": 3,
    "limit": 200
  }
}
```

For explicit memory_ids advanced mode:

```json
{
  "action": "mnemo.pack_preview",
  "params": {
    "memory_ids": ["mem_..."],
    "kinds": ["context_block", "hippocampus_entry"],
    "include_samples": true,
    "sample_per_kind": 3,
    "limit": 200
  }
}
```

Run `mnemo.pack_redaction_preview` exactly once with the same selector shape:
- topic stays topic
- group selector stays `group_id` plus `scope`
- explicit advanced `memory_ids` stay `memory_ids`

## Approval Summary

Before approval, summarize:
- selected topic/group/input
- selector mode
- selected row count
- exportable row count
- excluded or non-exportable kinds
- sample titles
- redaction categories/counts
- file_fingerprints path-disclosure note
- proposed pack_name
- `.mem` output
- unsigned vs signed mode

## Export

Unsigned exact group selector:

```json
{
  "action": "mnemo.pack_export",
  "params": {
    "pack_name": "<approved-pack-name>",
    "group_id": "<exact-group-id-from-catalog>",
    "scope": "core_plus_related",
    "kinds": ["context_block", "hippocampus_entry"],
    "allow_unsigned": true
  }
}
```

Unsigned exact topic:

```json
{
  "action": "mnemo.pack_export",
  "params": {
    "pack_name": "<approved-pack-name>",
    "topics": ["<topic>"],
    "kinds": ["context_block", "hippocampus_entry"],
    "allow_unsigned": true
  }
}
```

Signed exact group selector:

```json
{
  "action": "mnemo.pack_export",
  "params": {
    "pack_name": "<approved-pack-name>",
    "group_id": "<exact-group-id-from-catalog>",
    "scope": "core_plus_related",
    "kinds": ["context_block", "hippocampus_entry"],
    "sign_pack": true,
    "signer_id": "<approved-signer-id>",
    "signing_secret": "<operator-provided-secret>"
  }
}
```

Signed exact topic:

```json
{
  "action": "mnemo.pack_export",
  "params": {
    "pack_name": "<approved-pack-name>",
    "topics": ["<topic>"],
    "kinds": ["context_block", "hippocampus_entry"],
    "sign_pack": true,
    "signer_id": "<approved-signer-id>",
    "signing_secret": "<operator-provided-secret>"
  }
}
```

Only use explicit `memory_ids` when the operator supplied them directly in advanced mode.

## Result Summary

Always return:
- `pack_id`
- `pack_name`
- `output_path` ending with `.mem`
- exported rows
- signed/unsigned
- redaction summary
- file_fingerprints note: paths and hashes, not file contents
- next step: `/mnemo.memory-pack-import` on another machine or `mnemo.pack_inspect`
