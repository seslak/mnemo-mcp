# Mnemo Storage

Mnemo is local-first and SQLite-first.

Default SQLite path:

```text
state/mnemo/mnemo.sqlite
```

`memory.json` remains a compatibility/import/export format. In SQLite mode it is not the primary store.

## SQLite tables

Primary tables:

- `memories`
- `links`
- `events`
- `idf_profiles`
- `alias_concepts`
- `alias_terms`
- `alias_proposals`
- `alias_proposal_events`
- `memory_topics`
- `imported_packs`
- `imported_pack_rows`
- `exported_packs`
- `promoted_pack_rows`
- `promotion_audit`
- `trusted_signers`
- `meta`
- optional FTS5 table `memories_fts`
- optional FTS5 table `events_fts`

Lifecycle and query events are stored in SQLite in SQLite mode.

## Alias tables and views (0.13.4)

Alias knowledge is stored as dynamic SQLite state:

- `alias_concepts`: canonical concept rows with scope/status/weight
- `alias_terms`: active and disabled alias terms per concept
- `alias_proposals`: pending/approved/rejected proposal lifecycle rows
- `alias_proposal_events`: proposal-to-evidence event links

Inspection views:

- `v_alias_vocabulary`
- `v_alias_pending_proposals`
- `v_alias_concept_counts`

Alias lifecycle is curated through maintenance actions (`propose_aliases`, `list_alias_proposals`, `approve_alias`, `reject_alias_proposal`, `list_aliases`, `disable_alias`, `disable_alias_concept`) instead of editing repository files.

Runtime group discovery can reuse active alias runtime state to compute `group_type="alias"` groups. Pending proposals do not create groups, and disabled alias terms/concepts are excluded.

## Git-aware memory metadata (0.13.5)

Mnemo stores git context on new memory writes in SQLite mode by adding columns on `memories`:

- `git_sha` (`TEXT`)
- `git_branch` (`TEXT`)
- `git_dirty` (`INTEGER`, `0`/`1`)

Mnemo also stores touched-file fingerprints in:

- `memory_files(memory_table, memory_id, path, file_sha)`
- index: `idx_memory_files_path`

`memory_table` stores the row kind (`interaction_log`, `context_block`, `hippocampus_entry`, `agent_feedback`, etc.). `memory_id` stores the Mnemo memory id.

`file_sha` resolution order:

1. git blob SHA at `HEAD:path` when available
2. current working-tree file hash (`git hash-object`)
3. BLAKE2b-128 bytes digest fallback when git hashing is unavailable

If no digest can be resolved for a path, the row is skipped without failing the write.

Freshness-aware retrieval multiplies base score by:

- `1.0` when `git_sha IS NULL` (legacy rows stay neutral)
- `1.0` when all touched files are unchanged
- `0.7` when any touched file changed but still exists
- `0.3` when any touched file is missing/deleted/renamed away
- for mixed states across files, the minimum multiplier is used

Git-unavailable and non-git directories are treated as safe neutral paths (`1.0`), never hard failures.

## Memory Packs Phase 1 substrate (0.14.0)

Phase 1 adds pack-oriented schema substrate while keeping retrieval defaults local-only.

### Added `memories` columns

- `namespace TEXT NOT NULL DEFAULT 'local'`
- `origin TEXT NOT NULL DEFAULT 'local'`
- `import_freshness TEXT` (nullable placeholder for later import freshness summaries)

Existing rows are not rewritten manually; SQLite defaults apply during migration:

- `namespace='local'`
- `origin='local'`
- `import_freshness=NULL`

### Added topic metadata table

- `memory_topics(memory_id, topic, created_at, source)`
- `PRIMARY KEY(memory_id, topic)`
- indexes:
  - `idx_memory_topics_topic`
  - `idx_memory_topics_memory_id`

Topics are relational metadata rows. Topic CRUD/listing uses SQL joins, not FTS body text.

### Added placeholder pack registries

- `imported_packs`
  - trust levels: `trusted` or `quarantine`
  - stores namespace mapping and manifest payloads
- `exported_packs`
  - stores export ledger metadata only

Phase 1 intentionally does not implement pack wire-format import/export, redaction, signing, promotion, or trust-store policy.

### Namespace/origin retrieval filtering

Retrieval defaults:

- namespace scope defaults to `['local']`
- trusted imported namespaces are opt-in (`include_imported=true`)
- quarantine namespaces are separate opt-in (`include_quarantine=true`)
- origin is metadata by default (no origin filter unless explicitly requested)

Origin filter rules:

- explicit `origin` or `origins` applies origin restriction
- no explicit origin filter means all origins inside the namespace scope are eligible

Pack id derivation in retrieval metadata:

- `pack:trusted:<pack_id>` -> `<pack_id>`
- `pack:quarantine:<pack_id>` -> `<pack_id>`
- `pack:<pack_id>` -> `<pack_id>`
- other namespaces -> `pack_id=null`

## Memory Packs Phase 2a preview engine (0.15.0)

Phase 2a adds a read-only selection preview action: `pack_preview`.

Selection semantics:

- topic filtering joins `memory_topics` (no body-text/FTS topic inference)
- exact `memory_ids` selectors are supported for pack preview/export workflows
- touched path filtering joins `memory_files`
- namespace/origin filtering reuses Phase 1 scope semantics
- default namespace scope remains `['local']`
- trusted imported namespaces require `include_imported=true`
- quarantine namespaces require `include_quarantine=true`

Output includes bounded, deterministic summaries:

- selected row count and capped row ID list
- counts by kind/namespace/origin
- top 20 topic counts inside the selected set
- top referenced files from `memory_files`
- bounded sample previews (200-char compact text snippets)

Phase 2a alias output is intentionally a placeholder:

- `referenced_alias_count = 0`
- `top_alias_concepts = []`

Read-only guarantees:

- no writes to `exported_packs`
- no pack file writes
- no zip export/import, redaction, signing, trust, or promotion logic

## Memory Packs Phase 2b redaction dry-run (0.16.0)

Phase 2b adds a read-only dry-run action: `pack_redaction_preview`.

Behavior:

- reuses the same row selection semantics as `pack_preview`
- scans selected row text fields using a baseline built-in redaction ruleset
- reports bounded deterministic counts and sample previews
- never writes redacted memory back to SQLite
- never writes pack artifacts

Baseline categories in this phase:

- `private_key_header`
- `jwt`
- `aws_access_key`
- `email`
- `user_path`
- `ipv4`

Known scope limit:

- IPv6 and many provider-specific token formats are intentionally out of scope in the baseline-v1 ruleset.

## Memory Packs Phase 2c export artifact (0.17.0, `.mem` suffix in 0.21.5)

Phase 2c adds `pack_export`, which writes a local development pack artifact and records one export audit row in `exported_packs`.

Export policy and scope:

- requires `allow_unsigned=true` (signing is not implemented yet)
- exact `memory_ids` selectors resolve only the requested existing rows; unknown IDs do not broaden export scope
- exportable kinds are strict in this phase:
  - `context_block`
  - `hippocampus_entry`
- `interaction_log` and `agent_feedback` remain previewable but are rejected for export
- no import/signing/trust/promotion workflows are implemented

Output location:

- default: `state/mnemo/packs/exports/`
- default landing/import inbox: `state/mnemo/packs/inbox/`
- optional `output_dir` is supported, with sanitized filename handling
- landing-folder override: `MNEMO_PACK_LANDING_DIR`
- public artifact suffix: `.mem`
- internal container format: ZIP
- legacy `.zip` packs remain inspect/import compatible for backward compatibility

Pack safety and identity:

- source DB memory IDs are not exported
- exported row IDs are pack-local (`ctx_###`, `hip_###`)
- export runs mandatory baseline-v1 redaction on all exported text fields (`text`, `title`)
- baseline-v1 is intentionally incomplete and not a full DLP system

Manifest/content hash:

- `manifest.json` stores a SHA-256 `content_hash`
- hash covers:
  - `content/file_fingerprints.json`
  - `content/memories.jsonl`
  - `content/topics.json`
  - `provenance/origin.json`
  - `provenance/redactions.json`
- `manifest.json` itself is not included in the Phase 2c content hash coverage

`content/file_fingerprints.json`:

- records touched-file paths referenced by exported memories
- records file hashes used for provenance/freshness checks
- does not embed file contents
- may legitimately contain synthetic UX-lab paths such as `state/mnemo/synthetic_files/...` when exported memories reference synthetic touched files

Audit row:

- successful export inserts one row into `exported_packs`:
  - `pack_id`
  - `pack_name`
  - `exported_at`
  - `row_count`
  - `redaction_count`
  - `signed=0`
  - `manifest_json`

## Memory Packs Phase 3a inspect/validate pack artifacts (0.18.0)

Phase 3a adds `pack_inspect`, a read-only validator for exported pack artifacts.

Read-only guarantees:

- no writes to `memories`, `memory_topics`, `memory_files`, `imported_packs`, or `exported_packs`
- no ZIP extraction to disk
- no import/signing/trust/promotion behavior

Suffix behavior:

- `.mem` is the preferred public suffix
- legacy `.zip` is still accepted with compatibility warning
- other suffixes may be inspected if they open as valid ZIPs, but they are warned as nonstandard

Validation focus:

- required member presence:
  - `manifest.json`
  - `content/memories.jsonl`
  - `content/topics.json`
  - `content/file_fingerprints.json`
  - `provenance/origin.json`
  - `provenance/redactions.json`
- ZIP safety limits and member-path safety checks
- schema support (`pack_schema_version=1` in Phase 3a)
- manifest timestamp/shape checks
- row-level shape checks for `content/memories.jsonl`
- redaction metadata consistency between `manifest.json` and `provenance/redactions.json`

Canonical content hash verification:

- inspector recomputes SHA-256 over the canonical covered-member set:
  - `content/file_fingerprints.json`
  - `content/memories.jsonl`
  - `content/topics.json`
  - `provenance/origin.json`
  - `provenance/redactions.json`
- inspector rejects packs when `manifest.content_hash.covered_members` differs from that canonical set (`covered_members_mismatch`)

Leakage guard:

- any source-like DB memory ID pattern (`mem_*`) found in required JSON payloads invalidates the pack

Status and recommendation:

- status: `valid`, `invalid`, or `unsupported`
- recommendation: `quarantine_only` or `reject`
- valid unsigned packs are classified `quarantine_only` in this phase
- one bad row invalidates the whole pack (no partial acceptance policy in Phase 3a)

## Memory Packs Phase 3b+5b import policy (0.18.5 + 0.21.0)

Phase 3b introduced validated import; Phase 5b adds trusted import targeting without bypassing local adoption.

Import policy:

- exactly one target gate must be used:
  - trusted import: `allow_trusted_import=true` with `verification_secret`
  - quarantine import: `allow_unsigned_quarantine=true`
- imports only packs that pass shared `pack_inspect` validation gates
- trusted imports require verified `trusted_signer` classification
- quarantine imports use:
  - `namespace = pack:quarantine:<pack_id>`
  - `origin = imported`
  - `trust_level = quarantine` in `imported_packs`
- trusted imports use:
  - `namespace = pack:trusted:<pack_id>`
  - `origin = imported`
  - `trust_level = trusted` in `imported_packs`
- default retrieval still excludes these rows unless quarantine scope is explicitly enabled
- trusted imported rows are excluded from default retrieval and require `include_imported=true` (or explicit namespace)

Schema additions/usage:

- `imported_packs.received_zip_sha256`:
  - SHA-256 over the exact imported ZIP bytes
  - used to distinguish exact re-import vs same `pack_id` with distinct content
- `imported_pack_rows`:
  - maps `(pack_id, row_id_in_pack)` to newly created local `memory_id`
  - supports audit/debug and later governance workflows

Imported row provenance:

- imported rows always receive new local `mem_*` IDs
- source DB IDs are not imported
- source git context from pack rows is preserved on imported memories:
  - `git_sha_at_write -> memories.git_sha`
  - `git_branch_at_write -> memories.git_branch`
  - `git_dirty_at_write -> memories.git_dirty`
- imported topics are inserted into `memory_topics` with `source=pack_import`

`memory_files` semantics for imported rows:

- `memory_table` uses the imported memory kind (`context_block` or `hippocampus_entry`)
- `memory_id` uses the new local imported `mem_*` ID
- `file_sha` is the source/export fingerprint from the pack, not necessarily local file state at import time

Import freshness labels:

- per-memory `import_freshness` is diagnostic:
  - `verified`, `stale`, `missing`, `unknown`
- labels are computed using existing git-aware SHA helpers against local files when possible
- aggregate freshness counts are stored in `imported_packs.freshness_summary_json`

Re-import behavior:

- same `pack_id` + same `received_zip_sha256` => rejected (`pack_already_imported`)
- same `pack_id` + different ZIP bytes => rejected (`pack_id_collision_distinct_content`)
- legacy prior-import rows with missing stored hash are rejected as unknown-hash re-import (`pack_already_imported_legacy_unknown_hash`)

## Memory Packs Phase 4a+5b imported-row review + promotion preview (0.19.0 + 0.21.0)

Phase 4a adds read-only operator tooling over imported SQLite rows (quarantine and trusted imports):

- `pack_list_imports`
- `pack_review_import`
- `pack_promote_preview`

Data sources used for review/preview:

- `imported_packs`
- `imported_pack_rows`
- `memories`
- `memory_topics`
- `memory_files`

No ZIP reads are required for these actions. They review already-imported DB state.

Read-only guarantees:

- no writes to `memories`, `imported_packs`, `imported_pack_rows`, `memory_topics`, `memory_files`, `exported_packs`, alias tables, or FTS shadow tables
- no filesystem writes
- no promotion mutation

Review/promotion preview semantics:

- promotion preview target is fixed to:
  - `namespace = local`
  - `origin = promoted`
- preview does not allocate real promoted memory IDs
- quarantine rows remain unchanged

Operator-facing metadata notes:

- `source_label` is rendered as basename-only in review/list outputs
- `received_zip_sha256` is intentionally exposed for cross-machine imported-pack byte comparison
- imported row `memory_files.file_sha` remains the source/export fingerprint captured in the pack (not a local post-import recomputation)
- imported row git provenance (`git_sha`, `git_branch`, `git_dirty`) remains the source context imported from pack rows

Query filter behavior in pack review:

- `query` is simple case-insensitive substring matching over imported `text` and `title` columns only
- no FTS, no ranking, no tokenization

## Memory Packs Phase 4b manual promotion (0.19.5)

Phase 4b adds manual SQLite-to-SQLite promotion from quarantine imports into local memory:

- `pack_promote`

Promotion source/target:

- source rows are imported rows from:
  - `namespace = pack:quarantine:<pack_id>` or
  - `namespace = pack:trusted:<pack_id>`
  - `origin = imported`
- target rows are new local rows:
  - `namespace = local`
  - `origin = promoted`

Promotion is transactional and preserves quarantine rows unchanged.

### Added tables

- `promoted_pack_rows`
  - maps `(pack_id, row_id_in_pack)` to:
    - `imported_memory_id`
    - `promoted_memory_id`
  - stores:
    - `kind`
    - `promoted_at`
    - `original_import_freshness`
    - `promotion_id`
- `promotion_audit`
  - one row per successful `pack_promote` call
  - stores:
    - `promotion_id`
    - `pack_id`
    - `promoted_at`
    - canonical `filters_json`
    - `row_count`
    - limited/allow flags

### Copied provenance/content

Each promoted row copies from the imported row:

- kind
- text/title fields (already redacted from import flow)
- `git_sha`, `git_branch`, `git_dirty`
- `import_freshness`
- topics into `memory_topics` with `source=promotion`
- memory-file links into `memory_files` with:
  - `memory_table = promoted memory kind`
  - `memory_id = new promoted mem_*`
  - `file_sha = source/export SHA copied from imported row`

Topic rows for promoted and imported memories intentionally coexist. One topic can appear once for the quarantine imported memory and once for the promoted local memory because these are distinct memory rows.

`memory_files.file_sha` for promoted rows remains the imported source/export fingerprint; it is not recomputed at promotion time.

`import_freshness` on promoted rows reflects import-time diagnostics and is not recomputed during promotion in this phase.

### Behavior constraints

- `confirm_promote=true` is required
- explicit row filters are required unless `allow_promote_all=true`
- limited selection requires `allow_limited_promotion=true`
- duplicate promotion of a selected `(pack_id, row_id_in_pack)` is rejected (`pack_rows_already_promoted`)
- no `skip_already_promoted` or `allow_repromote` mode in 0.20.1
- no automatic trusted promotion/signing/trust-store/alias promotion behavior in this phase

## Memory Packs stabilization pass (0.19.6)

`0.19.6` is a stabilization release and does not add a new lifecycle phase.

Stabilization scope:

- full lifecycle regression coverage for:
  - record/topic tagging
  - `pack_preview`
  - `pack_redaction_preview`
  - `pack_export`
  - `pack_inspect`
  - `pack_import` (quarantine)
  - `pack_list_imports`
  - `pack_review_import`
  - `pack_promote_preview`
  - `pack_promote`
- migration/idempotency regression checks across legacy/current schema shapes
- read-only action contract regression checks
- export artifact safety regression checks
- retrieval boundary regression checks for local vs quarantine vs promoted visibility
- sync-parity validation support across `agentic/tools/mcp/mnemo`, `mnemo`, and `pub_mnemo` in development environments where sibling copies exist

Behavior remains unchanged:

- signing/trust policy is still not implemented
- alias pack import/export is still not implemented
- baseline redaction ruleset remains `baseline-v1` and is intentionally not full DLP
- promoted rows keep import-time `import_freshness` provenance metadata; freshness is not recomputed at promotion time

## Memory Packs signing/trust foundation (0.20.0)

`0.20.0` adds the first signing/trust layer with stdlib-only local HMAC signing.

### Signer registry

New table:

- `trusted_signers`
  - `signer_id` (PK)
  - `label`
  - `trust_level` (`trusted` or `blocked`)
  - `signature_algorithm` (Phase 5a: `hmac-sha256-local-v1`)
  - `secret_fingerprint` (no raw secret storage)
  - `public_key` (`NULL` in local-HMAC mode)
  - `created_at`, `updated_at`
  - `status` (`active` or `disabled`)
  - `notes`

Secret fingerprint recipe:

- `sha256(secret_utf8).hexdigest()[:32]`

### Pack signing fields

Signed exports may include:

- `manifest.signature` metadata:
  - `signature_algorithm`
  - `signature_payload_version`
  - `signer_id`
  - `secret_fingerprint`
  - `signature_member = signature/signature.json`
- `signature/signature.json` member with:
  - `signature_schema_version`
  - `signature_algorithm`
  - `signature_payload_version`
  - `signer_id`
  - `secret_fingerprint`
  - `signed_at`
  - `signature_value`

### Crypto scope and limits

- stdlib mode is local HMAC signing (`hmac-sha256-local-v1`)
- this is not public-key signing and not non-repudiation
- no persistent secret store in this phase
- no key revocation or remote key discovery in this phase
- trusted import exists but requires verified trusted signer + explicit operator consent

## Trusted Import Policy

Trusted import in `0.21.1` is a storage/trust classification change, not local adoption.

- trusted imports are written to `namespace = pack:trusted:<pack_id>`
- imported row `origin` remains `imported`
- trusted imports do not auto-create `namespace=local` rows
- manual promotion remains explicit (`pack_promote`)

Retrieval boundary rules:

- default retrieval remains local-only
- `include_imported=true` adds trusted imported namespaces
- `include_quarantine=true` adds quarantine namespaces
- both flags include both imported trust classes
- explicit namespace filters override include flags

Trusted gate requirements:

- signature verified
- trusted signer classification (`trusted_signer`)
- active trusted signer metadata and fingerprint match
- explicit operator permission (`allow_trusted_import=true`)

Fallback policy:

- unsigned/unverified/unknown/invalid/blocked/disabled/mismatch/unsupported signature packs cannot trusted-import
- quarantine import remains available (`allow_unsigned_quarantine=true`)

## Memory Packs lifecycle recap

Current lifecycle through `0.21.1`:

1. `record` + `topic_add`
2. `pack_preview`
3. `pack_redaction_preview`
4. `pack_export` (unsigned or signed local-HMAC mode)
5. `pack_inspect` (ZIP/hash validation + signature classification/verification + trusted-import availability signal)
6. `pack_import`:
   - trusted target: `pack:trusted:<pack_id>`
   - quarantine target: `pack:quarantine:<pack_id>`
7. `pack_list_imports` + `pack_review_import`
8. `pack_promote_preview`
9. `pack_promote` (manual local `origin=promoted` copy)

Still out of scope:

- public-key signing
- persistent secret store
- key revocation
- remote key discovery
- trusted auto-promotion
- alias pack import/export

## Memory Packs 0.21.1 stabilization

`0.21.1` is a stabilization pass, not a new feature phase.

Stabilization checks now cover:

- trusted/quarantine retrieval boundaries and include-flag semantics
- trusted import lifecycle, signed quarantine fallback lifecycle, and unsigned lifecycle
- cross-trust reimport/collision policy
- trusted-import tamper rejection before mutation
- verification-secret scrubbing across import/log/report surfaces
- trusted-source promotion provenance and import-freshness preservation

## Memory Packs v1 status

Complete for practical local export/import workflows when synthetic readiness reports `memory_packs_v1_status=ready`.

Future optional extensions:

- public-key signing
- persistent secret store
- revocation/discovery workflows
- alias-pack flows
- skip-already-promoted ergonomics

## Event typed columns (0.13.3)

The `events` table includes typed columns for event-history APIs:

- `event_id`, `ts`, `action`, `memory_id`
- `source_id`, `target_id`, `relation`
- `query_text`, `result_count`, `top_score`, `success`
- `agent_id`, `role`, `domain`, `kind`
- `summary`, `salience_text`, `include_in_salience`
- `data_json` (raw payload)

Migration is idempotent: older SQLite files with only legacy event columns are upgraded automatically.

Legacy rows keep `top_score=NULL` unless a numeric `top_score` (or fallback `score`) can be safely read from `data_json`.

Search-like actions (`mnemo_search`, `mnemo_recall`, `mnemo_salience_check`) emit miss-aware query events, and `alias_hint` events are stored in the same `events` table for alias proposal workflows.

## IDF profile table (0.13.1)

Mnemo persists project/domain IDF profile state in `idf_profiles`:

- `scope` (`project` or `domain`)
- `name` (`default` for project scope, domain name for domain scope)
- `profile_version`
- `status` (`cold`, `ready`, `disabled`, `unavailable`)
- `active`
- `doc_count`, `unique_terms`, `total_tokens`
- threshold columns (`min_documents`, `min_unique_terms`, `min_total_tokens`)
- `corpus_signature`
- `profile_json` (serialized Agent Salience IDF payload)
- `updated_at`

Rows are keyed by `(scope, name, profile_version)` and refreshed only when corpus signatures change.

When profile status is `ready`, Mnemo scoring paths can use:

- `idf_cosine` (IDF-weighted cosine)
- `idf_jaccard` (IDF-weighted Jaccard/Tanimoto)

This patch does not alter candidate generation; it changes scoring composition only.

## Signature columns

Mnemo adds deterministic signature columns to `memories`:

| Column | Meaning |
|---|---|
| `content_hash` | blake2b digest of full raw text after CRLF/CR to LF normalization and trailing whitespace trimming per line. |
| `normalized_hash` | blake2b digest of normalized tokens joined with spaces. |
| `token_count` | Number of normalized tokens. |
| `unique_token_count` | Number of unique normalized tokens. |
| `top_terms_json` | JSON list of top 32 terms by frequency, ties alphabetically. |
| `shingle_hashes_json` | JSON list of sorted min-K word-shingle hashes, capped at 256. |
| `signature_version` | Signature format version. Current: 1. |
| `normalizer_version` | Normalizer version. Current: 1. |
| `signature_updated_at` | UTC timestamp when signatures were generated. |

`content_hash` covers the full raw text. Token-based signatures use only the first `MAX_SIGNATURE_TEXT_CHARS = 50,000` characters for safety. The raw stored `text` column is unaffected.

## Migration and backfill

SQLite schema migration is idempotent. Existing v0.11.x stores load under modern releases. Rows without signatures remain usable and can be backfilled.

Git-aware memory metadata is not backfilled. Existing rows keep `git_sha=NULL` and remain retrieval-neutral.

Dry run:

```json
{"action":"maintenance","params":{"action":"backfill_signatures","dry_run":true}}
```

Apply:

```json
{"action":"maintenance","params":{"action":"backfill_signatures","dry_run":false}}
```

The same operation is available as the top-level alias:

```json
{"action":"backfill_signatures","params":{"dry_run":false}}
```

`doctor` warns when more than 10% of active memories have missing or outdated signatures.

## FTS5

FTS5 is used when available for search and candidate selection paths that benefit from it. If FTS5 is unavailable, Mnemo falls back to bounded lexical/signature candidate selection. `doctor` reports FTS availability and candidate source.

## Consolidation

Default consolidation is candidate-based:

```json
{"action":"maintenance","params":{"action":"consolidate","dry_run":true}}
```

It checks exact hashes first, then bounded candidates, shingle overlap, and only then full similarity.

The old O(n²) scan is intentionally moved behind `consolidate_full`:

```json
{"action":"maintenance","params":{"action":"consolidate_full","confirm_full_scan":true,"dry_run":true}}
```

Without `confirm_full_scan:true`, it returns a structured error with estimated pair count.

## Runtime artifacts

Do not commit runtime state unless intentionally publishing seed memory:

- `state/mnemo/`
- `mnemo.sqlite`
- `*.sqlite-shm`
- `*.sqlite-wal`
- `memory.json`
- `events.jsonl`
- `queries.jsonl`
- `*.archive.jsonl`
- `*.lock`
- generated exports
