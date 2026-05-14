# Salience, IDF, and LSH Preparation Notes

Mnemo v0.12 stores deterministic memory signatures so future search/consolidation improvements can be added without changing the storage model.

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

Mnemo owns:

- SQLite persistence
- signature columns
- backfill
- candidate selection
- memory lifecycle and consolidation

## IDF stance

IDF should be local and corpus-learned.

Rules:

- IDF is disabled while the corpus is cold.
- Project-level IDF can activate after enough local records exist.
- Domain-specific IDF should activate independently per domain when that domain has enough context.
- IDF is a scoring aid, not a replacement for Jaccard/signatures.
- Mnemo can store or export corpus material; Agent Salience defines IDF math.

## Alias-map stance

Alias maps are not owned by Mnemo or Agent Salience. They are policy artifacts maintained by the coordinator/router layer.

Mnemo may store evidence for alias suggestions as `agent_feedback`, `decision`, or `hippocampus_entry`, but it should not silently activate aliases.
