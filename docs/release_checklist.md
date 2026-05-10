# Release Checklist

Before publishing:

```bash
python -m compileall .
python smoke_test.py
python -m unittest discover -s . -p "test*.py"
```

Then verify:

- `memory.json` is not present or not tracked
- query/event logs are not tracked
- `__pycache__/` and `*.pyc` are absent
- `README.md` version matches `server.py`
- `CHANGELOG.md` has the release entry
- example MCP configs still point to `mnemo/server.py`
