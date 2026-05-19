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

Use maintenance actions instead of file editing. The safe flow is:

1. Preview candidates with `propose_aliases` and `dry_run=true`.
2. Review evidence and reject obvious generic/common-word proposals before persistence.
3. Persist the reviewable set with `propose_aliases` and `dry_run=false`.
4. Inspect persisted rows with `list_alias_proposals`.
5. Activate or reject persisted proposal ids with `approve_alias` / `reject_alias_proposal`.
6. Inspect active vocabulary with `list_aliases`.
7. Use `disable_alias` / `disable_alias_concept` for reversible deactivation.

Important: proposals returned from `dry_run=true` are not persisted and cannot be approved or rejected by `proposal_id`.

Approved aliases live in:

- `alias_concepts`
- `alias_terms`

These tables are consumed automatically by runtime retrieval/scoring.
