![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Version 0.22.1](https://img.shields.io/badge/version-0.22.1-green)
![License MIT](https://img.shields.io/badge/license-MIT-blue)

# Mnemo MCP 

Local-first project memory for MCP-capable coding agents.

Mnemo is a small stdio MCP server that gives coding agents a durable, project-scoped memory substrate. It stores decisions, interaction logs, context blocks, durable project knowledge, specialist feedback, useful commands, paths, failed approaches, and test results across sessions.

> Mnemo is a local project hippocampus: store broadly, index locally, recall narrowly, export readably, compact aggressively.

## Status

Current version: **0.22.1**

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
- Memory Packs for scripted `.mem` export, inspect, import, review, and promotion workflows
- Group-first runtime selection support (`memory_group_discover`, `memory_group_preview`), including active alias concept groups
- Landing-folder pack listing for import UX (`pack_landing_list`)
- Local-HMAC pack signing and trusted import controls (`signer_add`, `signer_list`, `signer_disable`, `signer_enable`)
- Direct group selectors for pack preview, redaction preview, and export (`group_id` + `scope`)
- Alias proposal admission based on continuous IDF strength, with duplicate-heavy IDF profiles handled by unique-value cutoffs
- Review-fix hardening for symlink-safe symbol lookup, SQLite busy timeout/schema readiness, row-scoped SQLite writes, defensive environment parsing, sanitized internal MCP errors, and memoized optional Agent Salience load failures
- Scripted Copilot prompt workflows for Memory Pack export, import, promotion, and list/select compatibility under `.github/prompts/`
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


## Memory Packs

Memory Packs are portable `.mem` artifacts for moving selected Mnemo memories between local environments.

A normal pack lifecycle is:

```text
export -> .mem file
import -> staged rows in imported_pack_rows
promote -> regular local rows in memories
```

Key runtime actions:

- `memory_group_discover` and `memory_group_preview` discover exportable memory groups.
- `pack_preview` previews a selector before export.
- `pack_redaction_preview` checks the export selection for baseline sensitive patterns.
- `pack_export` creates a `.mem` file. The public suffix is `.mem`; the internal container is ZIP.
- `pack_landing_list` lists `.mem` files from the configured landing folder.
- `pack_inspect` validates and summarizes a pack before import.
- `pack_import` stages pack rows for review in `imported_pack_rows`.
- `pack_review_import` summarizes staged imported rows.
- `pack_promote_preview` previews materialization of staged rows into local memory.
- `pack_promote` creates regular local memories after explicit approval.
- `signer_add`, `signer_list`, `signer_disable`, and `signer_enable` manage local-HMAC signing metadata.

Pack import is intentionally staged. Imported rows are not regular local memories until promotion. Promotion is explicit and approval-gated.

The repository also includes scripted Copilot prompt workflows:

- `.github/prompts/mnemo.memory-pack-export.prompt.md`
- `.github/prompts/mnemo.memory-pack-import.prompt.md`
- `.github/prompts/mnemo.memory-pack-promote.prompt.md`
- `.github/prompts/mnemo.memory-list-select.prompt.md` (compatibility stub that points to export)

Security and provenance notes:

- `.mem` files can contain project-sensitive memory text and metadata.
- `content/file_fingerprints.json` records touched-file paths and hashes; it does not embed file contents.
- Local-HMAC signing is a local integrity/trust workflow, not public-key identity or non-repudiation.


## Review-fix hardening (0.22.1)

This patch keeps the public action surface stable while tightening runtime behavior:

- `lookup_symbol` skips symlinked files whose resolved targets escape `MNEMO_WORKSPACE_ROOT`.
- SQLite connections use `PRAGMA busy_timeout=5000`, and schema readiness is memoized per process so read paths do not rerun migration/backfill checks on every session.
- SQLite `record`, `update`, and `delete` mutations use row-scoped writes instead of rewriting the full store for single-row changes.
- Numeric environment variables such as `MNEMO_CONSOLIDATE_THRESHOLD`, `MNEMO_MAX_MEMORIES`, and `MNEMO_SYMBOL_TTL_SECONDS` fall back cleanly when invalid.
- Internal MCP dispatch errors return sanitized client-facing messages while preserving full diagnostics on `stderr`.
- Optional `agent_salience` load failures are memoized to avoid repeated import attempts in hot paths.

Compatibility notes:

- SQLite schema version remains `7`.
- No Mnemo action names, parameter shapes, or normal success payload shapes changed.
- No Memory Pack format or lifecycle changes are included.

## Repository layout

```text
mnemo-mcp/
├── server.py
├── salience_loader.py
├── git_context.py
├── memory.example.json
├── smoke_test.py
├── test_server.py
├── benchmark_consolidation.py
├── .github/
│   ├── prompts/
│   └── workflows/
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
| `MNEMO_ALIAS_MIN_IDF_STRENGTH` | Minimum continuous IDF strength required for alias proposals. Default: `0.30`. |
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

`doctor` returns an `idf` object with project/domain status (`cold|ready|disabled|unavailable`), activation flags, corpus counts, and remaining maturity gaps.

All activation thresholds are AND-gated:

- documents threshold must pass
- unique-terms threshold must pass
- total-token threshold must pass

IDF is a scoring aid and does not replace signatures, lexical ranking, FTS, or baseline cosine/Jaccard primitives. It now contributes through both `idf_cosine` and `idf_jaccard`.

Alias proposal admission uses continuous IDF strength instead of requiring a token to land in the top IDF bucket. IDF summary cutoffs are computed over unique values so duplicate-heavy corpora do not collapse the high cutoff onto a single maximum value.

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
