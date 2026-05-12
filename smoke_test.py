#!/usr/bin/env python3
"""End-to-end smoke test for the mnemo MCP server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "server.py"


def rpc(proc: subprocess.Popen[str], request: dict) -> dict:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("server closed stdout")
    return json.loads(line)


def call_tool(proc: subprocess.Popen[str], request_id: int, name: str, arguments: dict) -> dict:
    return rpc(
        proc,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        memory_file = root / "mnemo" / "memory.json"
        workspace = root / "repo"
        workspace.mkdir()
        (workspace / "auth.py").write_text(
            "def authenticate(user):\n    return bool(user)\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["MNEMO_FILE"] = str(memory_file)
        env["MNEMO_WORKSPACE_ROOT"] = str(workspace)
        proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            init = rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "clientInfo": {"name": "smoke-test", "version": "1"},
                    },
                },
            )
            tools = rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            record = call_tool(
                proc,
                3,
                "mnemo_record",
                {
                    "kind": "decision",
                    "text": "Run validation commands before handoff and capture the exact output for release verification.",
                    "source": "smoke-test",
                    "tags": ["validation"],
                },
            )
            memory_id = record["result"]["structuredContent"]["memory"]["id"]
            search = call_tool(
                proc,
                4,
                "mnemo_search",
                {"query": "validation handoff", "limit": 3},
            )
            update = call_tool(
                proc,
                5,
                "mnemo_update",
                {"id": memory_id, "tags": ["validation", "handoff"]},
            )
            get_preview = call_tool(proc, 6, "mnemo_get", {"id": memory_id})
            get_full = call_tool(proc, 7, "mnemo_get", {"id": memory_id, "full": True})
            symbol = call_tool(proc, 8, "mnemo_lookup_symbol", {"name": "authenticate"})
            referenced = call_tool(
                proc,
                9,
                "mnemo_record",
                {
                    "kind": "note",
                    "text": "Reference smoke memory.",
                    "references": ["seed-id"],
                },
            )
            referenced_id = referenced["result"]["structuredContent"]["memory"]["id"]
            inspect_both = call_tool(proc, 10, "mnemo_inspect", {"id": referenced_id, "mode": "both"})
            related = call_tool(proc, 11, "mnemo_inspect", {"id": "seed-id", "mode": "related"})
            export_jsonl = call_tool(proc, 12, "mnemo_export", {"format": "jsonl"})
            doctor = call_tool(proc, 13, "mnemo_doctor", {})
            consolidate = call_tool(proc, 14, "mnemo_maintenance", {"action": "consolidate"})
            capped_search = call_tool(proc, 15, "mnemo_search", {"query": "validation handoff", "max_tokens": 30})
            shutdown = rpc(proc, {"jsonrpc": "2.0", "id": 16, "method": "shutdown"})
        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=5)

    assert init["result"]["serverInfo"]["name"] == "mnemo"
    assert any(t["name"] == "mnemo_update" for t in tools["result"]["tools"])
    assert record["result"]["isError"] is False
    assert search["result"]["structuredContent"]["matches"]
    assert update["result"]["structuredContent"]["memory"]["updated_at"]
    assert get_preview["result"]["structuredContent"]["full"] is False
    assert get_full["result"]["structuredContent"]["full"] is True
    assert symbol["result"]["structuredContent"]["matches"][0]["file"] == "auth.py"
    assert referenced["result"]["structuredContent"]["memory"]["references"] == ["seed-id"]
    assert inspect_both["result"]["structuredContent"]["events"]
    assert isinstance(inspect_both["result"]["structuredContent"]["related"], list)
    assert isinstance(related["result"]["structuredContent"]["related"], list)
    assert export_jsonl["result"]["isError"] is False
    assert export_jsonl["result"]["structuredContent"]["path"]
    doctor_structured = doctor["result"]["structuredContent"]
    assert doctor_structured["backend"] in {"sqlite", "json"}
    assert isinstance(doctor_structured["memory_count"], int)
    assert "count_by_kind" in doctor_structured
    assert "count_by_authority" in doctor_structured
    assert "count_by_retention" in doctor_structured
    assert "export_files" in doctor_structured
    assert "search_backend" in doctor_structured
    assert "memory_file" in doctor_structured
    assert "events_log" in doctor_structured
    assert "archive" in doctor_structured
    assert "drift" in doctor_structured
    assert "salience" in doctor_structured
    assert "warnings" in doctor_structured
    assert 0.0 <= float(doctor_structured["drift"]["value"]) <= 1.0
    assert consolidate["result"]["isError"] is False
    assert consolidate["result"]["structuredContent"]["applied"] is False
    assert capped_search["result"]["structuredContent"]["truncated"] is True
    assert shutdown["result"] == {}
    print("OK: mnemo MCP server smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
