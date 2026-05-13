# Contributing

Thanks for considering a contribution.

## Development setup

Mnemo is intentionally simple and stdlib-only.

```bash
python -m compileall .
python smoke_test.py
python -m unittest discover -s . -p "test*.py"
```

## Guidelines

- Preserve local-first behavior.
- Keep the public MCP surface small: Mnemo exposes one gateway tool, `mnemo`.
- Add new capabilities as gateway actions when possible, not as new public MCP tools.
- Keep response fields backward-compatible where practical.
- Prefer additive structured fields over breaking storage changes.
- Do not introduce required external dependencies without a strong reason.
- Keep exported MCP schemas compatible with constrained clients.
- Put validation, defaults, and bounds in Python handlers.
- Add tests for new behavior.
- Do not commit local `memory.json`, SQLite databases, query logs, event logs, archive files, or generated exports.

## Storage

SQLite is the default backend, but JSON compatibility should remain available unless deliberately changed before 1.0.

## Release notes

Update `CHANGELOG.md` for user-visible changes.
