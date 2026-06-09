# Mnemo Tool Reference

Mnemo exposes one public MCP gateway tool:

```text
mnemo
```

Every call uses this shape:

```json
{"action":"search","params":{"query":"release checklist"}}
```

`action` is required. `params` is optional and defaults to an empty object.

## Actions

### `doctor`

Returns server, storage, schema, FTS, signature, export, salience, event-history, and IDF diagnostics.

```json
{"action":"doctor"}
```

### `record`

Records a project memory.

Common params:

- `kind`: `decision`, `invariant`, `failed_approach`, `test_result`, `command`, `path`, `note`, `interaction_log`, `context_block`, `hippocampus_entry`, or `agent_feedback`
- `text`
- `summary` for `interaction_log`
- `body` for `context_block`
- `title`
- `source`
- `tags`
- `linked_ids`
- `agent_id`
- `role`
- `domain`
- `scope`
- `authority`
- `retention`
- `confidence`
- `source_run_id`
- `metadata`
- `touched_files`: optional array of workspace paths touched during the turn (used for git-aware freshness tracking)
- `namespace`: optional, defaults to `local`
- `origin`: optional, defaults to `local`

Example:

```json
{"action":"record","params":{"kind":"decision","text":"Use SQLite as the default Mnemo store."}}
```

Record-time duplicate behavior:

1. exact `content_hash` duplicate short-circuits
2. exact `normalized_hash` duplicate short-circuits
3. shingle-overlap survivors get full salience/fallback scoring
4. no automatic delete/merge occurs

Git-aware write metadata:

- each new row attempts to stamp `git_sha`, `git_branch`, and `git_dirty`
- when `touched_files` is present, Mnemo stores per-path digests in `memory_files`
- non-git roots and git failures are non-fatal; writes still succeed

### `alias_hint`

Records explicit alias evidence from failed wording to successful canonical wording.

```json
{"action":"alias_hint","params":{"domain":"agentic","canonical":"memory recall pipeline","candidate_alias":"hippocampus bridge","original_query":"hippocampus bridge","successful_query":"memory recall pipeline","confidence":"high","include_in_salience":true}}
```

Notes:

- `confidence` must be `low`, `medium`, or `high`
- this records evidence only; no alias activation occurs

### `topic_add`

Adds one topic row to one memory.

```json
{"action":"topic_add","params":{"memory_id":"mem_123","topic":"auth","source":"operator"}}
```

Notes:

- `topic` must be non-empty
- duplicate `(memory_id, topic)` rows are ignored
- `source` defaults to `agent`

### `topic_remove`

Removes one topic row from one memory.

```json
{"action":"topic_remove","params":{"memory_id":"mem_123","topic":"auth"}}
```

### `topic_list`

Lists topic metadata.

All topics with counts:

```json
{"action":"topic_list","params":{"scope":"all"}}
```

Topics for one memory:

```json
{"action":"topic_list","params":{"scope":"memory","memory_id":"mem_123"}}
```

### `pack_preview`

Read-only preview for future memory-pack selection.

```json
{"action":"pack_preview","params":{"topics":["auth"],"include_imported":true,"limit":100}}
```

Key params:

- `topics`: topic filter via `memory_topics` joins
- `group_id`: computed group selector resolved by the same runtime logic as `memory_group_preview`
- `scope`: `core`, `core_plus_related`, or `full_tree` when `group_id` is used
- `kinds`: defaults to `["context_block","hippocampus_entry"]`
- `memory_ids`: exact selector; unknown IDs are ignored with warning and do not broaden selection
- `namespace` or `namespaces` (mutually exclusive)
- `include_imported`: adds trusted imported namespaces
- `include_quarantine`: adds quarantine namespaces
- `origin` or `origins` (mutually exclusive; applied only when explicit)
- `created_after` / `created_before`: ISO-8601 UTC
- `touched_paths`: exact/normalized repo-relative path filter via `memory_files`
- `limit`: defaults to `100`
- `sample_per_kind`: defaults to `3`
- `include_samples`: defaults to `true`

Selector rules:

- `group_id` cannot be combined with `topics`
- `group_id` cannot be combined with `memory_ids`
- mixed selector requests fail with `ambiguous_selector`

Read-only behavior:

- does not create `exported_packs` rows
- does not write pack files
- does not run export/import/redaction/signing/trust/promotion flows

### `pack_redaction_preview`

Read-only baseline redaction dry-run over the same selection engine used by `pack_preview`.

```json
{"action":"pack_redaction_preview","params":{"topics":["auth"],"include_imported":true,"limit":100}}
```

Selection params match `pack_preview`, including:

- `topics`
- `group_id`
- `scope`
- `kinds`
- `memory_ids`
- `namespace` / `namespaces`
- `include_imported` / `include_quarantine`
- `origin` / `origins`
- `created_after` / `created_before`
- `touched_paths`
- `limit`

Redaction-specific params:

- `include_redacted_samples` (default `true`)
- `max_redacted_samples` (default `10`, capped to `50`)

Baseline redaction categories:

- `private_key_header`
- `jwt`
- `aws_access_key`
- `email`
- `user_path`
- `ipv4`

Read-only behavior:

- no `exported_packs` mutations
- no pack file writes
- no ZIP export/import, signing, trust, or promotion flows
- dry-run only; output is diagnostic counts plus bounded redacted previews

### `pack_export`

Writes a local pack artifact using the same selection semantics as `pack_preview`.

```json
{"action":"pack_export","params":{"pack_name":"auth_memory_pack","allow_unsigned":true}}
```

Key params:

- `pack_name` (required, sanitized for filesystem safety)
- unsigned mode:
  - `allow_unsigned` required when `sign_pack` is not enabled
- signed mode:
  - `sign_pack=true`
  - `signer_id` required
  - `signing_secret` required
  - `signature_algorithm` defaults to `hmac-sha256-local-v1`
- `output_dir` (optional; defaults to `state/mnemo/packs/exports/`)
- selection filters shared with `pack_preview`:
  - `topics`
  - `group_id`
  - `scope`
  - `kinds`
  - `memory_ids`
  - `namespace` / `namespaces`
  - `include_imported` / `include_quarantine`
  - `origin` / `origins`
  - `created_after` / `created_before`
  - `touched_paths`
  - `limit`
- `allow_limited_export` (default `false`)

Policy constraints:

- exportable kinds are strict:
  - `context_block`
  - `hippocampus_entry`
- preview-only kinds (`interaction_log`, `agent_feedback`) are rejected for export
- limited group/topic/memory-id selections require explicit confirmation:
  - `allow_limited_export=true`
  - otherwise export fails with `limited_export_requires_confirmation`
- `hmac-sha256-local-v1` is local/dev signing only; it is not public-key identity and not non-repudiation

Artifact/suffix behavior:

- exported files now end with `.mem`
- `.mem` remains a ZIP container internally
- legacy `.zip` packs remain accepted by `pack_inspect` and `pack_import` for compatibility
- `content/file_fingerprints.json` stores touched-file paths and hashes only, not file contents

Redaction behavior:

- redaction is mandatory during export
- baseline-v1 categories:
  - `private_key_header`
  - `jwt`
  - `aws_access_key`
  - `email`
  - `user_path`
  - `ipv4`
- baseline-v1 is intentionally incomplete and not a full DLP system

Prompt UX notes:

- `/mnemo.memory-pack-export` is the normal user-facing export command
- no-input export should browse and export in one flow
- final selectors must still be exact topic, exact `group_id`, or explicit advanced `memory_ids`
- normal export UX should prefer passing `group_id` + `scope` directly into `pack_preview`, `pack_redaction_preview`, and `pack_export`

### `pack_landing_list`

Read-only listing of inbound pack artifacts for the import prompt UX.

```json
{"action":"pack_landing_list","params":{"limit":20,"include_legacy_zip":false}}
```

Behavior notes:

- default landing folder: `state/mnemo/packs/inbox/`
- override with `MNEMO_PACK_LANDING_DIR`
- returns `.mem` packs with:
  - `filename`
  - `path`
  - `size_bytes`
  - `modified_at`
- ignores non-`.mem` files by default
- optional `include_legacy_zip=true` includes legacy `.zip` packs
- no import, move, or delete is performed

### `memory_group_discover`

Read-only deterministic discovery of computed memory groups from existing Mnemo rows.

```json
{"action":"memory_group_discover","params":{"query":"memory packs","include_imported":true,"limit_groups":20}}
```

Behavior notes:

- uses current Mnemo visibility rules (`local` by default, `include_imported`, `include_quarantine`, explicit namespace overrides)
- computes topic/domain/path/link/alias groups from existing SQLite data only
- alias groups use active alias concepts already present in SQLite alias runtime tables
- pending alias proposals do not create groups
- disabled alias terms/concepts do not create groups
- excludes mechanical topics such as `export:*`, `synthetic:run:*`, and `synthetic:cohort:*`
- returns bounded samples plus recommended scopes (`core`, `core_plus_related`, `full_tree`)

### `memory_group_preview`

Read-only resolution of one computed group into exact Mnemo `memory_ids`.

```json
{"action":"memory_group_preview","params":{"group_id":"topic:mnemo-memory-packs","scope":"core_plus_related","limit":500}}
```

Behavior notes:

- `group_id` is required
- alias-backed groups use `group_id="alias:<concept_id>"`
- scopes:
  - `core`
  - `core_plus_related`
  - `full_tree`
- returns exact `memory_ids`, bounded membership reasons, and pack-readiness output
- `pack_readiness.recommended_pack_selector.memory_ids` can be passed directly to `pack_preview`, `pack_redaction_preview`, or `pack_export`

Pack format notes:

- required ZIP members:
  - `manifest.json`
  - `content/memories.jsonl`
  - `content/topics.json`
  - `content/file_fingerprints.json`
  - `provenance/origin.json`
  - `provenance/redactions.json`
- signed packs also include:
  - `signature/signature.json`
- rows in `content/memories.jsonl` use pack-local row IDs (`ctx_###`, `hip_###`), not source DB `mem_*` IDs
- `manifest.json` includes SHA-256 `content_hash` over covered content/provenance members
- successful export writes one `exported_packs` audit row
- unsigned reason values accepted by inspector:
  - `operator_chose_unsigned` (current)
  - `signing_not_implemented` (historical compatibility)

### `pack_inspect`

Read-only inspection and validation for exported memory-pack files.

```json
{"action":"pack_inspect","params":{"pack_path":"D:/packs/my_pack.mem","include_samples":false,"sample_limit":5}}
```

Key params:

- `pack_path` (required local filesystem path)
- `include_samples` (default `false`)
- `sample_limit` (default `5`, capped to `20`)
- `verification_secret` (optional; required only when verifying local-HMAC signature with `hmac-sha256-local-v1`)

Validation scope:

- ZIP safety checks:
  - member-count/size limits
  - encrypted-member rejection
  - duplicate member/path traversal/unsafe path rejection
- required members:
  - `manifest.json`
  - `content/memories.jsonl`
  - `content/topics.json`
  - `content/file_fingerprints.json`
  - `provenance/origin.json`
  - `provenance/redactions.json`
- schema support (`pack_schema_version=1` in Phase 3a)
- content hash verification against canonical covered members (not attacker-controlled subsets)
- redaction metadata consistency checks
- source DB memory ID leak detection (`mem_*`) in required JSON payloads
- signature checks when present:
  - signed packs require `signature/signature.json`
  - `manifest.signature.signature_member` must equal `signature/signature.json`
  - `signature/signature.json` and `manifest.signature` metadata must match
  - local-HMAC verification runs when `verification_secret` is supplied

Status/recommendation behavior:

- `status`: `valid`, `invalid`, `unsupported`
- `import_recommendation`: `quarantine_only` or `reject`
- unsigned valid packs are marked `quarantine_only`
- valid signed packs are still `quarantine_only` in 0.20.1 (trusted import is not implemented)
- malformed/tampered/unsupported packs are marked `reject`
- invalid/unsupported packs return no samples
- `.mem` is the preferred public suffix
- legacy `.zip` suffixes are still accepted with compatibility warning `legacy_zip_suffix`
- trust/signature classification is returned under `structuredContent.signature.trust_classification`:
  - `unsigned`
  - `signature_not_verified`
  - `trusted_signer`
  - `unknown_signer`
  - `disabled_signer`
  - `blocked_signer`
  - `invalid_signature`
  - `secret_fingerprint_mismatch`
  - `unsupported_signature`

Read-only behavior:

- no database mutation
- no ZIP extraction
- no import/promotion mutation from inspect
- no signature-key persistence from inspect

### `pack_import`

Validated pack import with explicit target policy (Phase 5b trusted import update).

```json
{"action":"pack_import","params":{"pack_path":"D:/packs/my_pack.mem","allow_unsigned_quarantine":true}}
```

```json
{"action":"pack_import","params":{"pack_path":"D:/packs/my_pack.mem","allow_trusted_import":true,"verification_secret":"..."}}
```

Key params:

- `pack_path` (required local ZIP path)
- exactly one target gate must be true:
  - `allow_unsigned_quarantine=true`, or
  - `allow_trusted_import=true`
- `verification_secret` is required for `allow_trusted_import=true`

Import gate:

- import reuses the shared `pack_inspect` validation engine
- trusted mode requires a verified trusted signer classification:
  - `status = valid`
  - `signature.present = true`
  - `signature.verified = true`
  - `signature.trust_classification = trusted_signer`
  - `trusted_import_available = true`
- quarantine mode remains available and safe for unsigned/unverified/unknown/operator-cautious flows

Import policy:

- trusted import target:
  - `namespace = pack:trusted:<pack_id>`
  - `imported_packs.trust_level = trusted`
  - `origin = imported`
- quarantine import target:
  - `namespace = pack:quarantine:<pack_id>`
  - `imported_packs.trust_level = quarantine`
  - `origin = imported`
- trusted import is not local adoption and does not auto-promote
- kinds are restricted to:
  - `context_block`
  - `hippocampus_entry`

Prompt UX notes:

- `/mnemo.memory-pack-import` is the normal user-facing import command
- no-input import should browse `pack_landing_list`, inspect one chosen pack, ask for quarantine vs trusted import, then review
- review should use `include_grouped_summary=true`

Imported data behavior:

- imported memories get new local `mem_*` IDs
- source DB IDs are not imported
- source git provenance is preserved:
  - `git_sha_at_write -> memories.git_sha`
  - `git_branch_at_write -> memories.git_branch`
  - `git_dirty_at_write -> memories.git_dirty`
- topics are imported with `memory_topics.source = pack_import`
- touched files are imported to `memory_files` with:
  - `memory_table = memory kind`
  - `memory_id = new local mem_*`
  - `file_sha = source/export SHA from pack`

Audit and collision behavior:

- successful imports insert one `imported_packs` row with `received_zip_sha256`
- `imported_pack_rows` maps `(pack_id, row_id_in_pack)` to local `memory_id`
- re-import handling:
  - same `pack_id` + same `received_zip_sha256` => `pack_already_imported`
  - same `pack_id` + different ZIP bytes => `pack_id_collision_distinct_content`
  - legacy missing hash => `pack_already_imported_legacy_unknown_hash`

Freshness metadata:

- imported memories store diagnostic `import_freshness` labels:
  - `verified`, `stale`, `missing`, `unknown`
- labels use existing git-aware SHA helpers when local repo state is available

Scope limits:

- no public-key signing mode in stdlib deployments
- no persistent secret store
- no key revocation/remote key discovery
- no automatic trusted promotion
- no alias-pack import

### `signer_add`

Registers signer metadata in local SQLite trust registry.

```json
{"action":"signer_add","params":{"signer_id":"alice.dev","secret":"<32+ chars>","trust_level":"trusted"}}
```

Key params:

- `signer_id` required; ASCII regex: `^[A-Za-z0-9._:-]{3,128}$`
- `secret` required for `hmac-sha256-local-v1` and minimum 32 chars
- `trust_level`: `trusted` or `blocked` (default `trusted`)
- `signature_algorithm`: currently `hmac-sha256-local-v1` only

Safety:

- raw secret is never stored
- registry stores `secret_fingerprint = sha256(secret_utf8).hexdigest()[:32]`
- action output never returns raw secret

### `signer_list`

Lists local signer registry rows.

```json
{"action":"signer_list","params":{"status":"active","trust_level":"trusted","limit":100}}
```

Key params:

- `status` optional: `active|disabled`
- `trust_level` optional: `trusted|blocked`
- `limit` defaults to `100`, capped at `500`

### `signer_disable`

Disables a signer without deleting it.

```json
{"action":"signer_disable","params":{"signer_id":"alice.dev"}}
```

### `signer_enable`

Re-enables a disabled signer.

```json
{"action":"signer_enable","params":{"signer_id":"alice.dev"}}
```

### `pack_list_imports`

Read-only list view of imported pack registry rows (Phase 4a).

```json
{"action":"pack_list_imports","params":{"trust_level":"quarantine","limit":50}}
```

Key params:

- `trust_level` (optional `quarantine|trusted`)
- `pack_id` (optional exact match)
- `namespace` (optional exact match)
- `include_counts` (default `true`)
- `include_topics` (default `true`)
- `include_freshness` (default `true`)
- `limit` (default `50`, capped to `200`)

Output highlights:

- `total` is the unlimited matching pack count
- `packs` is the limited returned slice (`limited=true` when truncated)
- each pack entry includes:
  - `pack_id`, `pack_name`, `namespace`, `trust_level`, `imported_at`
  - `source_label` as basename-only display
  - `received_zip_sha256` (intentionally visible for cross-machine comparison)
  - `memory_count`, `topic_count`, `memory_file_count`
  - parsed `freshness` summary when available
  - bounded `top_topics` (max 10, ordered by count desc then topic asc)

### `pack_review_import`

Read-only review of one imported pack's mapped quarantine rows from SQLite (Phase 4a).

```json
{"action":"pack_review_import","params":{"pack_id":"pack_...","topics":["auth"],"limit":100,"sample_limit":10}}
```

Key params:

- `pack_id` (required)
- optional row filters:
  - `topics`
  - `kinds`
  - `import_freshness` (`verified|stale|missing|unknown`)
  - `row_ids` (pack-local row IDs)
  - `memory_ids` (local imported `mem_*` IDs)
  - `touched_paths`
- optional `query`:
  - case-insensitive substring matching against imported `text`/`title` only
  - no FTS, no ranking
- `include_samples` (default `true`)
- `sample_limit` (default `10`, capped to `50`)
- `limit` (default `100`, capped to `1000`)

Behavior notes:

- review scope is restricted to rows mapped by `imported_pack_rows` for the given `pack_id`
- filters combine with AND semantics
- list values inside one filter use OR semantics
- memory IDs that exist but are outside the selected pack are filtered with warning code `memory_ids_outside_pack_filtered`
- samples expose promotion mapping fields when present:
  - `promoted_to_memory_id`
  - `promotion_id`
  - `promoted_at`
- optional `include_grouped_summary=true` adds bounded topic/domain/path grouping and suggested promotion groups from already-imported SQLite rows

### `pack_promote_preview`

Read-only preview of a future promotion plan (Phase 4a). No promotion mutation occurs.

```json
{"action":"pack_promote_preview","params":{"pack_id":"pack_...","kinds":["context_block"],"limit":100}}
```

Key params:

- `pack_id` (required)
- accepts the same row filters as `pack_review_import` except `query`
- `include_samples` (default `true`)
- `sample_limit` (default `10`, capped to `50`)
- `limit` (default `100`, capped to `1000`)

Behavior notes:

- quarantine and trusted imported packs are eligible in this phase (`trust_level in {'quarantine','trusted'}`)
- target preview is fixed:
  - `target_namespace = local`
  - `target_origin = promoted`
- output contains candidate rows with preserved pack/import/git provenance
- no new memory rows are created; candidates mark `would_generate_memory_id=true`
- with no explicit row filters, warning code `preview_all_pack_rows` is returned
- limited previews emit `promotion_preview_limited`; candidate output is additionally bounded and may emit `candidate_rows_truncated`
- trusted-source previews emit warning code `trusted_import_source` with `phase="preview"`

Scope limits:

- no actual promotion in 0.19.0
- no trusted import, signing, signature verification, trust-store policy, or alias-pack import

### `pack_promote`

Manual promotion from imported quarantine rows into local promoted rows (Phase 4b).

```json
{"action":"pack_promote","params":{"pack_id":"pack_...","row_ids":["ctx_001"],"confirm_promote":true}}
```

Key params:

- `pack_id` (required)
- accepts row filters aligned with `pack_promote_preview`:
  - `topics`
  - `kinds`
  - `import_freshness`
  - `row_ids`
  - `memory_ids`
  - `touched_paths`
- `limit` (default `100`, capped to `1000`)
- `allow_limited_promotion` (default `false`)
- `allow_promote_all` (default `false`)
- `confirm_promote` (required true gate)

Hard guards:

- `confirm_promote=true` is mandatory
- if no row filters are supplied, `allow_promote_all=true` is mandatory
- if selected rows exceed `limit`, promotion fails unless `allow_limited_promotion=true`
- `query` filter is rejected for mutation (`query_filter_not_allowed_for_promotion`)
- only imported packs with `trust_level` in `{'quarantine','trusted'}` are eligible
- duplicate promotion of selected rows is rejected (`pack_rows_already_promoted`)

Mutation scope:

- creates new local `mem_*` rows with:
  - `namespace=local`
  - `origin=promoted`
- writes promotion mappings/audit:
  - `promoted_pack_rows`
  - `promotion_audit`
- copies topics with `memory_topics.source=promotion`
- copies memory-files links with preserved imported `file_sha`

Non-mutation guarantees:

- imported quarantine rows remain unchanged
- no pack ZIP reads/writes
- trusted-source promotions emit warning code `trusted_import_source` with `phase="promotion"`
- no trusted-import bypass into local namespace, signing, trust-store, or alias-pack promotion in this phase
- repeated limited promotions should use narrower filters or explicit `row_ids`; `skip_already_promoted`/`allow_repromote` are not implemented in 0.20.1

### Memory Packs Stabilization (0.19.6)

`0.19.6` is a stabilization release, not a new Memory Packs feature phase.

Stabilization focus:

- lifecycle regression coverage across:
  - `pack_preview`
  - `pack_redaction_preview`
  - `pack_export`
  - `pack_inspect`
  - `pack_import`
  - `pack_list_imports`
  - `pack_review_import`
  - `pack_promote_preview`
  - `pack_promote`
- migration/idempotency safety checks
- read-only contract regression checks
- artifact safety checks (hash/redaction/source-ID leak guards)
- retrieval boundary checks (local/promoted/quarantine visibility)

Scope remains unchanged:

- no signing/trust policy implementation
- no trusted import
- no alias-pack import/export

### Memory Packs Signing/Trust Foundation (0.20.0)

`0.20.0` introduces the first signing/trust layer while keeping quarantine as the import target.

Key additions:

- signer registry actions:
  - `signer_add`
  - `signer_list`
  - `signer_disable`
  - `signer_enable`
- signed export option:
  - `pack_export` with `sign_pack=true`
  - local-HMAC signature member `signature/signature.json`
- signature verification/classification in `pack_inspect`:
  - unsigned/not-verified/trusted/unknown/blocked/disabled/invalid/mismatch/unsupported states

Important scope:

- stdlib-only mode uses local HMAC (`hmac-sha256-local-v1`)
- this is not public-key signing and not non-repudiation
- trusted import remains future work; valid signed packs still return `quarantine_only`

### Trusted Import Policy

Trusted import is NOT local adoption.

- trusted imported rows are stored under `pack:trusted:<pack_id>`
- imported rows remain `origin=imported`
- trusted import does not write direct `namespace=local` rows

Trusted import is NOT automatic promotion.

- manual promotion is still explicit via `pack_promote`
- `pack_promote` gates remain in force (`confirm_promote`, selection filters, limit guards)
- manual promotion remains explicit

Trusted import is NOT default retrieval.

- default retrieval continues to scope to `namespace=local`
- trusted imports require `include_imported=true` or explicit namespace
- quarantine imports require `include_quarantine=true` or explicit namespace

Trusted import requires verified trust.

- `pack_inspect` classification must be `trusted_signer`
- signature must verify with `verification_secret`
- signer must be active/trusted and fingerprint-matched
- operator must pass `allow_trusted_import=true`

Quarantine remains the safe fallback.

- unsigned/unverified/unknown/invalid/blocked/disabled/mismatch/unsupported signature states cannot trusted-import
- valid unsigned or cautious imports continue to use `allow_unsigned_quarantine=true`

### Memory Packs lifecycle recap

Current workflow through `0.21.1`:

1. `record` + `topic_add`
2. `pack_preview`
3. `pack_redaction_preview`
4. `pack_export` (unsigned or signed local-HMAC mode)
5. `pack_inspect` (structure/hash + signature classification/verification + `trusted_import_available`)
6. `pack_import`:
   - trusted mode: `allow_trusted_import=true` + `verification_secret`
   - quarantine mode: `allow_unsigned_quarantine=true`
7. `pack_list_imports` + `pack_review_import`
8. `pack_promote_preview`
9. `pack_promote` (manual local promotion)

Retrieval include-flag semantics (`0.21.1`):

- default: local namespace only
- `include_imported=true`: adds trusted imported namespaces
- `include_quarantine=true`: adds quarantine imported namespaces
- both flags together: include both trusted and quarantine imported namespaces
- explicit `namespace`/`namespaces`: overrides include flags

Scope/limits retained:

- local HMAC remains the signing mode in stdlib-only deployments
- no public-key identity and no non-repudiation in local-HMAC mode
- no persistent secret store
- no key revocation or remote key discovery
- no automatic trusted promotion
- no alias pack import/export

### Memory Packs 0.21.1 stabilization

`0.21.1` is a stabilization pass, not a new feature phase.

Stabilization coverage includes:

- trusted/quarantine retrieval-boundary and include-flag regressions
- trusted lifecycle, quarantine fallback lifecycle, and unsigned lifecycle regressions
- cross-trust reimport/collision regressions
- trusted-import tamper rejection regressions
- verification-secret scrubbing regressions
- trusted-source promotion provenance/freshness regressions

If you migrate from pre-5b assumptions:

- `include_imported=true` now means trusted imported namespaces
- `include_quarantine=true` means quarantine imported namespaces
- use both for combined imported scope

### Memory Packs v1 status

Complete for practical local export/import workflows when synthetic readiness reports `memory_packs_v1_status=ready`.

Current v1 lifecycle:

- export
- inspect
- import (trusted or quarantine)
- review
- promotion preview
- manual promotion

Future optional extensions:

- public-key signing
- persistent secret store
- revocation/discovery workflows
- alias-pack flows
- skip-already-promoted ergonomics

### `search`

Searches project memories relevant to a query.

```json
{"action":"search","params":{"query":"SQLite signature backfill","limit":5}}
```

Namespace/origin scope params:

- `namespace` or `namespaces` (mutually exclusive)
- `include_imported` (adds trusted imported namespaces)
- `include_quarantine` (adds quarantine namespaces)
- `origin` or `origins` (origin filters applied only when explicitly supplied)

Default namespace scope when omitted:

- `['local']`

Miss tagging:

- search-like events include typed `result_count`, `success`, and `top_score`
- miss is recorded when `result_count == 0` or `top_score < MNEMO_MISS_TOP_SCORE_THRESHOLD` (default `0.15`)

Git-aware freshness weighting (SQLite mode):

- base ranking math is unchanged, then multiplied by freshness
- legacy rows / `git_sha=NULL`: `1.0`
- touched files unchanged: `1.0`
- any touched file changed: `0.7`
- any touched file missing/deleted: `0.3`
- mixed states use the minimum multiplier

No backfill is performed for legacy rows.

### `recall`

Returns bounded startup or agent-context bundles.

```json
{"action":"recall","params":{"mode":"startup","role":"coordinator","recent_logs":20}}
```

```json
{"action":"recall","params":{"mode":"agent","agent_id":"spec_auth","domain":"auth","task":"review middleware"}}
```

Recall supports the same namespace/origin scope params as `search` and defaults to `namespace='local'`.

When `query`/`task` is present, recall writes miss-aware query events using returned score evidence where available.

### `get`

Retrieves one memory by id.

```json
{"action":"get","params":{"id":"mem_123","full":true}}
```

### `link`

Links one memory to another.

```json
{"action":"link","params":{"source_id":"mem_a","target_id":"mem_b","relation":"expands","bidirectional":true}}
```

### `export`

Exports memories to local readable files.

Formats include:

- `jsonl`
- `json`
- `markdown`
- `hippocampus_markdown`
- `agent_feedback_markdown`
- `startup_context_markdown`

```json
{"action":"export","params":{"format":"jsonl"}}
```

Default exports go under `state/mnemo/exports/`.

### `compact_context`

Builds a prompt-ready context block grouped by memory kind.

```json
{"action":"compact_context","params":{"query":"auth middleware changes","limit":8,"max_tokens":2000}}
```

`compact_context` supports the same namespace/origin scope params as `search`.

### `salience_check`

Runs optional Agent Salience diagnostics when `agent-salience` is importable.

```json
{"action":"salience_check","params":{"text":"auth middleware decisions","candidate_limit":500,"max_scored":100}}
```

`salience_check` supports the same namespace/origin scope params as `search`.

The action is candidate-limited. In SQLite mode it uses FTS when available, then signature overlap, then scores bounded survivors.

Event tagging:

- when no match triggers the supplied threshold, `success=0` is recorded
- `top_score` stores the best scored candidate (or `0.0`)

When local IDF profiles are active, `salience_check` passes `mode="auto"` and the active project/domain `idf_profile` into Agent Salience scoring.

Active-IDF scoring uses an IDF-dominant mix:

- `idf_cosine: 0.55`
- `idf_jaccard: 0.35`
- `cosine: 0.05`
- `jaccard: 0.05`

This adds weighted Jaccard/Tanimoto (`idf_jaccard`) to reduce common-word overlap false positives.

Diagnostics include:

- `idf_used`
- `idf_scope_used`
- `idf_profile_status`
- `score_breakdown.cosine`
- `score_breakdown.jaccard`
- `score_breakdown.idf_cosine`
- `score_breakdown.idf_jaccard`
- `score_weights`

When IDF is cold, disabled, or unavailable, lexical scoring remains active and `idf_used` stays false.

### `maintenance`

Runs maintenance sub-actions.

#### `compact_logs`

```json
{"action":"maintenance","params":{"action":"compact_logs","older_than_count":20,"max_logs":50,"dry_run":true}}
```

#### `consolidate`

Candidate-based consolidation. This is the default safe path.

```json
{"action":"maintenance","params":{"action":"consolidate","dry_run":true,"max_candidates_per_memory":100}}
```

Default consolidation checks exact hashes globally, then uses bounded candidate retrieval and shingle overlap before full similarity.

#### `consolidate_full`

Explicit O(n²) full scan. Requires confirmation.

```json
{"action":"maintenance","params":{"action":"consolidate_full","confirm_full_scan":true,"dry_run":true}}
```

Without `confirm_full_scan:true`, this returns `full_scan_confirmation_required` with an estimated pair count.

#### `backfill_signatures`

Backfills missing or outdated v0.12.0 signatures.

```json
{"action":"maintenance","params":{"action":"backfill_signatures","dry_run":true}}
```

```json
{"action":"maintenance","params":{"action":"backfill_signatures","dry_run":false}}
```

`doctor` warns when more than 10% of active records are unsigned/outdated.

#### `propose_aliases`

Generates structured alias proposals from recent miss evidence and optional `alias_hint` events.

```json
{"action":"maintenance","params":{"action":"propose_aliases","window_days":30,"domain":"agentic","min_recurrence":3,"limit":20,"dry_run":true,"include_hints":true,"min_loose_score":0.20,"max_candidates_per_cluster":5}}
```

Output status values:

- `ok`
- `idf_cold`
- `no_misses`
- `no_proposals`

Persistence behavior:

- `dry_run=true`: returns proposals without writing
- `dry_run=false`: persists pending proposals in SQLite

Admission uses continuous alias `idf_strength` with `MNEMO_ALIAS_MIN_IDF_STRENGTH` (default `0.30`). `idf_terms` and `penalized_terms` remain proposal evidence for review; they are no longer binary admission gates.

#### `list_alias_proposals`

Lists proposal rows from `alias_proposals`.

```json
{"action":"maintenance","params":{"action":"list_alias_proposals","status":"pending","domain":"agentic","limit":50}}
```

#### `approve_alias`

Creates/updates `alias_concepts` + `alias_terms` and marks proposal approved when `proposal_id` is supplied.

```json
{"action":"maintenance","params":{"action":"approve_alias","proposal_id":"alias-prop-...","approved_by":"coordinator"}}
```

#### `reject_alias_proposal`

Marks a proposal rejected without deleting evidence.

```json
{"action":"maintenance","params":{"action":"reject_alias_proposal","proposal_id":"alias-prop-...","reason":"generic wording"}}
```

#### `list_aliases`

Lists active vocabulary rows.

```json
{"action":"maintenance","params":{"action":"list_aliases","domain":"agentic","language":"en","status":"active","limit":200}}
```

#### `disable_alias`

Disables one alias term without deleting it.

```json
{"action":"maintenance","params":{"action":"disable_alias","alias_id":"alias-term-...","reason":"deprecated"}}
```

#### `disable_alias_concept`

Disables one alias concept; runtime ignores disabled concepts.

```json
{"action":"maintenance","params":{"action":"disable_alias_concept","concept_id":"alias-concept-...","reason":"deprecated"}}
```

#### `import_json`

Imports JSON memories and computes signatures during import.

```json
{"action":"maintenance","params":{"action":"import_json","path":"state/mnemo/memory.json","dry_run":true}}
```

### Top-level maintenance aliases

For schema discoverability, these are also accepted as top-level gateway actions:

```json
{"action":"backfill_signatures","params":{"dry_run":false}}
```

```json
{"action":"consolidate_full","params":{"confirm_full_scan":true,"dry_run":true}}
```

### `inspect`

Reads lifecycle history and/or related-memory graph.

```json
{"action":"inspect","params":{"id":"mem_123","mode":"both","include_archive":true}}
```

### `recent`

Returns the most recently recorded memories.

```json
{"action":"recent","params":{"limit":10}}
```

### `recent_events`

Returns newest event rows first.

Typed query metadata includes `result_count`, `success`, and `top_score` when available.

```json
{"action":"recent_events","params":{"limit":20,"action":"optional","kind":"optional","domain":"optional"}}
```

### `search_events`

Searches event history across action, memory id, query text, summaries, salience text, and identity fields.

```json
{"action":"search_events","params":{"query":"IBAN validation","limit":20,"action":"optional","domain":"optional"}}
```

### `get_event`

Returns one event by id.

```json
{"action":"get_event","params":{"event_id":"evt_..."}}
```

### `memory_events`

Returns events related to one memory id.

```json
{"action":"memory_events","params":{"memory_id":"mem_...","limit":50}}
```

### `update`

Updates fields on an existing memory.

```json
{"action":"update","params":{"id":"mem_123","tags":["release"]}}
```

### `delete`

Soft-deletes an existing memory.

```json
{"action":"delete","params":{"id":"mem_123","reason":"obsolete"}}
```

### `lookup_symbol`

Finds likely source definition locations under `MNEMO_WORKSPACE_ROOT`.

```json
{"action":"lookup_symbol","params":{"name":"authenticate","limit":10}}
```

## Signature fields

SQLite rows include deterministic signatures:

- `content_hash`
- `normalized_hash`
- `token_count`
- `unique_token_count`
- `top_terms_json`
- `shingle_hashes_json`
- `signature_version`
- `normalizer_version`
- `signature_updated_at`

`content_hash` uses full raw text after stable line-ending normalization. Token-based signatures are capped at 50,000 characters.

## Schema compatibility

Mnemo exports a conservative MCP schema for Copilot-style clients. Advanced JSON Schema features are intentionally avoided; handlers enforce ranges, defaults, and validation.
