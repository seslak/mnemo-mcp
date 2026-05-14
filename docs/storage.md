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
- `meta`
- optional FTS5 table `memories_fts`

Lifecycle and query events are stored in SQLite in SQLite mode.

## Signature columns

Mnemo 0.12.0 adds deterministic signature columns to `memories`:

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

SQLite schema migration is idempotent. Existing v0.11.x stores load under v0.12.0. Rows without signatures remain usable and can be backfilled.

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
