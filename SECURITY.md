# Security Policy

Mnemo is a local stdio MCP server. It stores local project memory and can inspect source files under the configured workspace root for symbol lookup.

## Supported versions

The current pre-1.0 public line is `0.10.x`.

## Reporting issues

Please open a private security advisory or contact the repository owner if you find a vulnerability that could:

- expose local files outside the configured workspace root
- bypass workspace-root restrictions
- leak stored memory unexpectedly
- corrupt or delete memory outside documented soft-delete/maintenance behavior

## Local data

Mnemo memory files, SQLite databases, and exports may contain project-sensitive information. They are gitignored by default, but users should review local files before sharing a repository.

## Expected boundaries

- `mnemo_lookup_symbol` should stay under `MNEMO_WORKSPACE_ROOT`.
- Local memory state should not be committed unless intentionally shared as seed/example memory.
- Optional Agent Salience diagnostics should not require network access.
- Mnemo should not make network calls.
