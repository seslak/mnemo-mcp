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
- `exported_packs`
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

- `pack:quarantine:<pack_id>` -> `<pack_id>`
- `pack:<pack_id>` -> `<pack_id>`
- other namespaces -> `pack_id=null`

## Memory Packs Phase 2a preview engine (0.15.0)

Phase 2a adds a read-only selection preview action: `pack_preview`.

Selection semantics:

- topic filtering joins `memory_topics` (no body-text/FTS topic inference)
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

## Memory Packs Phase 2c export ZIP (0.17.0)

Phase 2c adds `pack_export`, which writes an unsigned local development pack ZIP and records one export audit row in `exported_packs`.

Export policy and scope:

- requires `allow_unsigned=true` (signing is not implemented yet)
- exportable kinds are strict in this phase:
  - `context_block`
  - `hippocampus_entry`
- `interaction_log` and `agent_feedback` remain previewable but are rejected for export
- no import/signing/trust/promotion workflows are implemented

Output location:

- default: `state/mnemo/packs/exports/`
- optional `output_dir` is supported, with sanitized filename handling

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

Audit row:

- successful export inserts one row into `exported_packs`:
  - `pack_id`
  - `pack_name`
  - `exported_at`
  - `row_count`
  - `redaction_count`
  - `signed=0`
  - `manifest_json`

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
