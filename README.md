# Mnemo MCP

Local-first project memory for MCP-capable coding agents.

Mnemo is a small stdio MCP server that gives coding agents a durable, project-scoped memory substrate. It stores decisions, interaction logs, context blocks, durable project knowledge, specialist feedback, useful commands, paths, failed approaches, and test results across sessions.

> Mnemo is a local project hippocampus: store broadly, index locally, recall narrowly, export readably, compact aggressively.

## Status

Current version: **0.21.2**

Runtime requirements:

- Python **3.10+**
- Standard library only
- SQLite-first local storage
- Optional: local `agent-salience` via `AGENT_SALIENCE_HOME` or normal Python import
- CI compatibility matrix enforces parse/test coverage on Python `3.10`, `3.11`, `3.12`, `3.13`

Mnemo is local-first. It does not require a cloud service, vector database, external database server, or package install.

## What Mnemo provides

- Local SQLite project memory by default
- JSON import/export compatibility for older `memory.json` files
- JSONL and Markdown exports for human inspection
- Bounded search and recall so memory growth does not automatically become token growth
- Structured memory layers for agentic systems
- Signature-at-write-time duplicate detection and consolidation support
- Candidate-based consolidation by default, with full O(n²) scan gated behind explicit confirmation
- Maintenance actions for log compaction, consolidation, JSON import, signature backfill, and alias proposal analysis
- Git-aware memory metadata on new writes (`git_sha`, `git_branch`, `git_dirty`) with touched-file tracking
- Freshness-aware retrieval multiplier in SQLite mode for file-linked memories
- Memory Packs Phase 1 substrate: namespaces/origins, topic metadata, and namespace-aware retrieval filters
- Memory Packs Phase 2a read-only pack selection preview (`pack_preview`)
- Memory Packs Phase 2b redaction dry-run preview (`pack_redaction_preview`)
- Memory Packs Phase 2c unsigned local ZIP export (`pack_export`)
- Memory Packs Phase 3a read-only ZIP inspect/validate (`pack_inspect`)
- Memory Packs Phase 3b basic unsigned ZIP import into quarantine (`pack_import`)
- Memory Packs Phase 4a read-only quarantine review and promotion preview (`pack_list_imports`, `pack_review_import`, `pack_promote_preview`)
- Memory Packs Phase 4b manual quarantine promotion to local (`pack_promote`)
- Memory Packs Phase 5b trusted-import policy and implementation (`pack_import` trusted mode to `pack:trusted:<pack_id>`)
- Memory Packs Phase 5a signing/trust foundation (`signer_add`, `signer_list`, `signer_disable`, `signer_enable`) and optional local-HMAC pack signing
- A single Copilot-friendly gateway MCP tool: `mnemo`
- Optional deterministic salience diagnostics
- Automatic local IDF activation when project/domain corpus maturity thresholds are met
- Lightweight symbol lookup under a configured workspace root

## Non-goals

Mnemo is not:

- an agent framework
- a hosted memory service
- a vector database
- a secrets manager
- a replacement for tests or source control

## Git-aware memory (0.13.5)

New memory writes in SQLite mode attempt to capture repository context:

- `git_sha` (HEAD commit)
- `git_branch` (current branch)
- `git_dirty` (`1` when working tree has local changes, else `0`)

When `record` receives `touched_files`, Mnemo records per-file digests in `memory_files`:

- preferred: git blob SHA at `HEAD:path`
- fallback: current file digest from `git hash-object` (or BLAKE2b-128 bytes digest when git hashing is unavailable)

Retrieval applies a post-score freshness multiplier:

- `git_sha` is `NULL` (legacy or non-git write): `1.0`
- all touched files unchanged: `1.0`
- any touched file changed but still present: `0.7`
- any touched file missing/deleted/renamed away: `0.3`
- mixed file states: minimum multiplier wins

Safety and compatibility:

- Non-git folders and git command failures are non-fatal.
- No backfill is performed for legacy rows.
- Legacy rows remain neutral (`1.0`) to preserve prior retrieval behavior.

## Memory Packs Phase 1 (0.14.0)

Phase 1 adds the SQLite substrate for future memory-pack workflows without enabling pack import/export policy workflows yet.

New memory metadata columns on `memories`:

- `namespace` (defaults to `local`)
- `origin` (defaults to `local`)
- `import_freshness` (nullable placeholder for later import phases)

New SQLite tables:

- `memory_topics(memory_id, topic, created_at, source)`
- `imported_packs` (placeholder registry)
- `exported_packs` (placeholder registry)

New first-class topic actions:

- `topic_add`
- `topic_remove`
- `topic_list`

Namespace-aware retrieval filters are now available across retrieval paths:

- default namespace scope is `local`
- `include_imported=true` adds trusted imported namespaces
- `include_quarantine=true` adds quarantine namespaces (never auto-included otherwise)
- origin filtering is applied only when `origin`/`origins` is explicitly provided

Freshness multiplier rules remain unchanged and are still applied after lexical/IDF scoring:

- legacy row or `git_sha=NULL`: `1.0`
- all touched files unchanged: `1.0`
- any touched file changed: `0.7`
- any touched file missing/deleted: `0.3`
- mixed file states use the minimum multiplier

Compatibility notes:

- existing rows default to `namespace='local'`, `origin='local'`, `import_freshness=NULL`
- legacy rows remain retrieval-neutral unless explicitly filtered
- no backfill is performed for topic metadata
- Phase 1 does **not** add pack export/import, redaction, signing, trust-store policy, or promotion flows

## Memory Packs Phase 2a (0.15.0)

Phase 2a adds a read-only selection engine for future pack export workflows via `pack_preview`.

`pack_preview` supports deterministic preview filtering by:

- `topics` (via `memory_topics` joins, not body text)
- `kinds` (default preview kinds: `context_block`, `hippocampus_entry`)
- `namespace` / `namespaces` with existing Phase 1 scope semantics
- `include_imported` / `include_quarantine`
- `origin` / `origins` (applied only when explicitly provided)
- `created_after` / `created_before`
- `touched_paths` (via `memory_files` joins)

Preview output includes:

- true selected row count and bounded row ID list
- counts by kind, namespace, origin, and top topics
- top referenced files from `memory_files`
- bounded per-kind samples with compact text previews
- structured warnings (for example, preview-policy warnings for `interaction_log`/`agent_feedback`)

Phase 2a is intentionally read-only:

- no `exported_packs` writes
- no pack files
- no zip export/import
- no redaction/signing/trust/promotion logic

## Memory Packs Phase 2b (0.16.0)

Phase 2b adds a read-only redaction dry-run action: `pack_redaction_preview`.

Phase 2b behavior:

- reuses the same pack selection semantics as `pack_preview`
- scans selected rows for baseline built-in redaction categories
- returns category counts and bounded redacted samples
- never returns original matched sensitive literals in structured output

Built-in baseline categories in this phase:

- `private_key_header`
- `jwt`
- `aws_access_key`
- `email`
- `user_path`
- `ipv4`

Compatibility and scope:

- `pack_preview` no longer performs content bootstrap on empty/fresh SQLite DBs
- schema creation/migration may still occur on first open
- no ZIP export/import/signing/trust/promotion flows
- baseline ruleset is intentionally limited and does not cover all secret formats (for example IPv6 and many provider-specific token formats)

## Memory Packs Phase 2c (0.17.0)

Phase 2c adds a local ZIP export action: `pack_export`.

Key behavior:

- requires `allow_unsigned=true` because signing is not implemented yet
- reuses `pack_preview` selection semantics (topic/kind/namespace/origin/date/touched_paths filters)
- export policy is strict in this phase:
  - exportable kinds: `context_block`, `hippocampus_entry`
  - non-exportable kinds (`interaction_log`, `agent_feedback`) fail early
- mandatory baseline-v1 redaction runs again during export (dry-run output is not trusted)
- exported content uses pack-local row IDs (`ctx_###`, `hip_###`), not source DB `mem_*` IDs
- writes a ZIP with required members:
  - `manifest.json`
  - `content/memories.jsonl`
  - `content/topics.json`
  - `content/file_fingerprints.json`
  - `provenance/origin.json`
  - `provenance/redactions.json`
- `manifest.json` includes a SHA-256 `content_hash` over covered content/provenance members
- successful exports write one `exported_packs` audit row
- default output directory is `state/mnemo/packs/exports/`

Scope limits in 0.17.0:

- unsigned development packs only
- no import, signing, trust-store policy, or promotion workflows
- baseline redaction ruleset is intentionally incomplete and not a full DLP system

## Memory Packs Phase 3a (0.18.0)

Phase 3a adds a read-only ZIP inspection action: `pack_inspect`.

Key behavior:

- validates required pack members and JSON/JSONL structure without extracting ZIPs
- enforces ZIP safety checks (member-path validation, duplicate/traversal rejection, bounded ZIP size limits)
- validates `manifest.json` schema and key metadata for Phase 2c packs (`pack_schema_version=1`)
- recomputes `content_hash` over the canonical covered-member list:
  - `content/file_fingerprints.json`
  - `content/memories.jsonl`
  - `content/topics.json`
  - `provenance/origin.json`
  - `provenance/redactions.json`
- rejects covered-member list tampering even if attacker-provided manifest hashes are internally consistent
- validates redaction metadata consistency between manifest and provenance payloads
- rejects packs that leak source DB-style IDs matching `mem_*`

Status and recommendation model:

- `status` is one of: `valid`, `invalid`, `unsupported`
- valid unsigned packs (`signed=false`, `unsigned_reason=signing_not_implemented`) return recommendation `quarantine_only`
- malformed/tampered/unsupported packs return recommendation `reject`

Scope limits in 0.18.0:

- inspection only; no import is performed
- no signing verification, trust store, or promotion flow
- one bad row invalidates the whole pack in this phase (no partial-row acceptance logic)

## Memory Packs Phase 3b (0.18.5)

Phase 3b adds basic quarantined import via `pack_import`.

Import gate and scope:

- import uses the same shared validation engine as `pack_inspect`
- import is allowed only when `allow_unsigned_quarantine=true`
- only valid unsigned packs (`signed=false`, `unsigned_reason=signing_not_implemented`) are accepted
- target namespace is fixed to `pack:quarantine:<pack_id>`
- trust level is fixed to `quarantine`
- no trusted import, signing, signature verification, trust-store, promotion, or alias import in this phase

Imported row behavior:

- imported rows get newly generated local `mem_*` IDs
- source DB IDs are not imported
- kind support remains strict (`context_block`, `hippocampus_entry`)
- pack git provenance is preserved on imported rows:
  - `git_sha_at_write -> memories.git_sha`
  - `git_branch_at_write -> memories.git_branch`
  - `git_dirty_at_write -> memories.git_dirty`
- topics are imported into `memory_topics` with `source=pack_import`
- touched files are imported into `memory_files` using:
  - `memory_table=<memory kind>`
  - `memory_id=<new local mem_* id>`
  - `file_sha=<source/export SHA from the pack>`

Import diagnostics and audit:

- `imported_packs` stores `received_zip_sha256` for exact-byte collision checks
- `imported_pack_rows` maps `pack_id + row_id_in_pack` to local `memory_id`
- import freshness labels are diagnostic (`verified|stale|missing|unknown`) and use existing git-aware SHA helpers
- re-import is rejected by default:
  - same pack ID + same ZIP hash => `pack_already_imported`
  - same pack ID + different ZIP hash => `pack_id_collision_distinct_content`

Retrieval visibility:

- imported quarantine rows are excluded from default retrieval
- `include_imported=true` alone does not include quarantine rows
- quarantine rows are visible only with `include_quarantine=true` or explicit quarantine namespace filtering

## Memory Packs Phase 4a (0.19.0)

Phase 4a adds post-import operator review tooling over imported SQLite data:

- `pack_list_imports`: list imported pack registry rows with compact counts and freshness summaries
- `pack_review_import`: review one imported pack's mapped quarantine rows from SQLite
- `pack_promote_preview`: preview a future promotion plan to `namespace=local`, `origin=promoted`

Read-only behavior:

- no pack ZIP reads for review/preview
- no promotion mutation
- no trust-level changes
- quarantine rows remain unchanged

Review/preview details:

- list/review output shows `received_zip_sha256` intentionally for cross-machine pack-byte comparison
- list/review output normalizes `source_label` to basename display
- review filters support topics/kinds/import_freshness/row_ids/memory_ids/touched_paths
- review `query`, when supplied, is simple case-insensitive substring matching over imported `text`/`title` fields only (no FTS/ranking)
- promotion preview candidates preserve imported git and pack provenance metadata in the returned plan

Scope limits in 0.19.0:

- no actual promotion
- no trusted import/signing/signature verification/trust-store policy
- no alias-pack import/export

## Memory Packs Phase 4b (0.19.5)

Phase 4b adds manual promotion from imported quarantine rows into local Mnemo memory via `pack_promote`.

Promotion gate and policy:

- `confirm_promote=true` is required
- explicit row filters are required, or `allow_promote_all=true` must be supplied
- if selection exceeds `limit`, promotion fails unless `allow_limited_promotion=true`
- duplicate promotion of the same `(pack_id, row_id_in_pack)` is rejected in this phase
- query-based fuzzy selection is intentionally not allowed for mutation (`query_filter_not_allowed_for_promotion`)

Promotion target and invariants:

- source rows remain unchanged in quarantine (`namespace=pack:quarantine:<pack_id>`, `origin=imported`)
- promoted copies are written as new local rows (`namespace=local`, `origin=promoted`)
- promoted rows receive new local `mem_*` IDs
- no quarantine delete/move occurs

Promotion provenance and audit:

- `promoted_pack_rows` maps:
  - `pack_id + row_id_in_pack + imported_memory_id -> promoted_memory_id`
  - also stores `promotion_id`, `promoted_at`, and `original_import_freshness`
- `promotion_audit` stores one row per promotion batch with:
  - `promotion_id`
  - canonical `filters_json`
  - row count and limited/allow flags

Preserved row content/provenance:

- kind/text/title copied from imported quarantine row
- topics copied to promoted row with `memory_topics.source=promotion`
- `memory_files` copied with new promoted memory ID and same source/export `file_sha`
- git provenance copied (`git_sha`, `git_branch`, `git_dirty`)
- `import_freshness` copied from import-time label (not recomputed during promotion)
- promoted-row created timestamp is promotion time by design (local adoption time)

Operational note:

- repeated limited promotions should use narrower filters or explicit `row_ids`
- `skip_already_promoted` and `allow_repromote` are not implemented in 0.21.1

## Memory Packs Stabilization (0.19.6)

0.19.6 is a stabilization pass, not a new Memory Packs feature phase.

What is tightened in this release:

- full lifecycle regression coverage for:
  - record/topic_add → pack_preview → pack_redaction_preview → pack_export → pack_inspect → pack_import → pack_list_imports → pack_review_import → pack_promote_preview → pack_promote
- migration/idempotency checks against multiple schema shapes
- action dispatch coverage for all Memory Packs actions
- read-only contract regression checks for read-only pack actions
- export artifact safety regression checks (required members, content hash verification, redaction, source-ID leak guard)
- retrieval boundary regression checks (local/promoted/quarantine visibility rules)
- synthetic stabilization run/report support under `_test_results/memory_packs_stabilization/`

Current lifecycle remains:

- export pack ZIP (`pack_export`)
- inspect/validate ZIP (`pack_inspect`)
- import valid unsigned pack into quarantine (`pack_import`)
- review imported quarantine rows (`pack_list_imports`, `pack_review_import`)
- preview promotion (`pack_promote_preview`)
- manually promote selected rows to local/promoted (`pack_promote`)

## Memory Packs Signing/Trust Foundation (0.20.0)

0.20.0 adds the first signing/trust layer while preserving quarantine-first import behavior.

What is added:

- signer registry actions:
  - `signer_add`
  - `signer_list`
  - `signer_disable`
  - `signer_enable`
- SQLite signer metadata table:
  - `trusted_signers`
- optional signed export mode in `pack_export`:
  - `sign_pack=true`
  - `signature_algorithm=hmac-sha256-local-v1`
  - `signer_id` + `signing_secret`
- signature verification/classification in `pack_inspect`:
  - verifies signature when `verification_secret` is supplied
  - classifies unsigned/not-verified/trusted/unknown/blocked/disabled/invalid/mismatch/unsupported states

Important cryptography scope in 0.20.0/0.20.1:

- default stdlib implementation is local HMAC signing (`hmac-sha256-local-v1`)
- this is not public-key signing and not non-repudiation
- the same shared secret signs and verifies
- anyone with the secret can produce signatures
- in 0.20.0/0.20.1 trusted import was not implemented; signed packs were `quarantine_only`

Secret policy:

- minimum secret length is 32 characters
- recommended generation: `secrets.token_hex(32)`
- raw secrets are never stored in SQLite signer rows, never exported into packs, and are scrubbed from logged params where generic action/event logging exists

## Trusted Import Policy

Trusted import is NOT local adoption.

- trusted imported rows are stored under `namespace=pack:trusted:<pack_id>`
- imported rows remain `origin=imported`
- trusted import never writes rows directly into `namespace=local`

Trusted import is NOT automatic promotion.

- `pack_promote` remains the only path that creates local adopted rows (`namespace=local`, `origin=promoted`)
- promotion gates remain explicit: `confirm_promote=true` and selection/limit checks
- manual promotion remains explicit

Trusted import is NOT default retrieval.

- default retrieval scope stays `namespace=local`
- trusted imported rows require `include_imported=true` or explicit namespace filtering
- quarantine rows require `include_quarantine=true` unless explicitly addressed by namespace

Trusted import requires verified trust.

- `pack_inspect` must verify the signature and classify the pack as `trusted_signer`
- signer must be registered, active, trusted, and fingerprint-matched
- operator must pass `allow_trusted_import=true` (and `verification_secret`)

Quarantine remains the safe fallback.

- unsigned, unverified, unknown, invalid, disabled, blocked, mismatch, and unsupported signature cases cannot trusted-import
- valid unsigned/unverified/unknown packs can still be imported with `allow_unsigned_quarantine=true`
- even signed trusted packs may still be intentionally imported into quarantine

Phase `0.21.0` still does not implement:

- public-key signing
- persistent secret store
- key revocation
- remote key discovery
- trusted auto-promotion
- alias pack import/export

## Memory Packs lifecycle recap

Current lifecycle through `0.21.1`:

1. Build/select local knowledge:
   - `record`, `topic_add`
2. Preview selection:
   - `pack_preview`
3. Redaction dry-run:
   - `pack_redaction_preview`
4. Export:
   - unsigned: `pack_export` + `allow_unsigned=true`
   - signed local-HMAC: `pack_export` + `sign_pack=true`
5. Inspect:
   - `pack_inspect` (structure/hash validation + signature classification/verification)
6. Import:
   - trusted import: `pack_import` + `allow_trusted_import=true` + `verification_secret`
   - quarantine import: `pack_import` + `allow_unsigned_quarantine=true`
7. Review imported rows:
   - `pack_list_imports`, `pack_review_import`
8. Promotion preview:
   - `pack_promote_preview`
9. Manual promotion:
   - `pack_promote` -> local `origin=promoted` rows

Retrieval semantics in `0.21.0`:

- default: local namespace only
- `include_imported=true`: adds trusted imported namespaces
- `include_quarantine=true`: adds quarantine namespaces
- both flags together: include both trusted and quarantine imported namespaces
- explicit `namespace`/`namespaces`: overrides include flags and uses listed namespaces verbatim

Operational notes:

- HMAC signing remains local shared-secret trust only (not public-key identity, not non-repudiation)
- secret distribution is out-of-band
- no persistent secret store yet; operators provide `verification_secret` at trusted-import time
- no pre-import trusted content browser yet; operators inspect metadata/signature first, then review rows after import

## Memory Packs 0.21.1 Stabilization

`0.21.1` is a stabilization pass, not a new Memory Packs feature phase.

Stabilization focus:

- trusted/quarantine include-flag semantics and retrieval-boundary regression coverage
- trusted and quarantine lifecycle regression coverage
- cross-trust reimport/collision regression coverage
- trusted-import tamper rejection regression coverage
- verification-secret scrubbing regression coverage
- trusted-source promotion provenance and freshness-preservation regression coverage
- policy/docs consistency checks and sync parity validation

If you previously treated `include_imported=true` as "all imported", migrate callers to:

- `include_imported=true` for trusted imported namespaces
- `include_quarantine=true` for quarantine imported namespaces
- both flags for combined imported scope

## Memory Packs v1 status

Complete for practical local export/import workflows when the synthetic readiness gate reports `memory_packs_v1_status=ready`.

Current v1 workflow:

- export
- inspect
- import quarantine or trusted
- review
- promotion preview
- manual promotion

Future optional extensions:

- public-key signing
- persistent secret store
- revocation/discovery workflows
- alias-pack flows
- skip-already-promoted ergonomics

## Repository layout

```text
mnemo-mcp/
├── server.py
├── salience_loader.py
├── memory.example.json
├── smoke_test.py
├── test_server.py
├── benchmark_consolidation.py
├── examples/
├── docs/
├── CHANGELOG.md
└── README.md
```

## Quick start

Point your MCP client at `server.py`.

Example VS Code MCP config when Mnemo is checked out as `mnemo-mcp/` inside your workspace:

```json
{
  "servers": {
    "mnemo": {
      "type": "stdio",
      "command": "python",
      "args": ["${workspaceFolder}/mnemo-mcp/server.py"],
      "env": {
        "MNEMO_STORE": "sqlite",
        "MNEMO_WORKSPACE_ROOT": "${workspaceFolder}",
        "MNEMO_FILE": "${workspaceFolder}/state/mnemo/memory.json",
        "MNEMO_SQLITE_FILE": "${workspaceFolder}/state/mnemo/mnemo.sqlite"
      }
    }
  }
}
```

More example configs are in [`examples/`](examples/).

## MCP gateway model

Mnemo exposes exactly one public MCP tool:

```text
mnemo
```

Call it with an `action` and optional `params` object:

```json
{"action":"record","params":{"kind":"decision","text":"Run validation commands before handoff."}}
```

This gateway model keeps the MCP surface small for clients with tool-inventory limits while preserving the full Mnemo feature set.

## Gateway actions

Supported top-level actions:

- `doctor`
- `search`
- `salience_check`
- `pack_preview`
- `pack_redaction_preview`
- `pack_export`
- `pack_inspect`
- `pack_import`
- `pack_list_imports`
- `pack_review_import`
- `pack_promote_preview`
- `pack_promote`
- `signer_add`
- `signer_list`
- `signer_disable`
- `signer_enable`
- `record`
- `alias_hint`
- `link`
- `recall`
- `get`
- `export`
- `update`
- `delete`
- `recent`
- `recent_events`
- `search_events`
- `get_event`
- `memory_events`
- `compact_context`
- `inspect`
- `maintenance`
- `backfill_signatures`
- `consolidate_full`
- `lookup_symbol`

`backfill_signatures` and `consolidate_full` are also available as top-level aliases. Alias lifecycle actions are available as `maintenance` sub-actions.

### Event history actions

```json
{"action":"recent_events","params":{"limit":20}}
```

```json
{"action":"search_events","params":{"query":"IBAN validation","limit":20}}
```

```json
{"action":"get_event","params":{"event_id":"evt_..."}}
```

```json
{"action":"memory_events","params":{"memory_id":"mem_...","limit":50}}
```

### `doctor`

Returns storage, schema, health, export, FTS, salience, and IDF diagnostics.

```json
{"action":"doctor"}
```

In SQLite mode, use `doctor` to verify `backend`, `sqlite_file_exists`, `sqlite_size_bytes`, `memory_count`, `newest_memory`, `fts`, signature warnings, and `idf` activation status.

### `record`

Records a project memory of any supported kind.

```json
{"action":"record","params":{"kind":"decision","text":"Run validation commands before handoff.","source":"team note","tags":["validation"],"pinned":true}}
```

Structured aliases are accepted by the generic record action:

```json
{"action":"record","params":{"kind":"interaction_log","summary":"Session handoff and active constraints.","role":"coordinator","agent_id":"coord_1"}}
```

```json
{"action":"record","params":{"kind":"context_block","body":"Expanded implementation context.","title":"Handoff block","linked_ids":["mem_log_id"]}}
```

```json
{"action":"record","params":{"kind":"hippocampus_entry","text":"Always run validation before release handoff.","evidence_ids":["mem_log_id"],"domain":"release"}}
```

```json
{"action":"record","params":{"kind":"agent_feedback","text":"Prefer middleware-first auth checks.","feedback_type":"good_pattern","agent_id":"spec_auth","domain":"auth"}}
```

### `search`

Searches project memory with bounded output.

```json
{"action":"search","params":{"query":"validation commands before handoff","limit":5,"phase":"implementation","max_tokens":2000}}
```

Search-like actions (`search`, `recall`, and `salience_check`) emit miss-aware query events:

- miss when `result_count == 0` or `top_score < MNEMO_MISS_TOP_SCORE_THRESHOLD`
- typed event fields include `result_count`, `success`, and `top_score`
- miss events are marked `include_in_salience=1` for downstream alias analysis

### `recall`

Returns startup or specialist recall bundles.

```json
{"action":"recall","params":{"mode":"startup","role":"coordinator","agent_id":"coord_1","query":"release handoff","recent_logs":20}}
```

```json
{"action":"recall","params":{"mode":"agent","agent_id":"spec_auth","role":"specialist","domain":"auth","task":"review auth middleware"}}
```

### `alias_hint`

Records explicit alias evidence linking failed wording to successful canonical wording.

```json
{"action":"alias_hint","params":{"domain":"agentic","canonical":"memory recall pipeline","candidate_alias":"hippocampus bridge","original_query":"hippocampus bridge","successful_query":"memory recall pipeline","confidence":"high","include_in_salience":true}}
```

### `get`

Retrieves one memory by id. Use this when a search/recall preview is not enough.

```json
{"action":"get","params":{"id":"mem_123","full":true}}
```

### `link`

Links two memory records.

```json
{"action":"link","params":{"source_id":"mem_log","target_id":"mem_block","relation":"expands","bidirectional":true}}
```

### `export`

Writes readable exports.

```json
{"action":"export","params":{"format":"jsonl"}}
```

```json
{"action":"export","params":{"format":"hippocampus_markdown"}}
```

Default outputs go under `state/mnemo/exports/`.

### `compact_context`

Builds a prompt-ready memory context block.

```json
{"action":"compact_context","params":{"query":"change the auth flow","limit":8,"phase":"implementation","max_tokens":2000}}
```

### `maintenance`

Maintenance sub-actions:

- `compact_logs`
- `consolidate`
- `consolidate_full`
- `import_json`
- `backfill_signatures`
- `propose_aliases`
- `list_alias_proposals`
- `approve_alias`
- `reject_alias_proposal`
- `list_aliases`
- `disable_alias`
- `disable_alias_concept`

Examples:

```json
{"action":"maintenance","params":{"action":"backfill_signatures","dry_run":true}}
```

```json
{"action":"maintenance","params":{"action":"consolidate","dry_run":true,"max_candidates_per_memory":100}}
```

```json
{"action":"maintenance","params":{"action":"consolidate_full","confirm_full_scan":true,"dry_run":true}}
```

```json
{"action":"maintenance","params":{"action":"propose_aliases","window_days":30,"min_recurrence":3,"include_hints":true,"dry_run":true}}
```

The default `consolidate` action is candidate-based. The O(n²) full scan is only available through `consolidate_full` with `confirm_full_scan:true`.

`propose_aliases` supports dry run and persistence:

- `dry_run=true`: returns proposals but does not persist
- `dry_run=false`: persists pending proposals in SQLite (`alias_proposals`, `alias_proposal_events`)

```json
{"action":"maintenance","params":{"action":"list_alias_proposals","status":"pending","domain":"agentic","limit":50}}
```

```json
{"action":"maintenance","params":{"action":"approve_alias","proposal_id":"alias-prop-...","approved_by":"coordinator"}}
```

```json
{"action":"maintenance","params":{"action":"reject_alias_proposal","proposal_id":"alias-prop-...","reason":"generic wording"}}
```

### Top-level maintenance aliases

For discoverability, these can also be called directly:

```json
{"action":"backfill_signatures","params":{"dry_run":false}}
```

```json
{"action":"consolidate_full","params":{"confirm_full_scan":true,"dry_run":true}}
```

### `inspect`

Inspects history and related memories.

```json
{"action":"inspect","params":{"id":"mem_123","mode":"both","limit":50,"depth":2,"include_archive":true}}
```

### `lookup_symbol`

Finds likely source definition locations under `MNEMO_WORKSPACE_ROOT`.

```json
{"action":"lookup_symbol","params":{"name":"authenticate","limit":10}}
```

### `salience_check`

Optional deterministic salience diagnostics when Agent Salience is available. When local IDF profiles are active, `salience_check` automatically uses IDF-dominant scoring.

```json
{"action":"salience_check","params":{"text":"auth middleware decisions","limit":5,"candidate_limit":500,"max_scored":100}}
```

`salience_check` is candidate-limited. In SQLite mode it uses FTS when available, then signature overlap, and scores only bounded survivors. Candidate generation is unchanged in this patch.

When IDF is active, Mnemo uses IDF-dominant weights:

- `idf_cosine: 0.55`
- `idf_jaccard: 0.35`
- `cosine: 0.05`
- `jaccard: 0.05`

The added `idf_jaccard` (weighted Jaccard/Tanimoto using corpus IDF weights) suppresses common-word overlap false positives. When IDF is cold/off/unavailable, lexical scoring remains active.

## Local storage

Mnemo uses SQLite by default.

```text
state/mnemo/
├── mnemo.sqlite              # primary store in SQLite mode
├── memory.json               # legacy/import/export compatibility path
└── exports/
    ├── memory.jsonl
    ├── hippocampus.md
    ├── agent_feedback.md
    └── startup_context_latest.md
```

`memory.json` is not the primary store when `MNEMO_STORE=sqlite`; it is retained as an import/export compatibility format.

## Signature-at-write-time

Mnemo stores deterministic signatures when memories are recorded, imported, or backfilled:

- `content_hash`: blake2b digest of full raw text after stable line-ending normalization
- `normalized_hash`: blake2b digest of normalized tokens joined with spaces
- `token_count`
- `unique_token_count`
- `top_terms_json`: top 32 terms by frequency, ties alphabetically
- `shingle_hashes_json`: sorted min-K word-shingle hashes, capped at 256
- `signature_version`
- `normalizer_version`
- `signature_updated_at`

`content_hash` covers the full text. Token-based signatures are capped at `MAX_SIGNATURE_TEXT_CHARS = 50,000` characters to avoid runaway tokenization on pasted logs or huge dumps. Raw stored text is unaffected.

Similarity policy:

```text
jaccard([], []) = 0.0
```

Empty-empty means no signal, not semantic identity. Tiny texts may match exact hashes but are skipped by shingle-based near-duplicate detection.

## Memory growth and token cost

The store can grow locally without automatically increasing token usage. Search and recall return bounded previews. Full bodies are loaded by id through `action="get"`.

## Structured memory layers

Mnemo uses neutral, reusable memory kinds:

- `interaction_log`: short continuity notes from recent work
- `context_block`: larger linked memory artifacts
- `hippocampus_entry`: durable project/system knowledge
- `agent_feedback`: feedback scoped to an `agent_id`, `role`, or `domain`

Mnemo does not hardcode personal agent names. Use metadata fields such as `agent_id`, `role`, `scope`, `domain`, `authority`, `retention`, `confidence`, `linked_ids`, `parent_id`, and `source_run_id`.

## Environment variables

| Variable | Description |
|---|---|
| `MNEMO_STORE` | Storage backend: `sqlite` or `json`. Default: `sqlite`. |
| `MNEMO_FILE` | Compatibility/import/export path for `memory.json`. Default: `<workspace>/state/mnemo/memory.json`. |
| `MNEMO_SQLITE_FILE` | SQLite path when `MNEMO_STORE=sqlite`. Default: `<workspace>/state/mnemo/mnemo.sqlite`. |
| `MNEMO_WORKSPACE_ROOT` | Workspace root for `lookup_symbol`. Default: current working directory. |
| `MNEMO_MAX_MEMORIES` | Total memory cap including retired entries. Default: `5000`. |
| `MNEMO_MAX_SEARCH_RESULTS` | Server-side cap for `search` results. Default: `20`. |
| `MNEMO_MAX_RECENT_RESULTS` | Server-side cap for `recent` results. Default: `50`. |
| `MNEMO_MAX_CHARS_PER_ITEM` | Per-item preview cap for search/recall/get preview mode. Default: `1200`. |
| `MNEMO_MAX_TOTAL_CHARS` | Total preview cap for bundled search/recall output. Default: `12000`. |
| `MNEMO_DECAY` | Set to `0` to disable time-decay scoring. Default: `1`. |
| `MNEMO_LOG_EVENTS` | In JSON mode, set to `0` to disable `events.jsonl`. In SQLite mode, lifecycle events are stored in SQLite. Default: `1`. |
| `MNEMO_LOG_QUERIES` | In JSON mode, set to `0` to disable `queries.jsonl`. In SQLite mode, query events are stored in SQLite. Default: `1`. |
| `MNEMO_LOG_ARCHIVE` | Set to `0` to disable permanent archiving of rotated query/event logs. Default: `1`. |
| `MNEMO_CONSOLIDATE_THRESHOLD` | Near-duplicate consolidation threshold. Default: `0.7`. |
| `MNEMO_MISS_TOP_SCORE_THRESHOLD` | Miss threshold used by search-like event tagging. Default: `0.15`. |
| `MNEMO_SYMBOL_TTL_SECONDS` | Symbol-index walk TTL. Default: `5`. |
| `MNEMO_MAX_FILES_SCANNED` | Max files scanned by `lookup_symbol`. Default: `5000`. |
| `MNEMO_MAX_TOTAL_BYTES` | Max total bytes scanned by `lookup_symbol`. Default: `52428800`. |
| `MNEMO_MAX_FILE_BYTES` | Max single file bytes read by `lookup_symbol`. Default: `1048576`. |
| `MNEMO_IDF_MODE` | `auto`, `off`, or `force`. Default: `auto`. |
| `MNEMO_IDF_MIN_DOCUMENTS` | Project IDF minimum documents. Default: `200`. |
| `MNEMO_IDF_MIN_UNIQUE_TERMS` | Project IDF minimum unique terms. Default: `1000`. |
| `MNEMO_IDF_MIN_TOTAL_TOKENS` | Project IDF minimum total tokens. Default: `10000`. |
| `MNEMO_IDF_DOMAIN_MIN_DOCUMENTS` | Domain IDF minimum documents. Default: `50`. |
| `MNEMO_IDF_DOMAIN_MIN_UNIQUE_TERMS` | Domain IDF minimum unique terms. Default: `300`. |
| `MNEMO_IDF_DOMAIN_MIN_TOTAL_TOKENS` | Domain IDF minimum total tokens. Default: `3000`. |
| `MNEMO_IDF_MIN_TEXT_TOKENS` | Minimum tokens per memory included in IDF corpus. Default: `5`. |
| `AGENT_SALIENCE_HOME` | Optional path to local `agent-salience` checkout. |

`MNEMO_MCP_PROFILE` is ignored. Mnemo always exposes one public gateway tool.

## Salience / IDF / LSH

Mnemo stores deterministic signature fields (`signature_version`, `normalizer_version`, `shingle_hashes_json`, hashes, token counts, and top terms) and automatically builds local project/domain IDF profiles when corpus maturity thresholds are met.

Defaults:

- `MNEMO_IDF_MODE=auto`
- `MNEMO_IDF_MIN_DOCUMENTS=200`
- `MNEMO_IDF_MIN_UNIQUE_TERMS=1000`
- `MNEMO_IDF_MIN_TOTAL_TOKENS=10000`
- `MNEMO_IDF_DOMAIN_MIN_DOCUMENTS=50`
- `MNEMO_IDF_DOMAIN_MIN_UNIQUE_TERMS=300`
- `MNEMO_IDF_DOMAIN_MIN_TOTAL_TOKENS=3000`
- `MNEMO_IDF_MIN_TEXT_TOKENS=5`

Modes:

- `auto`: activate only when thresholds are met
- `off`: do not build/use IDF
- `force`: activate below thresholds (dev/test only)

`doctor` now returns an `idf` object with project/domain status (`cold|ready|disabled|unavailable`), activation flags, corpus counts, and remaining maturity gaps.

All activation thresholds are AND-gated:

- documents threshold must pass
- unique-terms threshold must pass
- total-token threshold must pass

IDF is a scoring aid and does not replace signatures, lexical ranking, FTS, or baseline cosine/Jaccard primitives. It now contributes through both `idf_cosine` and `idf_jaccard`.

Full LSH/MinHash buckets are still not implemented in this release. See [`docs/salience_lsh_idf_notes.md`](docs/salience_lsh_idf_notes.md).

## Alias runtime boundary

Aliases are dynamic project/domain retrieval knowledge stored in Mnemo SQLite.

- Mnemo passively logs miss events and alias hints.
- `maintenance(action="propose_aliases")` proposes candidates and can persist pending proposals.
- Curation uses `list_alias_proposals`, `approve_alias`, and `reject_alias_proposal`.
- Runtime query paths (`search`, `recall`, `salience_check`, `compact_context`) consume active aliases automatically.
- SQLite views for inspection:
  - `v_alias_vocabulary`
  - `v_alias_pending_proposals`
  - `v_alias_concept_counts`

See [`docs/alias_proposals.md`](docs/alias_proposals.md) for scoring and curation workflow details.

## Copilot compatibility

Mnemo exposes a single public MCP gateway tool to avoid tool-inventory pressure and confusion with native assistant memory tools.

The exported schema avoids unsupported JSON Schema features. Defaults, bounds, and validation are enforced in Python handlers.

## Development

```bash
python -m compileall .
python smoke_test.py
python -m unittest discover -s . -p "test*.py"
python benchmark_consolidation.py
```

Expected result for this release: smoke test and unit suite pass locally.

The benchmark inserts 50,000 synthetic memories, backfills signatures, runs candidate-based consolidation, and verifies the `consolidate_full` confirmation gate.

## Privacy

Mnemo stores local project memory. Do not commit local state unless you intentionally want to share seed memory. See [`docs/storage.md`](docs/storage.md) and [`docs/tool_reference.md`](docs/tool_reference.md).

## License

MIT. See `LICENSE` when present in the packaged repository.
