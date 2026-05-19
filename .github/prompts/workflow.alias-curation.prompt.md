---
name: workflow.alias-curation
description: Review Mnemo alias proposals and curate approved aliases into Mnemo SQLite.
tools: ['nexus']
argument-hint: Optional filters such as domain, window_days, min_recurrence, or limit.
---

# Workflow: Alias Curation

You are curating Mnemo aliases.

Aliases are dynamic project/user/domain retrieval knowledge stored in Mnemo SQLite. They are not JSON config files.

Do not edit `aliases.json`.
Do not edit `aliases.example.json`.
Do not touch `.agentic/vocabulary/aliases.json`.
Do not touch `.agentic/vocabulary/aliases.example.json`.
Do not create generic alias packs.
Do not approve aliases only because they sound plausible.

## Goal

Review evidence-backed alias proposals from Mnemo miss events and alias hints, then approve, reject, or defer them through Mnemo maintenance actions.

Approved aliases must be written through Mnemo SQLite actions only.

## Required Sequence

### 1. Preview proposals without writing

Call Mnemo through Nexus:

```json
{
  "action": "mnemo.maintenance",
  "params": {
    "action": "propose_aliases",
    "dry_run": true
  }
}
```

Include any user-provided filters, for example:

```json
{
  "action": "mnemo.maintenance",
  "params": {
    "action": "propose_aliases",
    "dry_run": true,
    "window_days": 30,
    "domain": "agentic",
    "min_recurrence": 3,
    "limit": 20
  }
}
```

This preview is read-only.

Important: proposals returned from `dry_run=true` are not persisted and cannot be approved or rejected by `proposal_id`.

### 2. Review preview evidence

For each previewed proposal, inspect:

- candidate alias
- canonical concept or target wording
- domain
- language
- recurrence count
- miss queries
- alias_hint evidence
- target memory previews
- IDF terms
- penalized/common terms
- proposal score

Reject mentally before persistence if the proposal is:

- generic
- low-IDF/common-word driven
- ambiguous across domains
- not supported by repeated misses or alias hints
- only linguistically plausible but not evidenced

### 3. Ask before persisting

Before writing proposals into Mnemo SQLite, tell the user that previewed proposals are not yet persisted.

Ask whether to persist the reviewable proposal set.

Do not continue to approval/rejection by `proposal_id` until proposals have been persisted.

### 4. Persist pending proposals

If the user approves persistence, call:

```json
{
  "action": "mnemo.maintenance",
  "params": {
    "action": "propose_aliases",
    "dry_run": false
  }
}
```

Use the same filters used for preview unless the user changed them.

This writes pending rows into `alias_proposals`.

### 5. List persisted proposals

Call:

```json
{
  "action": "mnemo.maintenance",
  "params": {
    "action": "list_alias_proposals",
    "status": "pending"
  }
}
```

Apply domain/status/limit filters when relevant.

Only proposals returned from this persisted list should be approved or rejected by `proposal_id`.

### 6. Curate persisted proposals

For each persisted proposal, decide one of:

- approve
- reject
- defer

Approve only when the evidence is strong.

To approve:

```json
{
  "action": "mnemo.maintenance",
  "params": {
    "action": "approve_alias",
    "proposal_id": "<proposal_id>",
    "approved_by": "workflow.alias-curation"
  }
}
```

If the canonical concept should be edited before approval, pass corrected fields supported by the tool, such as:

```json
{
  "action": "mnemo.maintenance",
  "params": {
    "action": "approve_alias",
    "proposal_id": "<proposal_id>",
    "concept_id": "<concept_id>",
    "canonical": "<canonical>",
    "domain": "<domain>",
    "language": "<language>",
    "approved_by": "workflow.alias-curation",
    "notes": "<short reason>"
  }
}
```

To reject:

```json
{
  "action": "mnemo.maintenance",
  "params": {
    "action": "reject_alias_proposal",
    "proposal_id": "<proposal_id>",
    "reason": "<short reason>"
  }
}
```

To defer, leave the proposal pending and mention why.

### 7. Verify vocabulary state

After approval/rejection, call:

```json
{
  "action": "mnemo.maintenance",
  "params": {
    "action": "list_aliases"
  }
}
```

Optionally call doctor:

```json
{
  "action": "mnemo.doctor",
  "params": {}
}
```

Confirm that active aliases are stored in Mnemo SQLite and that pending proposal counts are reasonable.

## Approval Rules

Approve aliases only when at least one of these is true:

- repeated miss evidence shows the wording gap
- alias_hint evidence confirms a successful rephrase
- target memory evidence is clear
- IDF terms support domain-specific meaning
- domain/language scope is clear

Reject aliases when:

- the alias is too generic
- the alias is a common word
- the alias is ambiguous across domains
- the evidence is weak
- the proposal would create misleading retrieval matches
- the proposal is based only on surface wording similarity

## Safety Rules

- Never write aliases to JSON.
- Never edit `.agentic/vocabulary/aliases.json`.
- Never edit `.agentic/vocabulary/aliases.example.json`.
- Never approve bulk proposals without review.
- Never turn low-IDF/common terms into aliases.
- Preserve domain and language scope.
- Prefer fewer high-quality aliases over many weak aliases.

## Final Report

Return:

### Summary

What was reviewed and curated.

### Proposal Preview

Number of previewed proposals and filters used.

### Persisted Proposals

Number of proposals persisted into SQLite.

### Approved Aliases

List approved aliases with concept/domain.

### Rejected Proposals

List rejected proposals and reasons.

### Deferred Proposals

List deferred proposals and why.

### Verification

Mention `list_aliases` and/or `doctor` results.

### Remaining Risks

Mention ambiguous aliases, low corpus maturity, cold IDF, or proposals needing more evidence.
