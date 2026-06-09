---
description: "Unified Mnemo Memory Pack landing-folder import workflow"
agent: 'agent'
tools: ['nexus']
---
# Mnemo Memory-Pack Import

Use this prompt for the complete inspect/import/review workflow.

If invoked with no input, list available `.mem` packs from the landing folder and let the user choose one.

If invoked with a pack path or pack filename, resolve that pack and continue directly into inspect and import.

Stop before promotion unless the user explicitly asks to promote.

## Guardrails

- Use at most one `nexus.list_actions` capability check.
- Use `mnemo.pack_landing_list` when no explicit `pack_path` is provided.
- Always run `mnemo.pack_inspect` first.
- Always show `status`, `trust_classification`, `trusted_import_available`, row count, and file_fingerprint path count.
- Ask for exactly one import-mode decision after inspect.
- Recommend quarantine by default.
- Trusted import is NOT local adoption.
- Trusted import is NOT automatic promotion.
- Trusted import is NOT default retrieval.
- Never auto-promote.
- Always run `mnemo.pack_review_import` after import.
- Use `include_grouped_summary=true` for review.
- Only run `mnemo.pack_promote_preview` and `mnemo.pack_promote` if the user explicitly asks to promote.
- `confirm_promote=true` is required for promotion.

## Capability Check

Call once if needed:

```json
{
  "action": "nexus.list_actions",
  "params": {}
}
```

Required actions:
- `mnemo.pack_landing_list`
- `mnemo.pack_inspect`
- `mnemo.pack_import`
- `mnemo.pack_review_import`

Optional promotion actions:
- `mnemo.pack_promote_preview`
- `mnemo.pack_promote`

## No Input: Landing Folder Browse

List packs from the landing folder:

```json
{
  "action": "mnemo.pack_landing_list",
  "params": {
    "limit": 20,
    "include_legacy_zip": false
  }
}
```

Show a compact list with:
- number
- filename
- path
- size
- modified time

If the user gives only a filename, resolve it from the landing-folder list before inspect.

## Inspect First

```json
{
  "action": "mnemo.pack_inspect",
  "params": {
    "pack_path": "D:/packs/example.mem",
    "include_samples": false,
    "sample_limit": 5
  }
}
```

Show:
- filename/path
- status
- schema version
- signed/unsigned and trust classification
- trusted_import_available
- row count
- topics/groups summary
- file_fingerprints path count

## Import Decision

Ask one question:
- quarantine import
- trusted import, only if `trusted_import_available=true` and the operator can provide `verification_secret`

Recommend quarantine by default.

Quarantine import:

```json
{
  "action": "mnemo.pack_import",
  "params": {
    "pack_path": "D:/packs/example.mem",
    "allow_unsigned_quarantine": true
  }
}
```

Trusted import:

```json
{
  "action": "mnemo.pack_import",
  "params": {
    "pack_path": "D:/packs/example.mem",
    "allow_trusted_import": true,
    "verification_secret": "<operator secret>"
  }
}
```

## Review After Import

```json
{
  "action": "mnemo.pack_review_import",
  "params": {
    "pack_id": "pack_...",
    "include_samples": true,
    "sample_limit": 10,
    "include_grouped_summary": true
  }
}
```

Show grouped summary, then stop.

## Optional Promotion Only If Asked

If the user explicitly asks to promote:
1. run `mnemo.pack_promote_preview`
2. show preview
3. ask approval
4. run `mnemo.pack_promote` with `confirm_promote=true`

## Result Summary

Always summarize:
- import mode
- import namespace
- `pack_id`
- trust level
- grouped review summary
- exact next command to inspect, review again, or promote
