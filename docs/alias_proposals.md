# Alias Proposal Pipeline

Mnemo stores alias knowledge in SQLite as dynamic curated retrieval state.

## Boundary

- Mnemo proposes, stores, and serves alias knowledge from SQLite.
- Coordinator/user approval still decides what gets activated.
- Runtime query paths consume active aliases automatically.
- Repository JSON alias files are not part of runtime design.

## Evidence sources

`maintenance(action="propose_aliases")` mines:

1. miss events from query paths (`search`, `recall`, `salience_check`, `compact_context`)
2. `alias_hint` events when `include_hints=true`

Miss criteria:

- `result_count == 0`, or
- `top_score < MNEMO_MISS_TOP_SCORE_THRESHOLD` (default `0.15`)

## Proposal persistence

`propose_aliases` behavior:

- `dry_run=true`: return proposals only
- `dry_run=false`: persist proposals as `status='pending'` in `alias_proposals`

When available, source evidence event ids are linked in `alias_proposal_events`.

## IDF expectation

IDF improves proposal quality and reduces generic noise.

- active IDF enables stronger scoring confidence
- cold/unavailable IDF returns `idf_cold` and withholds proposals
- low-IDF/common terms are penalized

## Curation lifecycle

Use maintenance actions instead of file editing:

1. `propose_aliases`
2. `list_alias_proposals`
3. `approve_alias` or `reject_alias_proposal`
4. `list_aliases` for active vocabulary inspection
5. `disable_alias` / `disable_alias_concept` for reversible deactivation

Approved aliases live in:

- `alias_concepts`
- `alias_terms`

These tables are consumed automatically by runtime retrieval/scoring.
