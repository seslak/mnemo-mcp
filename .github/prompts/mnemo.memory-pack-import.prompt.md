---
description: "Scripted Mnemo Memory Pack import wizard. .mem landing-folder selection, inspect-first, quarantine/trusted import, grouped review, optional continuation to promotion with explicit allow_promote_all gate."
argument-hint: "[optional pack filename/path] [optional promote=true]"
agent: 'agent'
tools: ['nexus', 'vscode_askQuestions']
---

# /mnemo.memory-pack-import - Scripted Memory Pack Import Wizard

You are running the Mnemo Memory Pack import wizard.

This prompt is a scripted wizard. Follow the steps exactly. Do not improvise the workflow, wording, option labels, tool sequence, import mode sequence, review sequence, promotion gate, or post-import behavior.

The wizard imports a `.mem` Memory Pack from the Mnemo landing folder or from a user-provided `.mem` pack filename/path.

Normal workflow:

```text
landing list or input path -> inspect -> import mode -> approval -> import -> grouped review -> finish or continue to promotion
```

Promotion is not automatic. Promotion is allowed only when:
- the user explicitly requests promotion in the invocation or message, OR
- the user chooses `Continue to promotion` after import review.

Imported rows are staged in `imported_pack_rows`. They are not regular local memories until promotion materializes them into `memories`.

---

## Terminal rule

DO NOT END THE TURN until one terminal condition is reached:

1. `## Import Completed` has been printed, OR
2. `## Promotion Completed` has been printed, OR
3. `Memory Pack import cancelled.` has been printed, OR
4. `Memory Pack import stopped.` has been printed, OR
5. a required Mnemo/Nexus action is unavailable, OR
6. a required tool call fails and this prompt explicitly says to stop.

After any tool call, your immediate next action MUST be the next step in this prompt. Do not write transition prose.

When a terminal condition is reached, stop. Do not ask a follow-up question. Do not offer another import. Do not ask "Would you like me to...".

If the user invokes `/mnemo.memory-pack-import` again in the same chat after a completed import, treat it as a brand-new import run. Ignore the previous pack unless the user explicitly provides it as input.

---

## Hard rules

- Every user-facing decision MUST go through `vscode_askQuestions`.
- Do not ask chat-only questions.
- Do not present numbered chat-choice lists.
- Do not use different wording between runs.
- Use the exact `header`, `question`, fixed `options.label`, fixed `options.value`, and fixed `options.description` strings specified below.
- Dynamic landing-folder options from Mnemo are the only dynamic pack option labels.
- Import only `.mem` Memory Packs in this workflow.
- Do not offer `.zip` import.
- Do not include `.zip` packs in landing-folder lists.
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
- Do not print transition prose such as "I will...", "Now I will...", "Next I will...", "Would you like me to...", or "Choosing import mode...".
- Do not invent pack paths.
- A pack path used in tool calls MUST be copied exactly from `pack_landing_list` output or from an explicit user-provided `.mem` path.
- Always call `mnemo.pack_inspect` before `mnemo.pack_import`.
- Always call `mnemo.pack_review_import` after successful `mnemo.pack_import`.
- Use `include_grouped_summary=true` in `mnemo.pack_review_import`.
- Do not call `mnemo.pack_import` before pack selection/path resolution, inspect, import mode, and final approval are complete.
- Call `mnemo.pack_import` exactly once, unless it fails and the user chooses a retry path.
- Quarantine import is the recommended default.
- Trusted import is NOT local adoption.
- Trusted import is NOT automatic promotion.
- Trusted import is NOT default retrieval.
- Never auto-promote.
- Only call `mnemo.pack_promote_preview` and `mnemo.pack_promote` after an explicit promotion choice or explicit promotion invocation.
- Promotion requires `confirm_promote=true`.
- If promoting the full previewed pack without row filters, `mnemo.pack_promote` also requires `allow_promote_all=true`.
- `allow_promote_all=true` may be used only after the explicit `Approve promotion` form has been accepted.
- `content/file_fingerprints.json` contains touched-file paths and hashes, not file contents.
- Never print `VERIFICATION_SECRET`.

---

## Allowed tool actions

Only these actions are allowed:

- `vscode_askQuestions`
- `nexus.list_actions`
- `mnemo.pack_landing_list`
- `mnemo.pack_inspect`
- `mnemo.pack_import`
- `mnemo.pack_review_import`
- `mnemo.pack_promote_preview`
- `mnemo.pack_promote`

No other action is allowed.

Promotion actions are allowed only after an explicit promotion choice or explicit promotion invocation.

---

## Runtime constants

Use these constants exactly:

- `LANDING_LIMIT_INITIAL = 20`
- `LANDING_LIMIT_MORE = 50`
- `SAMPLE_LIMIT = 10`
- `DEFAULT_IMPORT_MODE = "quarantine"`

---

## Fixed form syntax

When this prompt says "Call `vscode_askQuestions` with:", make exactly one `vscode_askQuestions` call using the shown `questions` array.

Do not paraphrase `header`, `question`, fixed `label`, fixed `value`, or fixed `description`.

If a form is cancelled, dismissed, returns no answer, or returns an error before import has run, print exactly:

`Memory Pack import cancelled.`

Then exit.

If the post-import next-step form is cancelled or dismissed after import has already run, print the `## Import Completed` block with `Next step choice: finish import only`.

---

## Step 0 - Parse invocation

If the slash command was invoked with an argument, store it as `USER_INPUT`.

If no argument was provided, set `USER_INPUT = ""`.

Set:

```text
PROMOTION_REQUESTED = true
```

only if `USER_INPUT` contains one of these exact tokens:

- `promote=true`
- `--promote`
- `promote`

Otherwise set:

```text
PROMOTION_REQUESTED = false
```

Remove the promotion token from `USER_INPUT` before pack path/name resolution.

Do not ask the user whether to browse or provide a path.

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

- `mnemo.pack_landing_list`
- `mnemo.pack_inspect`
- `mnemo.pack_import`
- `mnemo.pack_review_import`
- `mnemo.pack_promote_preview`
- `mnemo.pack_promote`

If any required action is missing, print exactly:

`Memory Pack import cannot continue because required Mnemo action <action_name> is unavailable.`

Then exit.

If all required actions are available:

- If `USER_INPUT = ""`, continue to Step 2A.
- If `USER_INPUT != ""`, continue to Step 2B.

Do not print a capability summary.

---

## Step 2A - Browse landing folder

Call exactly once:

```json
{
  "action": "mnemo.pack_landing_list",
  "params": {
    "limit": 20
  }
}
```

Treat the response as internal.

Build:

```text
PACK_OPTIONS = .mem packs returned by pack_landing_list
PACK_MAP[path] = pack object
```

Every dynamic pack option MUST use exactly:

```yaml
label: filename
value: path
description: "<size_bytes> bytes - modified <modified_time>"
```

If `PACK_OPTIONS` is non-empty, call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo import - pack"
    question: "Choose Memory Pack"
    options:
      - label: "<dynamic filename>"
        value: "<dynamic exact pack path>"
        description: "<dynamic size bytes> bytes - modified <dynamic modified time>"
      - label: "Show more packs"
        value: "__show_more__"
        description: "Show up to 50 Memory Packs"
      - label: "Filter packs by phrase"
        value: "__filter__"
        description: "Filter the current landing-folder list by filename or path"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without importing"
    allowFreeformInput: false
```

Only dynamic pack options may vary. The fixed options must be exactly as shown.

If selected value is:

- `__cancel__` -> print exactly `Memory Pack import cancelled.` and exit.
- `__show_more__` -> continue to Step 2A-more.
- `__filter__` -> continue to Step 2C.
- any other value -> set `PACK_PATH = selected value` and continue to Step 3.

If `PACK_OPTIONS` is empty, print exactly:

`No Memory Packs were found.`

Then exit.

---

## Step 2A-more - Browse expanded landing folder

Call exactly once:

```json
{
  "action": "mnemo.pack_landing_list",
  "params": {
    "limit": 50
  }
}
```

Rebuild:

```text
PACK_OPTIONS = .mem packs returned by pack_landing_list
PACK_MAP[path] = pack object
```

If `PACK_OPTIONS` is empty, print exactly:

`No Memory Packs were found.`

Then exit.

Call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo import - pack"
    question: "Choose Memory Pack"
    options:
      - label: "<dynamic filename>"
        value: "<dynamic exact pack path>"
        description: "<dynamic size bytes> bytes - modified <dynamic modified time>"
      - label: "Filter packs by phrase"
        value: "__filter__"
        description: "Filter the current landing-folder list by filename or path"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without importing"
    allowFreeformInput: false
```

If selected value is:

- `__cancel__` -> print exactly `Memory Pack import cancelled.` and exit.
- `__filter__` -> continue to Step 2C.
- any other value -> set `PACK_PATH = selected value` and continue to Step 3.

---

## Step 2B - Resolve input

Call exactly once:

```json
{
  "action": "mnemo.pack_landing_list",
  "params": {
    "limit": 50
  }
}
```

Build:

```text
PACK_OPTIONS = .mem packs returned by pack_landing_list
PACK_MAP[path] = pack object
```

Resolve `USER_INPUT` using:

1. exact path match
2. exact filename match
3. case-insensitive filename contains `USER_INPUT`
4. case-insensitive path contains `USER_INPUT`

If exactly one option matches, set `PACK_PATH = option.path` and continue to Step 3.

If multiple options match, call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo import - pack"
    question: "Choose Memory Pack"
    options:
      - label: "<dynamic filename>"
        value: "<dynamic exact pack path>"
        description: "<dynamic size bytes> bytes - modified <dynamic modified time>"
      - label: "Filter packs by phrase"
        value: "__filter__"
        description: "Filter the current landing-folder list by filename or path"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without importing"
    allowFreeformInput: false
```

If selected value is:

- `__cancel__` -> print exactly `Memory Pack import cancelled.` and exit.
- `__filter__` -> continue to Step 2C.
- any other value -> set `PACK_PATH = selected value` and continue to Step 3.

If no option matches and `USER_INPUT` ends with `.mem`, set `PACK_PATH = USER_INPUT` and continue to Step 3.

If no option matches and input does not look like a `.mem` path, continue to Step 2C.

---

## Step 2C - Filter packs by phrase

Call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo import - filter"
    question: "Enter pack filter phrase"
    allowFreeformInput: true
```

Store the answer as `FILTER_PHRASE`.

If `FILTER_PHRASE` is empty, print exactly:

`No matching Memory Pack was selected.`

Then exit.

If `PACK_OPTIONS` is missing or empty, call:

```json
{
  "action": "mnemo.pack_landing_list",
  "params": {
    "limit": 50
  }
}
```

Rebuild `PACK_OPTIONS` and `PACK_MAP`.

Filter `PACK_OPTIONS` using only:

1. case-insensitive filename contains `FILTER_PHRASE`
2. case-insensitive path contains `FILTER_PHRASE`

If there are no matches, call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo import - filter"
    question: "No matching Memory Pack was found"
    options:
      - label: "Try another phrase"
        value: "__filter__"
        description: "Enter a different pack filter phrase"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without importing"
    allowFreeformInput: false
```

If selected value is `__filter__`, repeat Step 2C once.

If no match is found after the second phrase, print exactly:

`No matching Memory Pack was found.`

Then exit.

If matches exist, call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo import - pack"
    question: "Choose Memory Pack"
    options:
      - label: "<dynamic filename>"
        value: "<dynamic exact pack path>"
        description: "<dynamic size bytes> bytes - modified <dynamic modified time>"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without importing"
    allowFreeformInput: false
```

If selected value is:

- `__cancel__` -> print exactly `Memory Pack import cancelled.` and exit.
- any other value -> set `PACK_PATH = selected value` and continue to Step 3.

---

## Step 3 - Inspect pack

Call exactly once:

```json
{
  "action": "mnemo.pack_inspect",
  "params": {
    "pack_path": "<PACK_PATH>",
    "include_samples": false,
    "sample_limit": 5
  }
}
```

If inspect status is not valid and not ok, print exactly:

`Selected Memory Pack could not be inspected.`

Then exit.

Set values from output:

```text
PACK_ID = inspected pack_id
PACK_NAME = inspected pack_name
PACK_STATUS = inspect status
SCHEMA_VERSION = schema version
TRUST_CLASSIFICATION = trust classification
TRUSTED_IMPORT_AVAILABLE = trusted_import_available
ROW_COUNT = row count
TOPICS_SUMMARY = topics/groups summary
FILE_FINGERPRINT_COUNT = file_fingerprint path count
SIGNED_STATUS = signed or unsigned
```

Print exactly this heading block:

```text
## Pack Inspect

- Pack ID: <PACK_ID>
- Pack name: <PACK_NAME>
- Status: <PACK_STATUS>
- Schema version: <SCHEMA_VERSION>
- Signed: <SIGNED_STATUS>
- Trust classification: <TRUST_CLASSIFICATION>
- Trusted import available: <TRUSTED_IMPORT_AVAILABLE>
- Rows: <ROW_COUNT>
- Topics/groups: <TOPICS_SUMMARY>
- File fingerprint paths: <FILE_FINGERPRINT_COUNT>
```

Immediately continue to Step 4.

---

## Step 4 - Import mode

If `TRUSTED_IMPORT_AVAILABLE = true`, call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo import - mode"
    question: "Choose import mode"
    options:
      - label: "Import to quarantine"
        value: "quarantine"
        description: "Recommended default; imported rows stay staged for review"
        recommended: true
      - label: "Trusted import"
        value: "trusted"
        description: "Import to trusted pack namespace; not local adoption"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without importing"
    allowFreeformInput: false
```

If `TRUSTED_IMPORT_AVAILABLE` is not true, call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo import - mode"
    question: "Choose import mode"
    options:
      - label: "Import to quarantine"
        value: "quarantine"
        description: "Recommended default; imported rows stay staged for review"
        recommended: true
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without importing"
    allowFreeformInput: false
```

If selected value is:

- `__cancel__` -> print exactly `Memory Pack import cancelled.` and exit.
- `quarantine` -> set `IMPORT_MODE = quarantine` and continue to Step 6.
- `trusted` -> set `IMPORT_MODE = trusted` and continue to Step 5T.

---

## Step 5T - Trusted import secret

Call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo import - verification secret"
    question: "Enter verification secret"
    allowFreeformInput: true
```

If answer is empty, print exactly:

`Memory Pack import stopped.`

Then exit.

Set `VERIFICATION_SECRET` to the answer.

Never print `VERIFICATION_SECRET`.

Continue to Step 6.

---

## Step 6 - Final import approval

Print exactly this heading block:

```text
## Import Approval

- Pack ID: <PACK_ID>
- Pack name: <PACK_NAME>
- Import mode: <IMPORT_MODE>
- Rows: <ROW_COUNT>
- Trust classification: <TRUST_CLASSIFICATION>
- File fingerprint paths: <FILE_FINGERPRINT_COUNT>
- Note: import stages rows in imported_pack_rows; regular memories are created only after promotion.
```

Call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo import - approval"
    question: "Approve import"
    options:
      - label: "Approve import"
        value: "approve"
        description: "Stage the selected Memory Pack for review"
        recommended: true
      - label: "Change import mode"
        value: "change_import_mode"
        description: "Return to import mode"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without importing"
    allowFreeformInput: false
```

If selected value is:

- `approve` -> continue to Step 7.
- `change_import_mode` -> go to Step 4.
- `__cancel__` -> print exactly `Memory Pack import cancelled.` and exit.

---

## Step 7 - Import

If `IMPORT_MODE = quarantine`, call exactly once:

```json
{
  "action": "mnemo.pack_import",
  "params": {
    "pack_path": "<PACK_PATH>",
    "allow_unsigned_quarantine": true
  }
}
```

If `IMPORT_MODE = trusted`, call exactly once:

```json
{
  "action": "mnemo.pack_import",
  "params": {
    "pack_path": "<PACK_PATH>",
    "allow_trusted_import": true,
    "verification_secret": "<VERIFICATION_SECRET>"
  }
}
```

If import succeeds, continue below.

If import fails with `secret_too_short`, print exactly:

```text
## Import Failed

- Error: verification secret is too short
```

Then call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo import - retry"
    question: "Choose retry action"
    options:
      - label: "Enter new verification secret"
        value: "new_verification_secret"
        description: "Provide a verification secret with the required length"
      - label: "Import to quarantine"
        value: "quarantine"
        description: "Switch to quarantine import"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without importing"
    allowFreeformInput: false
```

If selected value is:

- `new_verification_secret` -> go to Step 5T.
- `quarantine` -> set `IMPORT_MODE = quarantine` and go to Step 6.
- `__cancel__` -> print exactly `Memory Pack import cancelled.` and exit.

If import fails for any other reason, print:

```text
## Import Failed

- Error: <error>
```

Then call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo import - retry"
    question: "Choose retry action"
    options:
      - label: "Retry same import"
        value: "retry_same_import"
        description: "Run the same import once more"
      - label: "Change import mode"
        value: "change_import_mode"
        description: "Return to import mode"
      - label: "Cancel"
        value: "__cancel__"
        description: "Stop without importing"
    allowFreeformInput: false
```

If selected value is:

- `retry_same_import` -> retry Step 7 exactly once.
- `change_import_mode` -> go to Step 4.
- `__cancel__` -> print exactly `Memory Pack import cancelled.` and exit.

Do not retry automatically.

Set values from import output:

```text
IMPORTED_PACK_ID = imported pack_id, or PACK_ID if unchanged
IMPORT_NAMESPACE = import namespace
TRUST_LEVEL = trust level
IMPORTED_ROWS = imported row count
```

Immediately continue to Step 8.

---

## Step 8 - Review import

Call exactly once:

```json
{
  "action": "mnemo.pack_review_import",
  "params": {
    "pack_id": "<IMPORTED_PACK_ID>",
    "include_samples": true,
    "sample_limit": 10,
    "include_grouped_summary": true
  }
}
```

Set values from review output:

```text
GROUPED_SUMMARY = grouped summary
SAMPLE_SUMMARY = sample summary
```

If `PROMOTION_REQUESTED = true`, continue to Step 10P.

If `PROMOTION_REQUESTED = false`, continue to Step 9.

---

## Step 9 - Import next step

Call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo import - next step"
    question: "Choose next step"
    options:
      - label: "Finish import only"
        value: "finish_import_only"
        description: "Keep rows staged in imported_pack_rows for later promotion"
        recommended: true
      - label: "Continue to promotion"
        value: "continue_to_promotion"
        description: "Preview and approve promotion into regular local memories"
    allowFreeformInput: false
```

If selected value is:

- `finish_import_only` -> continue to Step 9F.
- `continue_to_promotion` -> continue to Step 10P.

If the form is cancelled or dismissed, continue to Step 9F.

---

## Step 9F - Finish import only

Print exactly:

```text
## Import Completed

- Pack ID: <IMPORTED_PACK_ID>
- Pack name: <PACK_NAME>
- Import mode: <IMPORT_MODE>
- Import namespace: <IMPORT_NAMESPACE>
- Trust level: <TRUST_LEVEL>
- Imported rows: <IMPORTED_ROWS>
- Storage: staged in imported_pack_rows
- Regular memories: not created until promotion
- Grouped review summary: <GROUPED_SUMMARY>
- Sample summary: <SAMPLE_SUMMARY>
- Promotion: not run. Use /mnemo.memory-pack-promote to promote later.
```

End the turn.

---

## Step 10P - Promotion preview

Call exactly once:

```json
{
  "action": "mnemo.pack_promote_preview",
  "params": {
    "pack_id": "<IMPORTED_PACK_ID>",
    "include_samples": true,
    "sample_limit": 10
  }
}
```

Set values from output:

```text
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

- Pack ID: <IMPORTED_PACK_ID>
- Pack name: <PACK_NAME>
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

`Memory Pack import stopped.`

Then exit.

This approval authorizes promotion of all rows shown in the promotion preview. Because no row filters are supplied, the later `mnemo.pack_promote` call MUST include `allow_promote_all=true`.

Call `vscode_askQuestions` with:

```yaml
questions:
  - header: "Mnemo import - promotion"
    question: "Approve promotion"
    options:
      - label: "Approve promotion"
        value: "approve_promotion"
        description: "Create regular local memories from all previewed staged rows"
      - label: "Finish without promotion"
        value: "finish_without_promotion"
        description: "Keep rows staged in imported_pack_rows"
        recommended: true
    allowFreeformInput: false
```

If selected value is:

- `approve_promotion` -> continue to Step 11P.
- `finish_without_promotion` -> continue to Step 9F.

If the form is cancelled or dismissed, continue to Step 9F.

---

## Step 11P - Promotion

Call exactly once:

```json
{
  "action": "mnemo.pack_promote",
  "params": {
    "pack_id": "<IMPORTED_PACK_ID>",
    "confirm_promote": true,
    "allow_promote_all": true
  }
}
```

If promotion fails with `promote_all_requires_explicit_allow`, this prompt is stale or the required safety flag was omitted. Print exactly:

```text
## Promotion Failed

- Error: promote all requires allow_promote_all=true
```

Then exit.

Set values from output:

```text
PROMOTED_ROWS = promoted row count
PROMOTED_NAMESPACE = target namespace or local
```

Print exactly:

```text
## Promotion Completed

- Pack ID: <IMPORTED_PACK_ID>
- Pack name: <PACK_NAME>
- Promoted rows: <PROMOTED_ROWS>
- Target namespace: <PROMOTED_NAMESPACE>
- Storage: regular local memories created in memories
- Import staging: retained as import provenance
- Promotion: completed with confirm_promote=true and allow_promote_all=true.
```

End the turn.
