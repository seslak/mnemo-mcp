# Contributing

Thanks for considering a contribution.

Mnemo is intentionally small, local-first, and stdlib-only.

## Development setup

```bash
python -m compileall .
python smoke_test.py
python -m unittest discover -s . -p "test*.py"
```

No external services or package installs are required for normal development.

## Guidelines

- Preserve local-first behavior.
- Keep required runtime dependencies at zero unless there is a strong reason.
- Keep MCP tool schemas compatible with constrained MCP clients.
- Put validation, defaults, and bounds in Python handlers, not only in JSON Schema.
- Keep tool names stable once public.
- Prefer bounded outputs over large context dumps.
- Add or update tests for user-visible behavior.
- Update `CHANGELOG.md` for user-visible changes.
- Do not commit local runtime memory or logs.

## Storage changes

Mnemo uses SQLite by default and JSON as a compatibility/import/export format. Storage changes should preserve:

- importability of existing `memory.json`
- copy/paste portability
- readable exports
- bounded recall/search outputs

## Before opening a PR

Run:

```bash
python -m compileall .
python smoke_test.py
python -m unittest discover -s . -p "test*.py"
```
