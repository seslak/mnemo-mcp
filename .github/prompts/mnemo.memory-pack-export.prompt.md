---
description: "Scripted Mnemo Memory Pack export wizard. Fixed vscode_askQuestions forms, fixed option labels, signed/unsigned flow, no post-export follow-up, no improvisation."
argument-hint: "[optional topic/group label/group_id]"
agent: 'agent'
tools: ['nexus', 'vscode_askQuestions']
---

# /mnemo.memory-pack-export - Scripted Memory Pack Export Wizard

You are running the Mnemo Memory Pack export wizard.

This prompt is a scripted wizard. Follow the steps exactly. Do not improvise the workflow, wording, option labels, tool sequence, approval sequence, signing sequence, or post-export behavior.

The wizard exports Mnemo memories into a `.mem` Memory Pack. `.mem` is a ZIP container internally.

---

## Terminal rule

DO NOT END THE TURN until one terminal condition is reached:

1. `## Export Completed` has been printed, OR
2. `Memory Pack export cancelled.` has been printed, OR
3. `Memory Pack export stopped.` has been printed, OR
4. a required Mnemo/Nexus action is unavailable, OR
5. a required tool call fails and this prompt explicitly says to stop.

After any tool call, your immediate next action MUST be the next step in this prompt. Do not write transition prose.

When a terminal condition is reached, stop. Do not ask a follow-up question. Do not offer inspection. Do not offer import. Do not ask "Would you like me to...".

If the user invokes `/mnemo.memory-pack-export` again in the same chat after a completed export, treat it as a brand-new export run. Ignore the previous pack unless the user explicitly provides it as input.

---

## Hard rules

- Every user-facing decision MUST go through `vscode_askQuestions`.
- Do not ask chat-only questions.
- Do not present numbered chat-choice lists.
- Do not use different wording between runs.
- Use the exact `header`, `question`, fixed `options.label`, fixed `options.value`, and fixed `options.description` strings specified below.
- Dynamic catalog options from Mnemo are the only dynamic option labels.
- Do not call `nexus.start_interaction`.
- Do not call `nexus.finish_interaction`.
- Do not call `router.*`.
- Do not call `thrift.*`.
- Do not call `mnemo.search`.
- Do not call `mnemo.pack_inspect`.
- Do not call `mnemo.pack_import`.
- Do not read source files.
- Do not read docs.
- Do not inspect implementation files.
- Do not print raw Nexus/Mnemo JSON.
- Do not print capability-check output.
- Do not print transition prose such as "I will...", "Now I will...", "Next I will...", or "Would you like me to...".
- Do not invent group IDs.
- Do not transform labels into group IDs.
- Do not create group IDs such as `group:banking-risk`.
- A group ID used in tool calls MUST be copied exactly from `catalog.options[].value`.
- Use `group_id` and `scope` directly with `pack_preview`, `pack_redaction_preview`, and `pack_export`.
- Do not transfer raw memory ID arrays through chat.
- Do not use memory ID placeholders.
- Do not call `pack_export` before source, scope, preview, redaction preview, export action, pack name, signing details if signed, and final approval are complete.
- Call `pack_export` exactly once, unless it fails and the user chooses `Retry same export`.
- Export artifact extension is `.mem`.
- `content/file_fingerprints.json` contains touched-file paths and hashes, not file contents.
- Local HMAC signing is not public-key identity and not non-repudiation.
- Never print `SIGNING_SECRET`.

---

## Allowed tool actions

Only these actions are allowed:

- `vscode_askQuestions`
- `nexus.list_actions`
- `mnemo.memory_group_discover`
- `mnemo.pack_preview`
- `mnemo.pack_redaction_preview`
- `mnemo.pack_export`

No other action is allowed.

---

## Runtime constants

Use these constants exactly:

- `EXPORT_KINDS = ["context_block", "hippocampus_entry"]`
- `PACK_PREVIEW_LIMIT = 200`
- `CATALOG_LIMIT_INITIAL = 10`
- `CATALOG_LIMIT_MORE = 50`
- `DEFAULT_SCOPE = "core"`
- `DEFAULT_EXPORT_ACTION = "unsigned"`

`DEFAULT_SCOPE` is `core`.

---

## Fixed form syntax

When this prompt says "Call `vscode_askQuestions` with:", make exactly one `vscode_askQuestions` call using the shown `questions` array.

Do not paraphrase `header`, `question`, fixed `label`, fixed `value`, or fixed `description`.

If a form is cancelled, dismissed, returns no answer, or returns an error, print exactly:

`Memory Pack export cancelled.`

Then exit.

---

## Step 0 - Parse invocation

If the slash command was invoked with an argument, store it as `USER_INPUT`.

If no argument was provided, set `USER_INPUT = ""`.

Do not ask the user whether to browse or provide input.

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

- `mnemo.memory_group_discover`
- `mnemo.pack_preview`
- `mnemo.pack_redaction_preview`
- `mnemo.pack_export`

If any required action is missing, print exactly:

`Memory Pack export cannot continue because required Mnemo action <action_name> is unavailable.`

Then exit.

If all actions are available:

- If `USER_INPUT = ""`, continue to Step 2A.
- If `USER_INPUT != ""`, continue to Step 2B.

Do not print a capability summary.

---

## Step 2A - Browse catalog

Call exactly once:

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

Treat the response as internal.

Build:

```text
CATALOG_OPTIONS = response.catalog.options
CATALOG_MAP[value] = option
```

Where `value` is the exact `option.value`.

Every dynamic catalog option MUST use exactly:

```yaml
label: option.label
value: option.value
description: option.description
```

If `CATALOG_OPTIONS` is non-empty, call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo export - source"
    question: "Choose export source"
    options:
      - label: "<dynamic option.label>"
        value: "<dynamic option.value>"
        description: "<dynamic option.description>"
      - label: "Show more sources"
        value: "__show_more__"
        description: "Show up to 50 export sources"
      - label: "Filter sources by phrase"
        value: "__filter__"
        description: "Filter the current catalog by label or group ID"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without exporting"
    allowFreeformInput: false
```

Only dynamic catalog options may vary. The fixed options must be exactly as shown.

If selected value is:

- `__cancel__` -> print exactly `Memory Pack export cancelled.` and exit.
- `__show_more__` -> continue to Step 2A-more.
- `__filter__` -> continue to Step 2C.
- any other value -> set `SELECTED_GROUP_ID = selected value` and continue to Step 3.

If `CATALOG_OPTIONS` is empty, call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo export - source"
    question: "No export sources were found"
    options:
      - label: "Show more sources"
        value: "__show_more__"
        description: "Show up to 50 export sources"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without exporting"
    allowFreeformInput: false
```

If selected value is:

- `__show_more__` -> continue to Step 2A-more.
- anything else -> print exactly `Memory Pack export cancelled.` and exit.

---

## Step 2A-more - Browse expanded catalog

Call exactly once:

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

Rebuild:

```text
CATALOG_OPTIONS = response.catalog.options
CATALOG_MAP[value] = option
```

If `CATALOG_OPTIONS` is empty, print exactly:

`No export sources were found.`

Then exit.

Call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo export - source"
    question: "Choose export source"
    options:
      - label: "<dynamic option.label>"
        value: "<dynamic option.value>"
        description: "<dynamic option.description>"
      - label: "Filter sources by phrase"
        value: "__filter__"
        description: "Filter the current catalog by label or group ID"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without exporting"
    allowFreeformInput: false
```

Only dynamic catalog options may vary. There is no second `Show more sources`.

If selected value is:

- `__cancel__` -> print exactly `Memory Pack export cancelled.` and exit.
- `__filter__` -> continue to Step 2C.
- any other value -> set `SELECTED_GROUP_ID = selected value` and continue to Step 3.

---

## Step 2B - Resolve input

Call exactly once:

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

Build:

```text
CATALOG_OPTIONS = response.catalog.options
CATALOG_MAP[value] = option
```

Resolve `USER_INPUT` using only `CATALOG_OPTIONS`.

Allowed matches, in order:

1. exact `option.value`
2. exact case-insensitive `option.label`
3. case-insensitive `option.label` contains `USER_INPUT`
4. case-insensitive `USER_INPUT` contains `option.label`

If exactly one option matches, set `SELECTED_GROUP_ID = option.value` and continue to Step 3.

If multiple options match, call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo export - source"
    question: "Choose export source"
    options:
      - label: "<dynamic option.label>"
        value: "<dynamic option.value>"
        description: "<dynamic option.description>"
      - label: "Filter sources by phrase"
        value: "__filter__"
        description: "Filter the current catalog by label or group ID"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without exporting"
    allowFreeformInput: false
```

If selected value is:

- `__cancel__` -> print exactly `Memory Pack export cancelled.` and exit.
- `__filter__` -> continue to Step 2C.
- any other value -> set `SELECTED_GROUP_ID = selected value` and continue to Step 3.

If no option matches, continue to Step 2C.

---

## Step 2C - Filter sources by phrase

Call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo export - filter"
    question: "Enter source filter phrase"
    allowFreeformInput: true
```

Store the answer as `FILTER_PHRASE`.

If `FILTER_PHRASE` is empty, print exactly:

`No matching export source was selected.`

Then exit.

If `CATALOG_OPTIONS` is missing or empty, call:

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

Rebuild `CATALOG_OPTIONS` and `CATALOG_MAP`.

Filter `CATALOG_OPTIONS` using only:

1. case-insensitive `option.label` contains `FILTER_PHRASE`
2. case-insensitive `option.value` contains `FILTER_PHRASE`
3. case-insensitive `option.description` contains `FILTER_PHRASE`

If there are no matches, call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo export - filter"
    question: "No matching source was found"
    options:
      - label: "Try another phrase"
        value: "__filter__"
        description: "Enter a different source filter phrase"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without exporting"
    allowFreeformInput: false
```

If selected value is `__filter__`, repeat Step 2C once.

If no match is found after the second phrase, print exactly:

`No matching export source was found.`

Then exit.

If matches exist, call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo export - source"
    question: "Choose export source"
    options:
      - label: "<dynamic option.label>"
        value: "<dynamic option.value>"
        description: "<dynamic option.description>"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without exporting"
    allowFreeformInput: false
```

If selected value is:

- `__cancel__` -> print exactly `Memory Pack export cancelled.` and exit.
- any other value -> set `SELECTED_GROUP_ID = selected value` and continue to Step 3.

---

## Step 3 - Choose scope

Before asking scope, verify:

```text
SELECTED_GROUP_ID in CATALOG_MAP
```

If false, print exactly:

`Selected source is not a valid Mnemo group_id from the current catalog.`

Then exit.

Call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo export - scope"
    question: "Choose export scope"
    options:
      - label: "Core only"
        value: "core"
        description: "Only memories directly in this group"
        recommended: true
      - label: "Core + related"
        value: "core_plus_related"
        description: "Core memories plus bounded related memories"
      - label: "Full tree"
        value: "full_tree"
        description: "Largest related-memory scope"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without exporting"
    allowFreeformInput: false
```

If selected value is:

- `__cancel__` -> print exactly `Memory Pack export cancelled.` and exit.
- otherwise set `SELECTED_SCOPE = selected value`.

Immediately continue to Step 4.

---

## Step 4 - Pack preview

Call exactly once:

```json
{
  "action": "mnemo.pack_preview",
  "params": {
    "group_id": "<SELECTED_GROUP_ID>",
    "scope": "<SELECTED_SCOPE>",
    "kinds": ["context_block", "hippocampus_entry"],
    "include_samples": true,
    "sample_per_kind": 3,
    "limit": 200
  }
}
```

If preview has zero exportable rows, print exactly:

`No exportable memories were found for this selection.`

Then exit.

Set values from output:

```text
SOURCE_LABEL = CATALOG_MAP[SELECTED_GROUP_ID].label
EXPORTABLE_COUNT = preview exportable count
NON_EXPORTABLE_COUNT = preview non-exportable count
SAMPLE_TITLES = up to 3 sample titles
```

Print exactly this heading block:

```text
## Preview

- Source: <SOURCE_LABEL>
- Scope: <SELECTED_SCOPE>
- Exportable memories: <EXPORTABLE_COUNT>
- Non-exportable memories: <NON_EXPORTABLE_COUNT>
- Sample titles: <SAMPLE_TITLES>
```

Immediately continue to Step 5.

---

## Step 5 - Redaction preview

Call exactly once with the same selector:

```json
{
  "action": "mnemo.pack_redaction_preview",
  "params": {
    "group_id": "<SELECTED_GROUP_ID>",
    "scope": "<SELECTED_SCOPE>",
    "kinds": ["context_block", "hippocampus_entry"],
    "include_samples": true,
    "sample_per_kind": 3,
    "limit": 200
  }
}
```

Set values from output:

```text
REDACTION_RULESET = ruleset name
REDACTION_MATCHES = total match count
REDACTION_CATEGORIES = categories
```

Print exactly this heading block:

```text
## Redaction Preview

- Redaction ruleset: <REDACTION_RULESET>
- Redaction matches: <REDACTION_MATCHES>
- Redaction categories: <REDACTION_CATEGORIES>
- File fingerprint note: file_fingerprints records touched-file paths and hashes, not file contents.
```

Immediately continue to Step 6.

---

## Step 6 - Export action

Call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo export - action"
    question: "Choose export action"
    options:
      - label: "Export unsigned"
        value: "unsigned"
        description: "Create an unsigned .mem pack using allow_unsigned=true"
        recommended: true
      - label: "Export signed"
        value: "signed"
        description: "Create a signed .mem pack using local HMAC"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without exporting"
    allowFreeformInput: false
```

If selected value is:

- `__cancel__` -> print exactly `Memory Pack export cancelled.` and exit.
- `unsigned` -> set `EXPORT_MODE = unsigned` and continue to Step 7.
- `signed` -> set `EXPORT_MODE = signed` and continue to Step 7.

---

## Step 7 - Pack name

Create:

```text
SUGGESTED_PACK_NAME = lowercase slug of SOURCE_LABEL
```

Call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo export - pack name"
    question: "Choose pack name"
    allowFreeformInput: true
```

Pre-fill/default to `SUGGESTED_PACK_NAME` if the runtime supports defaults.

If answer is empty, call the same form once more.

If the second answer is empty, print exactly:

`Memory Pack export stopped.`

Then exit.

Set:

```text
PACK_NAME = answer
```

If `EXPORT_MODE = unsigned`, continue to Step 9.

If `EXPORT_MODE = signed`, continue to Step 8S.

---

## Step 8S - Signed export details

Call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo export - signer"
    question: "Enter signer ID"
    allowFreeformInput: true
```

If answer is empty, print exactly:

`Memory Pack export stopped.`

Then exit.

Set `SIGNER_ID` to the answer.

Call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo export - signing secret"
    question: "Enter signing secret"
    allowFreeformInput: true
```

If answer is empty, print exactly:

`Memory Pack export stopped.`

Then exit.

Set `SIGNING_SECRET` to the answer.

Never print `SIGNING_SECRET`.

Continue to Step 9.

---

## Step 9 - Final approval

Print exactly this heading block:

```text
## Export Approval

- Source: <SOURCE_LABEL>
- Scope: <SELECTED_SCOPE>
- Pack name: <PACK_NAME>
- Export mode: <EXPORT_MODE>
- Exportable memories: <EXPORTABLE_COUNT>
- Redaction matches: <REDACTION_MATCHES>
- Output: .mem
- File fingerprint note: file_fingerprints records touched-file paths and hashes, not file contents.
```

Call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo export - approval"
    question: "Approve export"
    options:
      - label: "Approve export"
        value: "approve"
        description: "Create the .mem Memory Pack now"
        recommended: true
      - label: "Change pack name"
        value: "change_pack_name"
        description: "Return to pack name"
      - label: "Change export action"
        value: "change_export_action"
        description: "Return to export action"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without exporting"
    allowFreeformInput: false
```

If selected value is:

- `approve` -> continue to Step 10.
- `change_pack_name` -> go to Step 7.
- `change_export_action` -> go to Step 6.
- `__cancel__` -> print exactly `Memory Pack export cancelled.` and exit.

---

## Step 10 - Export

If `EXPORT_MODE = unsigned`, call exactly once:

```json
{
  "action": "mnemo.pack_export",
  "params": {
    "pack_name": "<PACK_NAME>",
    "group_id": "<SELECTED_GROUP_ID>",
    "scope": "<SELECTED_SCOPE>",
    "kinds": ["context_block", "hippocampus_entry"],
    "allow_unsigned": true
  }
}
```

If `EXPORT_MODE = signed`, call exactly once:

```json
{
  "action": "mnemo.pack_export",
  "params": {
    "pack_name": "<PACK_NAME>",
    "group_id": "<SELECTED_GROUP_ID>",
    "scope": "<SELECTED_SCOPE>",
    "kinds": ["context_block", "hippocampus_entry"],
    "sign_pack": true,
    "signer_id": "<SIGNER_ID>",
    "signing_secret": "<SIGNING_SECRET>"
  }
}
```

If export succeeds, continue to Step 11.

If export fails with `secret_too_short`, print exactly:

```text
## Export Failed

- Error: signing secret is too short
```

Then call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo export - retry"
    question: "Choose retry action"
    options:
      - label: "Enter new signing secret"
        value: "new_signing_secret"
        description: "Provide a signing secret with the required length"
      - label: "Export unsigned"
        value: "unsigned"
        description: "Switch to unsigned .mem export"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without exporting"
    allowFreeformInput: false
```

If selected value is:

- `new_signing_secret` -> go to Step 8S, preserving `PACK_NAME` and `SIGNER_ID` if possible.
- `unsigned` -> set `EXPORT_MODE = unsigned` and go to Step 9.
- `__cancel__` -> print exactly `Memory Pack export cancelled.` and exit.

If export fails for any other reason, print:

```text
## Export Failed

- Error: <error>
```

Then call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo export - retry"
    question: "Choose retry action"
    options:
      - label: "Retry same export"
        value: "retry_same_export"
        description: "Run the same export once more"
      - label: "Change pack name"
        value: "change_pack_name"
        description: "Return to pack name"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without exporting"
    allowFreeformInput: false
```

If selected value is:

- `retry_same_export` -> retry Step 10 exactly once.
- `change_pack_name` -> go to Step 7.
- `__cancel__` -> print exactly `Memory Pack export cancelled.` and exit.

Do not retry automatically.

---

## Step 11 - Final report

Print exactly:

```text
## Export Completed

- Pack ID: <pack_id>
- Pack name: <pack_name>
- Output path: <output_path>
- Exported memories: <exported_rows>
- Signed: <true_or_false>
- Redaction summary: <redaction_summary>
- File fingerprint note: file_fingerprints records touched-file paths and hashes, not file contents.
- Next step: Use /mnemo.memory-pack-import on another machine, or inspect locally with mnemo.pack_inspect.
```

`Output path` must end in `.mem`.

Do not ask a follow-up question.

Do not offer to run `mnemo.pack_inspect`.

Do not offer to run import.

End the turn.
