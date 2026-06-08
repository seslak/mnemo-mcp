---
description: "Scripted Mnemo Memory Pack promotion wizard. Promotes staged imported Memory Pack rows into regular local memories with explicit allow_promote_all gate."
argument-hint: "[optional pack_id/pack name]"
agent: 'agent'
tools: ['nexus', 'vscode_askQuestions']
---

# /mnemo.memory-pack-promote - Scripted Memory Pack Promotion Wizard

You are running the Mnemo Memory Pack promotion wizard.

This prompt is a scripted wizard. Follow the steps exactly. Do not improvise the workflow, wording, option labels, tool sequence, approval sequence, or post-promotion behavior.

Promotion materializes staged imported pack rows from `imported_pack_rows` into regular local rows in `memories`.

Normal workflow:

```text
list or resolve imported pack -> promotion preview -> approval -> promote -> stop
```

---

## Terminal rule

DO NOT END THE TURN until one terminal condition is reached:

1. `## Promotion Completed` has been printed, OR
2. `Memory Pack promotion cancelled.` has been printed, OR
3. `Memory Pack promotion stopped.` has been printed, OR
4. a required Mnemo/Nexus action is unavailable, OR
5. a required tool call fails and this prompt explicitly says to stop.

After any tool call, your immediate next action MUST be the next step in this prompt. Do not write transition prose.

When a terminal condition is reached, stop. Do not ask a follow-up question. Do not offer another promotion. Do not ask "Would you like me to...".

If the user invokes `/mnemo.memory-pack-promote` again in the same chat after a completed promotion, treat it as a brand-new promotion run.

---

## Hard rules

- Every user-facing decision MUST go through `vscode_askQuestions`.
- Do not ask chat-only questions.
- Do not present numbered chat-choice lists.
- Do not use different wording between runs.
- Use the exact `header`, `question`, fixed `options.label`, fixed `options.value`, and fixed `options.description` strings specified below.
- Dynamic imported-pack options from Mnemo are the only dynamic pack option labels.
- Do not call `nexus.start_interaction`.
- Do not call `nexus.finish_interaction`.
- Do not call `nexus.status`.
- Do not call `router.*`.
- Do not call `thrift.*`.
- Do not call `mnemo.search`.
- Do not read source files.
- Do not read docs.
- Do not inspect implementation files.
- Do not print raw Nexus/Mnemo JSON.
- Do not read session-store files such as `content.json`.
- Do not query or read tool-output files.
- If a tool response is too large or hidden behind a file link, use only the visible top-level summary and continue; do not open the file.
- Do not print capability-check output.
- Do not print transition prose such as "I will...", "Now I will...", "Next I will...", "Would you like me to...", "Checking...", "Listing...", "Requesting...", "Building...", "Debugging...", "Mapping...", or "Evaluating...".
- Do not invent pack IDs.
- A pack ID used in tool calls MUST be copied exactly from `pack_list_imports` output or from an explicit user-provided pack_id.
- Always call `mnemo.pack_promote_preview` before `mnemo.pack_promote`.
- Do not call `mnemo.pack_promote` before final approval.
- Call `mnemo.pack_promote` exactly once, unless it fails and the user chooses `Retry same promotion`.
- Promotion requires `confirm_promote=true`.
- If promoting the full previewed pack without row filters, `mnemo.pack_promote` also requires `allow_promote_all=true`.
- `allow_promote_all=true` may be used only after the explicit `Approve promotion` form has been accepted.

---

## Allowed tool actions

Only these actions are allowed:

- `vscode_askQuestions`
- `nexus.list_actions`
- `mnemo.pack_list_imports`
- `mnemo.pack_promote_preview`
- `mnemo.pack_promote`

No other action is allowed.

---

## Runtime constants

Use these constants exactly:

- `IMPORT_LIST_LIMIT_INITIAL = 20`
- `IMPORT_LIST_LIMIT_MORE = 50`
- `SAMPLE_LIMIT = 10`

---

## Fixed form syntax

When this prompt says "Call `vscode_askQuestions` with:", make exactly one `vscode_askQuestions` call using the shown `questions` array.

Do not paraphrase `header`, `question`, fixed `label`, fixed `value`, or fixed `description`.

If a form is cancelled, dismissed, returns no answer, or returns an error, print exactly:

`Memory Pack promotion cancelled.`

Then exit.

---

## Step 0 - Parse invocation

If the slash command was invoked with an argument, store it as `USER_INPUT`.

If no argument was provided, set `USER_INPUT = ""`.

Do not ask the user whether to browse or provide a pack ID.

Immediately continue to Step 1.

---

## Step 1 - Capability check

Call exactly once:

```json
{
  "action": "nexus.list_actions",
  "params": {}
}
```

Required actions:

- `mnemo.pack_list_imports`
- `mnemo.pack_promote_preview`
- `mnemo.pack_promote`

If any required action is missing, print exactly:

`Memory Pack promotion cannot continue because required Mnemo action <action_name> is unavailable.`

Then exit.

If all actions are available:

- If `USER_INPUT = ""`, continue to Step 2A.
- If `USER_INPUT != ""`, continue to Step 2B.

Do not print a capability summary.

---

## Step 2A - Browse imported packs

Call exactly once:

```json
{
  "action": "mnemo.pack_list_imports",
  "params": {
    "limit": 20
  }
}
```

Treat the response as internal.

Build:

```text
IMPORT_OPTIONS = imported packs returned by pack_list_imports
IMPORT_MAP[pack_id] = imported pack object
```

Every dynamic imported-pack option MUST use exactly:

```yaml
label: "<pack_name> (<pack_id>)"
value: "<pack_id>"
description: "<trust_level> - <imported row count> rows - <namespace>"
```

If `pack_name` is empty, use:

```yaml
label: "<pack_id>"
value: "<pack_id>"
description: "<trust_level> - <imported row count> rows - <namespace>"
```

If `vscode_askQuestions` returns the label instead of the value, recover deterministically by extracting the `pack_...` token from the label, or by exact label match against `IMPORT_OPTIONS`. Do not print debugging text.

If `IMPORT_OPTIONS` is non-empty, call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo promote - pack"
    question: "Choose imported Memory Pack"
    options:
      - label: "<dynamic pack_name> (<dynamic pack_id>)"
        value: "<dynamic exact pack_id>"
        description: "<dynamic trust_level> - <dynamic imported row count> rows - <dynamic namespace>"
      - label: "Show more imported packs"
        value: "__show_more__"
        description: "Show up to 50 imported Memory Packs"
      - label: "Filter imported packs by phrase"
        value: "__filter__"
        description: "Filter by pack ID, pack name, namespace, or trust level"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without promotion"
    allowFreeformInput: false
```

Only dynamic imported-pack options may vary. The fixed options must be exactly as shown.

If selected value is:

- `__cancel__` -> print exactly `Memory Pack promotion cancelled.` and exit.
- `__show_more__` -> continue to Step 2A-more.
- `__filter__` -> continue to Step 2C.
- any other value -> resolve `PACK_ID` by exact selected value, exact selected label, or embedded `pack_...` token; then continue to Step 3.

If `IMPORT_OPTIONS` is empty, print exactly:

`No imported Memory Packs were found for promotion.`

Then exit.

---

## Step 2A-more - Browse expanded imported packs

Call exactly once:

```json
{
  "action": "mnemo.pack_list_imports",
  "params": {
    "limit": 50
  }
}
```

Rebuild:

```text
IMPORT_OPTIONS = imported packs returned by pack_list_imports
IMPORT_MAP[pack_id] = imported pack object
```

If `IMPORT_OPTIONS` is empty, print exactly:

`No imported Memory Packs were found for promotion.`

Then exit.

Call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo promote - pack"
    question: "Choose imported Memory Pack"
    options:
      - label: "<dynamic pack_name> (<dynamic pack_id>)"
        value: "<dynamic exact pack_id>"
        description: "<dynamic trust_level> - <dynamic imported row count> rows - <dynamic namespace>"
      - label: "Filter imported packs by phrase"
        value: "__filter__"
        description: "Filter by pack ID, pack name, namespace, or trust level"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without promotion"
    allowFreeformInput: false
```

If selected value is:

- `__cancel__` -> print exactly `Memory Pack promotion cancelled.` and exit.
- `__filter__` -> continue to Step 2C.
- any other value -> resolve `PACK_ID` by exact selected value, exact selected label, or embedded `pack_...` token; then continue to Step 3.

---

## Step 2B - Resolve input

Call exactly once:

```json
{
  "action": "mnemo.pack_list_imports",
  "params": {
    "limit": 50
  }
}
```

Build:

```text
IMPORT_OPTIONS = imported packs returned by pack_list_imports
IMPORT_MAP[pack_id] = imported pack object
```

Resolve `USER_INPUT` using:

1. exact pack_id match
2. exact case-insensitive pack_name match
3. case-insensitive pack_id contains `USER_INPUT`
4. case-insensitive pack_name contains `USER_INPUT`
5. case-insensitive namespace contains `USER_INPUT`

If exactly one option matches, set `PACK_ID = option.pack_id` and continue to Step 3.

If multiple options match, call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo promote - pack"
    question: "Choose imported Memory Pack"
    options:
      - label: "<dynamic pack_name> (<dynamic pack_id>)"
        value: "<dynamic exact pack_id>"
        description: "<dynamic trust_level> - <dynamic imported row count> rows - <dynamic namespace>"
      - label: "Filter imported packs by phrase"
        value: "__filter__"
        description: "Filter by pack ID, pack name, namespace, or trust level"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without promotion"
    allowFreeformInput: false
```

If selected value is:

- `__cancel__` -> print exactly `Memory Pack promotion cancelled.` and exit.
- `__filter__` -> continue to Step 2C.
- any other value -> resolve `PACK_ID` by exact selected value, exact selected label, or embedded `pack_...` token; then continue to Step 3.

If no option matches and `USER_INPUT` starts with `pack_`, set `PACK_ID = USER_INPUT` and continue to Step 3.

If no option matches, continue to Step 2C.

---

## Step 2C - Filter imported packs by phrase

Call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo promote - filter"
    question: "Enter imported pack filter phrase"
    allowFreeformInput: true
```

Store the answer as `FILTER_PHRASE`.

If `FILTER_PHRASE` is empty, print exactly:

`No imported Memory Pack was selected.`

Then exit.

If `IMPORT_OPTIONS` is missing or empty, call:

```json
{
  "action": "mnemo.pack_list_imports",
  "params": {
    "limit": 50
  }
}
```

Rebuild `IMPORT_OPTIONS` and `IMPORT_MAP`.

Filter `IMPORT_OPTIONS` using only:

1. case-insensitive pack_id contains `FILTER_PHRASE`
2. case-insensitive pack_name contains `FILTER_PHRASE`
3. case-insensitive namespace contains `FILTER_PHRASE`
4. case-insensitive trust_level contains `FILTER_PHRASE`

If there are no matches, call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo promote - filter"
    question: "No matching imported Memory Pack was found"
    options:
      - label: "Try another phrase"
        value: "__filter__"
        description: "Enter a different imported pack filter phrase"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without promotion"
    allowFreeformInput: false
```

If selected value is `__filter__`, repeat Step 2C once.

If no match is found after the second phrase, print exactly:

`No matching imported Memory Pack was found.`

Then exit.

If matches exist, call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo promote - pack"
    question: "Choose imported Memory Pack"
    options:
      - label: "<dynamic pack_name> (<dynamic pack_id>)"
        value: "<dynamic exact pack_id>"
        description: "<dynamic trust_level> - <dynamic imported row count> rows - <dynamic namespace>"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without promotion"
    allowFreeformInput: false
```

If selected value is:

- `__cancel__` -> print exactly `Memory Pack promotion cancelled.` and exit.
- any other value -> resolve `PACK_ID` by exact selected value, exact selected label, or embedded `pack_...` token; then continue to Step 3.

---

## Step 3 - Promotion preview

Call exactly once:

```json
{
  "action": "mnemo.pack_promote_preview",
  "params": {
    "pack_id": "<PACK_ID>",
    "include_samples": true,
    "sample_limit": 10
  }
}
```

If preview status is not ok, print exactly:

`Selected imported Memory Pack could not be previewed for promotion.`

Then exit.

Set values from output:

```text
PACK_NAME = pack.pack_name
TRUST_LEVEL = pack.trust_level
IMPORT_NAMESPACE = pack.namespace
PROMOTE_SELECTED_ROWS = selection.selected_rows
PROMOTE_LIMITED = selection.limited
PROMOTE_TARGET_NAMESPACE = promotion_plan.target_namespace
PROMOTE_TARGET_ORIGIN = promotion_plan.target_origin
PROMOTE_WOULD_CREATE_MEMORY_COUNT = promotion_plan.would_create_memory_count
PROMOTE_WOULD_COPY_TOPIC_COUNT = promotion_plan.would_copy_topic_count
PROMOTE_WOULD_COPY_MEMORY_FILE_COUNT = promotion_plan.would_copy_memory_file_count
PROMOTE_WOULD_PRESERVE_GIT_PROVENANCE = promotion_plan.would_preserve_git_provenance
PROMOTE_WOULD_PRESERVE_PACK_PROVENANCE = promotion_plan.would_preserve_pack_provenance
PROMOTE_SAMPLE_SUMMARY = up to 10 candidate row summaries
```

Print exactly this heading block:

```text
## Promotion Preview

- Pack ID: <PACK_ID>
- Pack name: <PACK_NAME>
- Trust level: <TRUST_LEVEL>
- Import namespace: <IMPORT_NAMESPACE>
- Selected rows: <PROMOTE_SELECTED_ROWS>
- Target namespace: <PROMOTE_TARGET_NAMESPACE>
- Target origin: <PROMOTE_TARGET_ORIGIN>
- Would create memories: <PROMOTE_WOULD_CREATE_MEMORY_COUNT>
- Would copy topics: <PROMOTE_WOULD_COPY_TOPIC_COUNT>
- Would copy memory-file links: <PROMOTE_WOULD_COPY_MEMORY_FILE_COUNT>
- Preserve git provenance: <PROMOTE_WOULD_PRESERVE_GIT_PROVENANCE>
- Preserve pack provenance: <PROMOTE_WOULD_PRESERVE_PACK_PROVENANCE>
- Samples: <PROMOTE_SAMPLE_SUMMARY>
```

If `PROMOTE_WOULD_CREATE_MEMORY_COUNT` is zero, print exactly:

`Memory Pack promotion stopped.`

Then exit.

Immediately continue to Step 4.

---

## Step 4 - Final approval

This approval authorizes promotion of all rows shown in the promotion preview. Because no row filters are supplied, the later `mnemo.pack_promote` call MUST include `allow_promote_all=true`.

Call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo promote - approval"
    question: "Approve promotion"
    options:
      - label: "Approve promotion"
        value: "approve_promotion"
        description: "Create regular local memories from all previewed staged rows"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without promotion"
        recommended: true
    allowFreeformInput: false
```

If selected value is:

- `approve_promotion` -> continue to Step 5.
- `__cancel__` -> print exactly `Memory Pack promotion cancelled.` and exit.

---

## Step 5 - Promote

Call exactly once:

```json
{
  "action": "mnemo.pack_promote",
  "params": {
    "pack_id": "<PACK_ID>",
    "confirm_promote": true,
    "allow_promote_all": true
  }
}
```

If promotion succeeds, continue to Step 6.

If promotion fails with `promote_all_requires_explicit_allow`, this prompt is stale or the required safety flag was omitted. Print exactly:

```text
## Promotion Failed

- Error: promote all requires allow_promote_all=true
```

Then exit.

If promotion fails, print:

```text
## Promotion Failed

- Error: <error>
```

Then call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo promote - retry"
    question: "Choose retry action"
    options:
      - label: "Retry same promotion"
        value: "retry_same_promotion"
        description: "Run the same promotion once more"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without promotion"
    allowFreeformInput: false
```

If selected value is:

- `retry_same_promotion` -> retry Step 5 exactly once.
- `__cancel__` -> print exactly `Memory Pack promotion cancelled.` and exit.

Do not retry automatically.

---

## Step 6 - Final report

Set values from output:

```text
PROMOTED_ROWS = promoted row count
PROMOTED_NAMESPACE = target namespace or local
```

Print exactly:

```text
## Promotion Completed

- Pack ID: <PACK_ID>
- Pack name: <PACK_NAME>
- Promoted rows: <PROMOTED_ROWS>
- Target namespace: <PROMOTED_NAMESPACE>
- Storage: regular local memories created in memories
- Import staging: retained as import provenance
- Promotion: completed with confirm_promote=true and allow_promote_all=true.
```

End the turn.
