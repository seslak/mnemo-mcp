# Security Policy

Mnemo is a local stdio MCP server. It stores local project memory and can inspect source files under the configured workspace root for symbol lookup.

## Supported versions

Mnemo is pre-1.0. Security fixes are expected to target the latest released minor version.

Current active line: `0.11.x`.

## Reporting issues

Please open a private security advisory or contact the repository owner if you find a vulnerability that could expose local files, bypass workspace-root restrictions, or leak stored memory.

## Local data

Mnemo memory files, SQLite databases, and exports may contain project-sensitive information. They are gitignored by default, but users should review local files before sharing a repository or ZIP.

Common local files include:

- `state/mnemo/mnemo.sqlite`
- `state/mnemo/memory.json`
- `state/mnemo/exports/*`
- legacy `events.jsonl` / `queries.jsonl` files

## Expected boundaries

- `lookup_symbol` should stay under `MNEMO_WORKSPACE_ROOT`.
- Local memory files should not be committed unless intentionally shared as seed memory.
- Optional Agent Salience diagnostics should not require network access.
- Mnemo should not access external services by default.
