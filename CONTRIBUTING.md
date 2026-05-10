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

- Keep the MCP tool names stable.
- Keep response fields backward-compatible where practical.
- Prefer additive structured fields over breaking changes.
- Do not introduce required external dependencies without a strong reason.
- Preserve local-first behavior.
- Add tests for new behavior.
- Do not commit local `memory.json`, query logs, event logs, or archive files.

## Release notes

Update `CHANGELOG.md` for user-visible changes.
