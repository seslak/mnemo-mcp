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

## Git-aware memory schema (0.13.5)

Mnemo 0.13.5 adds git-aware metadata to the SQLite `memories` table. The migration is additive and safe to re-run.

New columns on `memories`:

| Column | Meaning |
|---|---|
| `git_sha` | Commit SHA at write time, when Mnemo can read git state. |
| `git_branch` | Branch name at write time, when available. |
| `git_dirty` | `1` if the working tree was dirty, `0` if clean, `NULL` if git context is unavailable. |

Touched-file fingerprints are stored in `memory_files`:

```sql
CREATE TABLE IF NOT EXISTS memory_files (
  memory_table TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  path TEXT NOT NULL,
  file_sha TEXT NOT NULL,
  PRIMARY KEY (memory_table, memory_id, path)
);
```

`memory_table` stores the Mnemo memory kind because current Mnemo uses one physical `memories` table with a `kind` discriminator. `memory_id` is text because memory IDs use values such as `mem_...`.

There is no backfill. Pre-existing rows keep `NULL` git metadata and remain neutral during retrieval.

## Freshness reweighting

When a memory has git metadata and touched-file fingerprints, retrieval applies a post-score freshness multiplier:

| Condition | Multiplier |
|---|---:|
| Legacy row or `git_sha IS NULL` | `1.0` |
| All touched files unchanged | `1.0` |
| Any touched file changed but still exists | `0.7` |
| Any touched file is deleted/missing | `0.3` |

For multiple files, Mnemo uses the minimum multiplier. The IDF/Jaccard score itself is unchanged; freshness is applied after scoring and before final sorting. If the workspace is not a git repository, git is unavailable, or a git command fails, the multiplier is neutral (`1.0`).

