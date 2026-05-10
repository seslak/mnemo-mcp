"""Small end-to-end smoke test for the Mnemo MCP stdio server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "server.py"


def rpc_call(proc: subprocess.Popen[str], method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    request: dict[str, Any] = {"jsonrpc": "2.0", "id": method, "method": method}
    if params is not None:
        request["params"] = params
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        stderr = proc.stderr.read() if proc.stderr else ""
        raise AssertionError(f"server produced no response; stderr={stderr}")
    return json.loads(line)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "auth.py").write_text(
            "def authenticate(user):\n    return bool(user)\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.update(
            {
                "MNEMO_FILE": str(tmp_path / "memory.json"),
                "MNEMO_WORKSPACE_ROOT": str(workspace),
                "MNEMO_LOG_QUERIES": "0",
                "MNEMO_LOG_EVENTS": "0",
                "MNEMO_DECAY": "0",
            }
        )
        proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            initialized = rpc_call(
                proc,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "mnemo-smoke", "version": "0"},
                },
            )
            assert initialized["result"]["serverInfo"]["name"] == "mnemo", initialized

            tools = rpc_call(proc, "tools/list")
            tool_names = {tool["name"] for tool in tools["result"]["tools"]}
            for expected in {"memory_record", "memory_search", "memory_recent", "lookup_symbol"}:
                assert expected in tool_names, tool_names

            recorded = rpc_call(
                proc,
                "tools/call",
                {
                    "name": "memory_record",
                    "arguments": {
                        "kind": "decision",
                        "text": "Run Mnemo smoke tests before publishing.",
                        "tags": ["smoke", "validation"],
                        "pinned": True,
                    },
                },
            )
            assert not recorded["result"].get("isError"), recorded
            memory_id = recorded["result"]["structuredContent"]["memory"]["id"]

            searched = rpc_call(
                proc,
                "tools/call",
                {
                    "name": "memory_search",
                    "arguments": {"query": "smoke tests publishing", "limit": 3},
                },
            )
            assert not searched["result"].get("isError"), searched
            ids = [match["id"] for match in searched["result"]["structuredContent"]["matches"]]
            assert memory_id in ids, searched

            symbol = rpc_call(
                proc,
                "tools/call",
                {
                    "name": "lookup_symbol",
                    "arguments": {"name": "authenticate", "limit": 3},
                },
            )
            assert not symbol["result"].get("isError"), symbol
            matches = symbol["result"]["structuredContent"]["matches"]
            assert matches and matches[0]["file"] == "auth.py", symbol

            recent = rpc_call(proc, "tools/call", {"name": "memory_recent", "arguments": {"limit": 5}})
            assert not recent["result"].get("isError"), recent
            assert recent["result"]["structuredContent"]["memories"], recent

            rpc_call(proc, "shutdown")
            print("OK: mnemo MCP server smoke test passed")
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
