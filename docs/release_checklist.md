# Release Checklist

Before publishing:

```bash
python -m compileall .
python smoke_test.py
python -m unittest discover -s . -p "test*.py"
```

Then verify:

- `server.py` reports the intended `SERVER_VERSION`
- `pyproject.toml` version matches `server.py`
- `README.md` describes the current gateway tool model
- `CHANGELOG.md` has the release entry
- example MCP configs still point to `mnemo/server.py`
- `LICENSE` is present
- runtime memory files are absent
- SQLite databases are absent
- query/event logs are absent unless intentionally included as examples
- `__pycache__/` and `*.pyc` are absent
- `.pytest_cache/`, `build/`, `dist/`, and `*.egg-info/` are absent

Files that should not be included in release ZIPs:

- `mnemo.sqlite`
- `memory.json`
- `queries.jsonl`
- `events.jsonl`
- `*.archive.jsonl`
- lock files
- generated exports under `state/mnemo/exports/`
