# Release Checklist

Before publishing:

```bash
python -m compileall .
python smoke_test.py
python -m unittest discover -s . -p "test*.py"
```

Verify:

- `server.py` reports the intended version.
- `README.md` version matches `server.py`.
- `CHANGELOG.md` has the release entry.
- `pyproject.toml` version matches the release.
- Example MCP configs use `mnemo_*` tools, not legacy `memory_*` tools.
- No runtime state is included:
  - `mnemo.sqlite`
  - `memory.json`
  - `queries.jsonl`
  - `events.jsonl`
  - `*.archive.jsonl`
  - `*.lock`
  - generated exports under `state/mnemo/exports/`
- No Python/cache/build artifacts are included:
  - `__pycache__/`
  - `*.pyc`
  - `.pytest_cache/`
  - `.mypy_cache/`
  - `.coverage`
  - `dist/`
  - `build/`
  - `*.egg-info/`

Optional manual check:

```bash
python server.py
```

Then send JSON-RPC `initialize`, `tools/list`, and one `mnemo_doctor` call from a client or manual stdin probe.
