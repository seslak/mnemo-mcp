# Salience, IDF, and LSH Notes

Mnemo stores deterministic memory signatures and can automatically activate local IDF profiles when corpus maturity thresholds are met.

## Current status

Mnemo currently stores:

- `content_hash`
- `normalized_hash`
- `token_count`
- `unique_token_count`
- `top_terms_json`
- `shingle_hashes_json`
- `signature_version`
- `normalizer_version`
- `signature_updated_at`

These fields support exact duplicate detection, bounded near-duplicate candidate selection, and safe consolidation without all-pairs comparison.

Mnemo stores IDF profile state in SQLite (`idf_profiles`) for:

- project scope (`scope=project`, `name=default`)
- domain scope (`scope=domain`, `name=<domain>`)

## LSH stance

Mnemo does **not** implement full LSH / MinHash buckets yet.

Current goal:

- keep deterministic signatures
- avoid all-pairs consolidation
- make future LSH a signature-version/index upgrade

Future LSH should be introduced only when real stores need it. Until then, bounded shingle signatures are easier to inspect and test.

## Agent Salience boundary

Agent Salience owns reusable salience math:

- token normalization
- Jaccard/cosine semantics
- stable hash helper
- shingle helpers
- text signatures
- optional fuzzy lexical similarity
- optional alias expansion
- optional cold-start-aware IDF profiles
- optional IDF-weighted Jaccard/Tanimoto scoring

Mnemo owns:

- SQLite persistence
- signature columns
- backfill
- candidate selection
- memory lifecycle and consolidation

## IDF activation stance

IDF is local and corpus-learned.

Rules:

- In `MNEMO_IDF_MODE=auto`, IDF remains cold until maturity thresholds are reached.
- Project-level IDF activates after enough local records exist.
- Domain-level IDF activates independently per domain.
- In `MNEMO_IDF_MODE=off`, IDF remains disabled.
- In `MNEMO_IDF_MODE=force`, IDF can activate below thresholds (dev/test use only).
- IDF is a scoring aid, not a replacement for lexical/FTS/signature/Jaccard behavior.
- In active mode, Mnemo uses both `idf_cosine` and `idf_jaccard` so common words such as "and" have low impact.
- Mnemo owns corpus selection and persistence; Agent Salience defines IDF math/scoring.

Default thresholds:

- project: `200 docs`, `1000 unique terms`, `10000 total tokens`
- domain: `50 docs`, `300 unique terms`, `3000 total tokens`
- memory inclusion floor: `MNEMO_IDF_MIN_TEXT_TOKENS=5`

Each threshold set is AND-gated:

- docs threshold must pass
- unique-terms threshold must pass
- total-tokens threshold must pass

`mnemo.doctor` reports `idf.mode`, `idf.available`, project/domain status, remaining maturity counts, warnings, and recommendations.

This patch does not change candidate generation. It closes scoring behavior only.

## Alias stance

Alias knowledge is owned by Mnemo SQLite state in this release.

- proposal evidence comes from miss and `alias_hint` events
- proposal rows live in `alias_proposals` (+ `alias_proposal_events`)
- approved runtime vocabulary lives in `alias_concepts` + `alias_terms`
- runtime query paths consume active aliases automatically

For proposal quality:

- active domain/project IDF is expected for high-confidence `propose_aliases` output
- low-IDF/common terms should be penalized
- IDF-cold corpora should be treated as low-confidence and reviewed conservatively
