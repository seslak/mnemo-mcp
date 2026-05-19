from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from importlib import import_module
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import salience_loader
import server


ENV_KEYS = [
    "MNEMO_FILE",
    "MNEMO_STORE",
    "MNEMO_SQLITE_FILE",
    "MNEMO_MAX_MEMORIES",
    "MNEMO_LOG_QUERIES",
    "MNEMO_WORKSPACE_ROOT",
    "MNEMO_SYMBOL_TTL_SECONDS",
    "MNEMO_DECAY",
    "MNEMO_LOG_EVENTS",
    "MNEMO_LOG_ARCHIVE",
    "MNEMO_CONSOLIDATE_THRESHOLD",
    "MNEMO_MAX_SEARCH_RESULTS",
    "MNEMO_MAX_RECENT_RESULTS",
    "MNEMO_MAX_FILES_SCANNED",
    "MNEMO_MAX_TOTAL_BYTES",
    "MNEMO_MAX_FILE_BYTES",
    "MNEMO_MCP_PROFILE",
    "MNEMO_IDF_MODE",
    "MNEMO_IDF_MIN_DOCUMENTS",
    "MNEMO_IDF_MIN_UNIQUE_TERMS",
    "MNEMO_IDF_MIN_TOTAL_TOKENS",
    "MNEMO_IDF_DOMAIN_MIN_DOCUMENTS",
    "MNEMO_IDF_DOMAIN_MIN_UNIQUE_TERMS",
    "MNEMO_IDF_DOMAIN_MIN_TOTAL_TOKENS",
    "MNEMO_IDF_MIN_TEXT_TOKENS",
    "MNEMO_MISS_TOP_SCORE_THRESHOLD",
    "AGENT_SALIENCE_HOME",
]


class MnemoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = {key: os.environ.get(key) for key in ENV_KEYS}
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.memory_file = self.root / "mnemo" / "memory.json"
        self.workspace = self.root / "repo"
        self.workspace.mkdir(parents=True)
        os.environ["MNEMO_FILE"] = str(self.memory_file)
        os.environ["MNEMO_STORE"] = "json"
        os.environ.pop("MNEMO_SQLITE_FILE", None)
        os.environ["MNEMO_WORKSPACE_ROOT"] = str(self.workspace)
        os.environ["MNEMO_LOG_QUERIES"] = "1"
        os.environ["MNEMO_LOG_EVENTS"] = "1"
        os.environ.pop("MNEMO_LOG_ARCHIVE", None)
        os.environ.pop("MNEMO_MAX_MEMORIES", None)
        os.environ.pop("MNEMO_SYMBOL_TTL_SECONDS", None)
        os.environ.pop("MNEMO_DECAY", None)
        os.environ.pop("MNEMO_CONSOLIDATE_THRESHOLD", None)
        os.environ.pop("MNEMO_MAX_SEARCH_RESULTS", None)
        os.environ.pop("MNEMO_MAX_RECENT_RESULTS", None)
        os.environ.pop("MNEMO_MAX_FILES_SCANNED", None)
        os.environ.pop("MNEMO_MAX_TOTAL_BYTES", None)
        os.environ.pop("MNEMO_MAX_FILE_BYTES", None)
        os.environ.pop("MNEMO_MCP_PROFILE", None)
        os.environ.pop("MNEMO_IDF_MODE", None)
        os.environ.pop("MNEMO_IDF_MIN_DOCUMENTS", None)
        os.environ.pop("MNEMO_IDF_MIN_UNIQUE_TERMS", None)
        os.environ.pop("MNEMO_IDF_MIN_TOTAL_TOKENS", None)
        os.environ.pop("MNEMO_IDF_DOMAIN_MIN_DOCUMENTS", None)
        os.environ.pop("MNEMO_IDF_DOMAIN_MIN_UNIQUE_TERMS", None)
        os.environ.pop("MNEMO_IDF_DOMAIN_MIN_TOTAL_TOKENS", None)
        os.environ.pop("MNEMO_IDF_MIN_TEXT_TOKENS", None)
        os.environ.pop("MNEMO_MISS_TOP_SCORE_THRESHOLD", None)
        os.environ.pop("AGENT_SALIENCE_HOME", None)
        server._SYMBOL_CACHE.clear()

    def tearDown(self) -> None:
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        server._SYMBOL_CACHE.clear()
        self.tmp.cleanup()

    def read_store(self) -> dict:
        return server.load_store()

    def write_store(self, memories: list[dict]) -> None:
        server.save_store({"version": 1, "memories": memories})

    def read_events(self) -> list[dict]:
        return server.read_event_rows(include_archive=False)

    def record(self, text: str, **kwargs) -> dict:
        args = {"text": text}
        args.update(kwargs)
        result = server.record_memory(args)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]["memory"]


class SalienceLoaderTests(MnemoTestCase):
    def make_fake_salience_home(self) -> Path:
        home = self.root / "salience_home"
        package_dir = home / "src" / "agent_salience"
        package_dir.mkdir(parents=True, exist_ok=True)
        (package_dir / "__init__.py").write_text(
            "__version__ = '0.0.test'\nTEST_MARKER = 'fake-loader'\n",
            encoding="utf-8",
        )
        return home

    def test_salience_loader_unavailable_returns_reason(self) -> None:
        os.environ.pop("AGENT_SALIENCE_HOME", None)
        with mock.patch("salience_loader.importlib.import_module", side_effect=ModuleNotFoundError("missing")):
            module, reason = salience_loader.load_optional_agent_salience()
        self.assertIsNone(module)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("AGENT_SALIENCE_HOME", reason)

    def test_salience_loader_imports_via_agent_salience_home(self) -> None:
        home = self.make_fake_salience_home()
        os.environ["AGENT_SALIENCE_HOME"] = str(home)
        sys.modules.pop("agent_salience", None)

        calls = {"count": 0}
        real_import = import_module

        def side_effect(name: str, package: str | None = None):
            if name == "agent_salience" and calls["count"] == 0:
                calls["count"] += 1
                raise ModuleNotFoundError("forced miss")
            return real_import(name, package=package)

        with mock.patch("salience_loader.importlib.import_module", side_effect=side_effect):
            module, reason = salience_loader.load_optional_agent_salience()
        self.assertIsNone(reason)
        self.assertIsNotNone(module)
        assert module is not None
        self.assertEqual(getattr(module, "TEST_MARKER", ""), "fake-loader")
        sys.modules.pop("agent_salience", None)

    def test_salience_loader_repeated_calls_do_not_duplicate_sys_path(self) -> None:
        home = self.make_fake_salience_home()
        os.environ["AGENT_SALIENCE_HOME"] = str(home)
        sys.modules.pop("agent_salience", None)
        target_path = str((home / "src").resolve())
        before = sys.path.count(target_path)

        calls = {"count": 0}
        real_import = import_module

        def side_effect(name: str, package: str | None = None):
            if name == "agent_salience" and calls["count"] == 0:
                calls["count"] += 1
                raise ModuleNotFoundError("forced miss")
            return real_import(name, package=package)

        with mock.patch("salience_loader.importlib.import_module", side_effect=side_effect):
            first, _ = salience_loader.load_optional_agent_salience()
            second, _ = salience_loader.load_optional_agent_salience()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        after = sys.path.count(target_path)
        self.assertEqual(after, before + (0 if before else 1))
        sys.modules.pop("agent_salience", None)


class MemorySalienceToolTests(MnemoTestCase):
    def local_agent_salience_home(self) -> Path:
        return Path(__file__).resolve().parent.parent / "agent-salience"

    def ensure_local_agent_salience(self) -> Path:
        home = self.local_agent_salience_home()
        if not (home / "src" / "agent_salience" / "__init__.py").exists():
            self.skipTest("local agent-salience sibling not available")
        return home

    def test_memory_salience_check_unavailable_response(self) -> None:
        self.record("auth middleware marker", kind="note")
        with mock.patch("server.load_optional_agent_salience", return_value=(None, "forced missing")):
            result = server.memory_salience_check({"text": "auth middleware marker"})
        self.assertTrue(result["isError"])
        structured = result["structuredContent"]
        self.assertEqual(structured["error"], "agent_salience_unavailable")
        self.assertIn("AGENT_SALIENCE_HOME", structured["message"])

    def test_memory_salience_check_available_related_memory_triggers(self) -> None:
        home = self.ensure_local_agent_salience()
        os.environ["AGENT_SALIENCE_HOME"] = str(home)
        self.record("Use auth middleware before route handlers.", kind="decision")
        self.record("Completely unrelated deployment note.", kind="note")
        result = server.memory_salience_check({"text": "auth middleware route handlers", "limit": 5})
        self.assertFalse(result["isError"], result)
        structured = result["structuredContent"]
        self.assertTrue(structured["available"])
        self.assertTrue(any(match["triggered"] for match in structured["matches"]))
        self.assertIn("breakdown", structured["matches"][0])

    def test_memory_salience_check_unrelated_does_not_trigger_high_threshold(self) -> None:
        home = self.ensure_local_agent_salience()
        os.environ["AGENT_SALIENCE_HOME"] = str(home)
        self.record("auth middleware operations", kind="note")
        result = server.memory_salience_check({"text": "kubernetes ingress rollout", "threshold": 0.95})
        self.assertFalse(result["isError"], result)
        self.assertFalse(result["structuredContent"]["triggered"])

    def test_memory_salience_check_threshold_override(self) -> None:
        home = self.ensure_local_agent_salience()
        os.environ["AGENT_SALIENCE_HOME"] = str(home)
        self.record("auth middleware threshold marker", kind="note")
        strict = server.memory_salience_check({"text": "auth marker", "threshold": 0.99})
        permissive = server.memory_salience_check({"text": "auth marker", "threshold": 0.1})
        self.assertFalse(strict["structuredContent"]["triggered"])
        self.assertTrue(permissive["structuredContent"]["triggered"])

    def test_memory_salience_check_deleted_and_superseded_filters(self) -> None:
        home = self.ensure_local_agent_salience()
        os.environ["AGENT_SALIENCE_HOME"] = str(home)
        old = self.record("legacy auth salience marker", kind="decision")
        self.record("modern auth path", kind="decision", supersedes=old["id"])
        deleted = self.record("deleted salience marker", kind="note")
        server.delete_memory({"id": deleted["id"], "reason": "obsolete"})

        default = server.memory_salience_check({"text": "legacy auth salience marker", "limit": 20})
        with_superseded = server.memory_salience_check(
            {"text": "legacy auth salience marker", "limit": 20, "include_superseded": True}
        )
        with_deleted = server.memory_salience_check(
            {"text": "deleted salience marker", "limit": 20, "include_deleted": True}
        )

        default_ids = {match["memory_id"] for match in default["structuredContent"]["matches"]}
        superseded_ids = {match["memory_id"] for match in with_superseded["structuredContent"]["matches"]}
        deleted_ids = {match["memory_id"] for match in with_deleted["structuredContent"]["matches"]}
        self.assertNotIn(old["id"], default_ids)
        self.assertIn(old["id"], superseded_ids)
        self.assertIn(deleted["id"], deleted_ids)


class StructuredMemoryLayerTests(MnemoTestCase):
    def test_record_interaction_log_defaults(self) -> None:
        result = server.record_memory({"kind": "interaction_log", "summary": "coordinator startup sync"})
        self.assertFalse(result["isError"], result)
        memory = result["structuredContent"]["memory"]
        self.assertEqual(memory["kind"], "interaction_log")
        self.assertEqual(memory["role"], "coordinator")
        self.assertEqual(memory["retention"], "compressible")
        self.assertEqual(memory["authority"], "low")

    def test_record_context_block_stores_body_and_links(self) -> None:
        log = server.record_memory({"kind": "interaction_log", "summary": "first log", "linked_ids": []})[
            "structuredContent"
        ]["memory"]
        result = server.record_memory(
            {
                "kind": "context_block",
                "title": "Context title",
                "body": "Larger context body",
                "linked_ids": [log["id"]],
                "tags": ["context"],
            }
        )
        self.assertFalse(result["isError"], result)
        memory = result["structuredContent"]["memory"]
        self.assertEqual(memory["kind"], "context_block")
        self.assertEqual(memory["text"], "Larger context body")
        self.assertIn(log["id"], memory["linked_ids"])
        self.assertEqual(memory["metadata"]["title"], "Context title")

    def test_record_hippocampus_entry_links_evidence(self) -> None:
        base = self.record("evidence reference", kind="note")
        result = server.record_memory(
            {
                "kind": "hippocampus_entry",
                "title": "Durable rule",
                "text": "Always run release validation before tagging.",
                "domain": "release",
                "scope": "project",
                "tags": ["release", "validation"],
                "evidence_ids": [base["id"]],
            }
        )
        self.assertFalse(result["isError"], result)
        memory = result["structuredContent"]["memory"]
        self.assertEqual(memory["kind"], "hippocampus_entry")
        self.assertEqual(memory["domain"], "release")
        self.assertEqual(memory["scope"], "project")
        self.assertIn(base["id"], memory["linked_ids"])
        self.assertEqual(memory["retention"], "durable")

    def test_record_agent_feedback_requires_scope_and_recall_finds_it(self) -> None:
        missing = server.record_memory({"kind": "agent_feedback", "text": "too broad"})
        self.assertTrue(missing["isError"])
        self.assertIn("at least one of agent_id, role, or domain is required", missing["content"][0]["text"])

        stored = server.record_memory(
            {
                "kind": "agent_feedback",
                "agent_id": "spec_auth",
                "role": "specialist",
                "domain": "auth",
                "feedback_type": "good_pattern",
                "text": "Prefer middleware-first auth checks.",
            }
        )
        self.assertFalse(stored["isError"], stored)
        memory = stored["structuredContent"]["memory"]
        self.assertEqual(memory["kind"], "agent_feedback")
        self.assertEqual(memory["metadata"]["feedback_type"], "good_pattern")

        recalled = server.memory_recall(
            {"mode": "agent", "agent_id": "spec_auth", "role": "specialist", "domain": "auth", "task": "auth middleware"}
        )
        self.assertFalse(recalled["isError"], recalled)
        feedback_ids = {item["id"] for item in recalled["structuredContent"]["agent_feedback"]}
        self.assertIn(memory["id"], feedback_ids)

    def test_memory_link_updates_links_and_bidirectional(self) -> None:
        left = self.record("left node", kind="note")
        right = self.record("right node", kind="note")

        linked = server.memory_link(
            {"source_id": left["id"], "target_id": right["id"], "relation": "related_to", "bidirectional": True}
        )
        self.assertFalse(linked["isError"], linked)
        structured = linked["structuredContent"]
        self.assertIn(right["id"], structured["source_links"])
        self.assertIn(left["id"], structured["target_links"])

        missing = server.memory_link({"source_id": "missing", "target_id": right["id"]})
        self.assertTrue(missing["isError"])
        self.assertIn("memory not found", missing["content"][0]["text"])

    def test_memory_recall_startup_context_returns_layer_bundle(self) -> None:
        log = server.record_memory(
            {"kind": "interaction_log", "summary": "Coordinator handoff summary", "agent_id": "coord_1", "role": "coordinator"}
        )["structuredContent"]["memory"]
        block = server.record_memory(
            {
                "kind": "context_block",
                "body": "Block linked from startup log.",
                "linked_ids": [log["id"]],
                "agent_id": "coord_1",
                "role": "coordinator",
            }
        )["structuredContent"]["memory"]
        hip = server.record_memory(
            {"kind": "hippocampus_entry", "text": "Durable startup rule.", "domain": "coordination", "scope": "project"}
        )["structuredContent"]["memory"]
        feedback = server.record_memory(
            {"kind": "agent_feedback", "role": "coordinator", "domain": "coordination", "text": "Keep summaries concise."}
        )["structuredContent"]["memory"]

        result = server.memory_recall(
            {
                "mode": "startup",
                "agent_id": "coord_1",
                "role": "coordinator",
                "query": "startup rule",
                "recent_logs": 20,
                "max_blocks": 5,
                "max_hippocampus": 8,
                "max_feedback": 5,
            }
        )
        self.assertFalse(result["isError"], result)
        structured = result["structuredContent"]
        log_ids = {item["id"] for item in structured["recent_logs"]}
        block_ids = {item["id"] for item in structured["context_blocks"]}
        hip_ids = {item["id"] for item in structured["hippocampus_entries"]}
        feedback_ids = {item["id"] for item in structured["agent_feedback"]}
        self.assertIn(log["id"], log_ids)
        self.assertIn(block["id"], block_ids)
        self.assertIn(hip["id"], hip_ids)
        self.assertIn(feedback["id"], feedback_ids)

    def test_memory_recall_agent_context_role_domain(self) -> None:
        fb1 = server.record_memory(
            {"kind": "agent_feedback", "role": "reviewer", "domain": "security", "text": "Always check auth boundaries."}
        )["structuredContent"]["memory"]
        fb2 = server.record_memory(
            {
                "kind": "agent_feedback",
                "agent_id": "spec_sec",
                "role": "reviewer",
                "domain": "security",
                "text": "Flag privilege drift.",
            }
        )["structuredContent"]["memory"]
        hip = server.record_memory(
            {"kind": "hippocampus_entry", "text": "Security invariants for auth layer", "domain": "security"}
        )["structuredContent"]["memory"]

        result = server.memory_recall(
            {"mode": "agent", "agent_id": "spec_sec", "role": "reviewer", "domain": "security", "task": "auth privilege checks"}
        )
        self.assertFalse(result["isError"], result)
        feedback_ids = {item["id"] for item in result["structuredContent"]["agent_feedback"]}
        self.assertIn(fb1["id"], feedback_ids)
        self.assertIn(fb2["id"], feedback_ids)
        hippocampus_ids = {item["id"] for item in result["structuredContent"]["hippocampus_entries"]}
        self.assertIn(hip["id"], hippocampus_ids)

    def test_memory_maintenance_compact_logs_dry_run_and_write(self) -> None:
        for idx in range(6):
            server.record_memory({"kind": "interaction_log", "summary": f"log {idx}", "agent_id": "coord", "role": "coordinator"})

        dry = server.memory_maintenance(
            {"action": "compact_logs", "older_than_count": 2, "agent_id": "coord", "role": "coordinator", "dry_run": True, "max_logs": 10}
        )
        self.assertFalse(dry["isError"], dry)
        self.assertTrue(dry["structuredContent"]["dry_run"])
        candidate = dry["structuredContent"]["candidate"]
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["kind"], "context_block")

        applied = server.memory_maintenance(
            {"action": "compact_logs", "older_than_count": 2, "agent_id": "coord", "role": "coordinator", "dry_run": False, "max_logs": 10}
        )
        self.assertFalse(applied["isError"], applied)
        block_id = applied["structuredContent"]["block_id"]
        store = self.read_store()
        block = next((memory for memory in store["memories"] if memory["id"] == block_id), None)
        self.assertIsNotNone(block)
        assert block is not None
        self.assertEqual(block["kind"], "context_block")
        # Source logs remain present and readable.
        log_ids = [memory["id"] for memory in store["memories"] if memory["kind"] == "interaction_log"]
        self.assertGreaterEqual(len(log_ids), 6)

    def test_backward_compatibility_old_tools_still_work(self) -> None:
        recorded = server.record_memory({"kind": "note", "text": "legacy note record"})
        self.assertFalse(recorded["isError"], recorded)
        searched = server.search_memories({"query": "legacy note", "limit": 5})
        self.assertFalse(searched["isError"], searched)
        self.assertTrue(searched["structuredContent"]["matches"])

        self.write_store(self.read_store()["memories"] + [])
        self.write_store(self.read_store()["memories"])
        self.write_store(self.read_store()["memories"])
        self.workspace.joinpath("mod.py").write_text("def marker_symbol():\n    return 1\n", encoding="utf-8")
        symbol = server.lookup_symbol({"name": "marker_symbol", "limit": 5})
        self.assertFalse(symbol["isError"], symbol)
        self.assertTrue(symbol["structuredContent"]["matches"])

        with mock.patch("server.load_optional_agent_salience", return_value=(None, "forced missing")):
            salience = server.memory_salience_check({"text": "legacy note"})
        self.assertTrue(salience["isError"])
        self.assertEqual(salience["structuredContent"]["error"], "agent_salience_unavailable")


class ToolSurfaceTests(MnemoTestCase):
    def test_tools_list_exposes_single_mnemo_gateway(self) -> None:
        names = {tool["name"] for tool in server.TOOLS}
        self.assertEqual(names, {"mnemo"})
        removed = {
            "memory_record",
            "memory_search",
            "mnemo_doctor",
            "mnemo_record",
            "mnemo_search",
            "mnemo_recall",
            "mnemo_get",
            "mnemo_export",
            "mnemo_maintenance",
            "mnemo_lookup_symbol",
        }
        self.assertTrue(removed.isdisjoint(names))

    def test_tools_descriptions_include_project_memory_gateway_note(self) -> None:
        tool = server.TOOLS[0]
        self.assertEqual(tool["name"], "mnemo")
        desc = str(tool.get("description", ""))
        self.assertIn("Mnemo project-memory", desc)
        self.assertIn("gateway", desc)
        self.assertIn("Copilot native memory", desc)

    def test_mnemo_doctor_returns_expected_fields(self) -> None:
        result = server.mnemo_doctor({})
        self.assertFalse(result["isError"], result)
        payload = result["structuredContent"]
        self.assertEqual(payload["server_name"], server.SERVER_NAME)
        self.assertEqual(payload["version"], server.SERVER_VERSION)
        self.assertTrue(payload["package_file"])
        self.assertTrue(payload["python"])
        self.assertTrue(payload["executable"])
        self.assertIn("memory_file", payload)
        self.assertIn("events_log", payload)
        self.assertIn("archive", payload)
        self.assertIn("drift", payload)
        self.assertIn("salience", payload)
        self.assertIn("warnings", payload)
        self.assertTrue(payload["memory_file"]["path"])
        self.assertIn("exists", payload["memory_file"])
        self.assertIn("size_bytes", payload["memory_file"])
        self.assertIn("memory_count", payload["memory_file"])
        self.assertIn("kinds", payload["memory_file"])
        self.assertIn("last_write_iso", payload["memory_file"])
        self.assertIn("last_memory_id", payload["memory_file"])
        self.assertIn("path", payload["events_log"])
        self.assertIn("exists", payload["events_log"])
        self.assertIn("last_event_iso", payload["events_log"])
        self.assertIn("last_event_kind", payload["events_log"])
        self.assertIn("workspace_root", payload)
        self.assertTrue(payload["structured_memory_tools_available"])
        self.assertEqual(payload["public_tool_prefix"], "mnemo")
        self.assertTrue(payload["gateway"])
        self.assertEqual(payload["gateway_tool"], "mnemo")
        self.assertEqual(payload["public_tool_count"], 1)
        self.assertIn("record", payload["available_actions"])
        self.assertIn("search", payload["available_actions"])

    def test_gateway_includes_event_history_actions(self) -> None:
        for action in ("recent_events", "search_events", "get_event", "memory_events"):
            self.assertIn(action, server.GATEWAY_ACTIONS)
        tool = server.TOOLS[0]
        enum_values = tool["inputSchema"]["properties"]["action"]["enum"]
        for action in ("recent_events", "search_events", "get_event", "memory_events"):
            self.assertIn(action, enum_values)


class DoctorPayloadTests(MnemoTestCase):
    def test_doctor_returns_memory_file_path_size_count(self) -> None:
        self.record("doctor payload base", kind="note")
        result = server.mnemo_doctor({})
        self.assertFalse(result["isError"], result)
        payload = result["structuredContent"]["memory_file"]
        self.assertEqual(payload["path"], str(self.memory_file.resolve()))
        self.assertTrue(payload["exists"])
        self.assertGreater(payload["size_bytes"], 0)
        self.assertEqual(payload["memory_count"], 1)

    def test_doctor_returns_kinds_breakdown(self) -> None:
        self.record("kinds one", kind="interaction_log", summary="k1")
        self.record("kinds two", kind="context_block", body="k2")
        result = server.mnemo_doctor({})
        kinds = result["structuredContent"]["memory_file"]["kinds"]
        self.assertEqual(kinds.get("interaction_log"), 1)
        self.assertEqual(kinds.get("context_block"), 1)

    def test_doctor_returns_last_write_iso_and_id(self) -> None:
        first = self.record("doctor last id 1", kind="note")
        second = self.record("doctor last id 2", kind="note")
        result = server.mnemo_doctor({})
        payload = result["structuredContent"]["memory_file"]
        self.assertTrue(payload["last_write_iso"])
        self.assertEqual(payload["last_memory_id"], second["id"])
        self.assertNotEqual(first["id"], second["id"])

    def test_doctor_returns_drift_with_default_windows(self) -> None:
        for idx in range(6):
            self.record(f"drift token {idx}", kind="note")
        result = server.mnemo_doctor({})
        drift = result["structuredContent"]["drift"]
        self.assertIn("value", drift)
        self.assertIn("recent_count", drift)
        self.assertIn("older_count", drift)
        self.assertIn("interpretation", drift)
        self.assertGreaterEqual(float(drift["value"]), 0.0)
        self.assertLessEqual(float(drift["value"]), 1.0)

    def test_doctor_drift_insufficient_history_when_few_memories(self) -> None:
        self.record("few history one", kind="note")
        self.record("few history two", kind="note")
        result = server.mnemo_doctor({})
        drift = result["structuredContent"]["drift"]
        self.assertEqual(float(drift["value"]), 0.0)
        self.assertEqual(drift["interpretation"], "insufficient_history")

    def test_doctor_returns_salience_loaded_status(self) -> None:
        fake_module = type("FakeSalience", (), {"__version__": "0.1.1", "__file__": str(self.root / "fake.py")})()
        with mock.patch("server.load_optional_agent_salience", return_value=(fake_module, None)):
            result = server.mnemo_doctor({})
        salience = result["structuredContent"]["salience"]
        self.assertTrue(salience["loaded"])
        self.assertEqual(salience["version"], "0.1.1")

    def test_doctor_emits_warning_when_memory_file_empty(self) -> None:
        self.memory_file.parent.mkdir(parents=True, exist_ok=True)
        self.memory_file.write_text("", encoding="utf-8")
        result = server.mnemo_doctor({})
        warnings = result["structuredContent"]["warnings"]
        self.assertTrue(any("possibly empty" in warning for warning in warnings))

    def test_doctor_emits_warning_when_events_disabled(self) -> None:
        os.environ["MNEMO_LOG_EVENTS"] = "0"
        result = server.mnemo_doctor({})
        warnings = result["structuredContent"]["warnings"]
        self.assertTrue(any("MNEMO_LOG_EVENTS=0" in warning for warning in warnings))


class InspectTests(MnemoTestCase):
    def test_inspect_mode_history_returns_events(self) -> None:
        memory = self.record("inspect history marker", kind="note")
        server.update_memory({"id": memory["id"], "tags": ["inspect"]})
        result = server.memory_inspect({"id": memory["id"], "mode": "history"})
        self.assertFalse(result["isError"], result)
        self.assertEqual([event["event"] for event in result["structuredContent"]["events"]], ["create", "update"])

    def test_inspect_mode_related_returns_graph_walk(self) -> None:
        left = self.record("inspect related left", kind="note")
        right = self.record("inspect related right", kind="note")
        server.memory_link({"source_id": left["id"], "target_id": right["id"]})
        result = server.memory_inspect({"id": left["id"], "mode": "related"})
        self.assertFalse(result["isError"], result)
        ids = [item["id"] for item in result["structuredContent"]["related"]]
        self.assertIn(right["id"], ids)

    def test_inspect_mode_both_returns_events_and_related(self) -> None:
        root = self.record("inspect both root", kind="note")
        neighbor = self.record("inspect both neighbor", kind="note")
        server.memory_link({"source_id": root["id"], "target_id": neighbor["id"]})
        result = server.memory_inspect({"id": root["id"], "mode": "both"})
        self.assertFalse(result["isError"], result)
        self.assertIn("events", result["structuredContent"])
        self.assertIn("related", result["structuredContent"])

    def test_inspect_default_mode_is_both(self) -> None:
        memory = self.record("inspect default", kind="note")
        result = server.memory_inspect({"id": memory["id"]})
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["mode"], "both")
        self.assertIn("events", result["structuredContent"])
        self.assertIn("related", result["structuredContent"])

    def test_inspect_missing_id_returns_tool_error(self) -> None:
        result = server.memory_inspect({})
        self.assertTrue(result["isError"])
        self.assertIn("id is required", result["content"][0]["text"])


class MaintenanceTests(MnemoTestCase):
    def test_maintenance_action_compact_logs_dry_run(self) -> None:
        for idx in range(6):
            self.record(f"log compact dry {idx}", kind="interaction_log", role="coordinator", agent_id="coord")
        result = server.memory_maintenance({"action": "compact_logs", "older_than_count": 2, "dry_run": True})
        self.assertFalse(result["isError"], result)
        self.assertTrue(result["structuredContent"]["dry_run"])
        self.assertIsNotNone(result["structuredContent"]["candidate"])

    def test_maintenance_action_compact_logs_apply(self) -> None:
        for idx in range(6):
            self.record(f"log compact apply {idx}", kind="interaction_log", role="coordinator", agent_id="coord")
        result = server.memory_maintenance({"action": "compact_logs", "older_than_count": 2, "dry_run": False})
        self.assertFalse(result["isError"], result)
        self.assertFalse(result["structuredContent"]["dry_run"])
        self.assertTrue(result["structuredContent"]["block_id"])

    def test_maintenance_action_consolidate_dry_run(self) -> None:
        self.record("duplicate consolidate", kind="decision")
        self.record("duplicate consolidate now", kind="decision")
        result = server.memory_maintenance({"action": "consolidate", "dry_run": True})
        self.assertFalse(result["isError"], result)
        self.assertFalse(result["structuredContent"]["applied"])
        self.assertIn("clusters", result["structuredContent"])

    def test_maintenance_action_consolidate_apply(self) -> None:
        self.write_store(
            [
                server.new_memory("old", "decision", "use auth middleware before handling route requests securely", "", []),
                server.new_memory("new", "decision", "use auth middleware before handling route requests now", "", []),
            ]
        )
        result = server.memory_maintenance({"action": "consolidate", "dry_run": False})
        self.assertFalse(result["isError"], result)
        self.assertTrue(result["structuredContent"]["applied"])

    def test_maintenance_unknown_action_returns_tool_error(self) -> None:
        result = server.memory_maintenance({"action": "unknown"})
        self.assertTrue(result["isError"])
        self.assertIn("action must be one of", result["content"][0]["text"])

    def test_maintenance_threshold_env_var_respected_in_consolidate(self) -> None:
        os.environ["MNEMO_CONSOLIDATE_THRESHOLD"] = "0.95"
        self.write_store(
            [
                server.new_memory("old", "decision", "use auth middleware before handling route requests securely", "", []),
                server.new_memory("new", "decision", "use auth middleware before handling route requests now", "", []),
            ]
        )
        result = server.memory_maintenance({"action": "consolidate", "dry_run": True})
        self.assertEqual(result["structuredContent"]["clusters"], [])


class SchemaCompatibilityTests(MnemoTestCase):
    def _blocked_terms(self) -> tuple[str, ...]:
        return tuple(str(term) for term in sorted(server.FORBIDDEN_SCHEMA_KEYS))

    def _safe_keys(self) -> set[str]:
        return set(str(key) for key in server.SUPPORTED_SCHEMA_KEYS)

    def _walk_keywords(self, value: object, found: set[str]) -> None:
        blocked = set(self._blocked_terms())
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "properties" and isinstance(item, dict):
                    for prop_schema in item.values():
                        self._walk_keywords(prop_schema, found)
                    continue
                if key in blocked:
                    found.add(key)
                self._walk_keywords(item, found)
        elif isinstance(value, list):
            for item in value:
                self._walk_keywords(item, found)

    def _tools_list_response(self) -> list[dict]:
        captured: list[dict] = []

        def capture(message: dict) -> None:
            captured.append(message)

        with mock.patch("server.send", side_effect=capture):
            server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        self.assertTrue(captured)
        response = captured[-1]["result"]["tools"]
        self.assertIsInstance(response, list)
        return response

    def test_schema_cleanup_drops_blocked_keys(self) -> None:
        blocked = list(self._blocked_terms())
        schema = {
            "type": "object",
            "properties": {"limit": {"type": "integer", blocked[0]: 1, blocked[1]: 20}},
        }
        cleaned = server.make_copilot_safe_schema(schema)
        self.assertNotIn(blocked[0], cleaned["properties"]["limit"])
        self.assertNotIn(blocked[1], cleaned["properties"]["limit"])

    def test_schema_cleanup_recurses_at_any_depth(self) -> None:
        blocked = list(self._blocked_terms())
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    blocked[2]: [],
                    "items": {"type": "object", "properties": {"flag": {"type": "boolean", blocked[2]: False}}},
                }
            },
        }
        cleaned = server.make_copilot_safe_schema(schema)
        found: set[str] = set()
        self._walk_keywords(cleaned, found)
        self.assertEqual(found, set())

    def test_schema_cleanup_keeps_type_enum_description_intact(self) -> None:
        schema = {
            "type": "object",
            "properties": {"mode": {"type": "string", "enum": ["startup", "agent"], "description": "recall mode"}},
        }
        cleaned = server.make_copilot_safe_schema(schema)
        mode = cleaned["properties"]["mode"]
        self.assertEqual(mode["type"], "string")
        self.assertEqual(mode["enum"], ["startup", "agent"])
        self.assertEqual(mode["description"], "recall mode")

    def test_schema_cleanup_drops_unsupported_schema_keys(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "a": {
                    "type": "string",
                    "title": "drop me",
                    "description": "keep me",
                }
            },
            "title": "drop root title",
        }
        cleaned = server.make_copilot_safe_schema(schema)
        self.assertNotIn("title", cleaned)
        self.assertNotIn("title", cleaned["properties"]["a"])
        self.assertEqual(cleaned["properties"]["a"]["description"], "keep me")

    def test_tools_list_response_has_no_unsupported_keywords(self) -> None:
        tools = self._tools_list_response()
        found: set[str] = set()
        for tool in tools:
            self._walk_keywords(tool.get("inputSchema", {}), found)
        self.assertEqual(found, set())

    def test_tools_list_response_has_no_type_arrays(self) -> None:
        tools = self._tools_list_response()
        for tool in tools:
            schema = tool.get("inputSchema", {})
            stack = [schema]
            while stack:
                current = stack.pop()
                if isinstance(current, dict):
                    self.assertFalse(isinstance(current.get("type"), list))
                    stack.extend(current.values())
                elif isinstance(current, list):
                    stack.extend(current)

    def test_tools_list_response_uses_safe_schema_keys_only(self) -> None:
        safe = self._safe_keys()
        tools = self._tools_list_response()
        stack = [tool.get("inputSchema", {}) for tool in tools]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key, value in current.items():
                    if key == "properties":
                        self.assertIsInstance(value, dict)
                        stack.extend(value.values())
                        continue
                    self.assertIn(key, safe)
                    stack.append(value)
            elif isinstance(current, list):
                stack.extend(current)

    def test_handlers_still_clamp_limits_when_schema_does_not(self) -> None:
        for index in range(30):
            self.record(f"search clamp marker {index}", kind="note")
        result = server.search_memories({"query": "search clamp marker", "limit": 9999})
        self.assertFalse(result["isError"], result)
        self.assertEqual(len(result["structuredContent"]["matches"]), 20)


class ProfileExposureTests(MnemoTestCase):
    def _tools_list_names(self) -> set[str]:
        captured: list[dict] = []

        def capture(message: dict) -> None:
            captured.append(message)

        with mock.patch("server.send", side_effect=capture):
            server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        response = captured[-1]["result"]["tools"]
        return {str(tool["name"]) for tool in response}

    def test_gateway_exposure_ignores_legacy_full_profile(self) -> None:
        os.environ["MNEMO_MCP_PROFILE"] = "full"
        self.assertEqual(self._tools_list_names(), {"mnemo"})

    def test_gateway_exposure_ignores_legacy_core_profile(self) -> None:
        os.environ["MNEMO_MCP_PROFILE"] = "core"
        self.assertEqual(self._tools_list_names(), {"mnemo"})

    def test_gateway_exposure_ignores_invalid_profile(self) -> None:
        os.environ["MNEMO_MCP_PROFILE"] = "weird"
        self.assertEqual(self._tools_list_names(), {"mnemo"})


class ConsolidationCompatibilityTests(MnemoTestCase):
    def test_record_kind_interaction_log_accepts_summary_as_text(self) -> None:
        result = server.record_memory({"kind": "interaction_log", "summary": "startup summary entry"})
        self.assertFalse(result["isError"], result)
        memory = result["structuredContent"]["memory"]
        self.assertEqual(memory["kind"], "interaction_log")
        self.assertEqual(memory["text"], "startup summary entry")

    def test_record_kind_context_block_accepts_body_as_text(self) -> None:
        result = server.record_memory({"kind": "context_block", "body": "context body payload"})
        self.assertFalse(result["isError"], result)
        memory = result["structuredContent"]["memory"]
        self.assertEqual(memory["kind"], "context_block")
        self.assertEqual(memory["text"], "context body payload")

    def test_record_kind_hippocampus_merges_evidence_ids_into_linked_ids(self) -> None:
        ref = self.record("evidence source", kind="note")
        result = server.record_memory(
            {
                "kind": "hippocampus_entry",
                "text": "durable rule",
                "linked_ids": ["custom-link"],
                "evidence_ids": [ref["id"]],
            }
        )
        self.assertFalse(result["isError"], result)
        memory = result["structuredContent"]["memory"]
        self.assertIn("custom-link", memory["linked_ids"])
        self.assertIn(ref["id"], memory["linked_ids"])

    def test_record_kind_agent_feedback_requires_scope(self) -> None:
        result = server.record_memory({"kind": "agent_feedback", "text": "scope missing"})
        self.assertTrue(result["isError"])
        self.assertIn("at least one of agent_id, role, or domain is required", result["content"][0]["text"])

    def test_recall_mode_startup_returns_startup_bundle_shape(self) -> None:
        server.record_memory({"kind": "interaction_log", "summary": "startup log"})
        result = server.memory_recall({"mode": "startup"})
        self.assertFalse(result["isError"], result)
        payload = result["structuredContent"]
        self.assertEqual(payload["mode"], "startup")
        self.assertIn("recent_logs", payload)
        self.assertIn("context_blocks", payload)
        self.assertIn("hippocampus_entries", payload)
        self.assertIn("agent_feedback", payload)
        self.assertIn("pinned", payload)

    def test_recall_mode_agent_returns_agent_bundle_shape(self) -> None:
        server.record_memory({"kind": "agent_feedback", "text": "agent scope", "domain": "auth"})
        result = server.memory_recall({"mode": "agent", "domain": "auth"})
        self.assertFalse(result["isError"], result)
        payload = result["structuredContent"]
        self.assertEqual(payload["mode"], "agent")
        self.assertIn("agent_feedback", payload)
        self.assertIn("hippocampus_entries", payload)
        self.assertIn("context_blocks", payload)
        self.assertIn("recent_logs", payload)

    def test_recall_default_mode_is_startup(self) -> None:
        result = server.memory_recall({})
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["mode"], "startup")


class TokenizationAndScoringTests(MnemoTestCase):
    def test_tokenization_splits_camel_case(self) -> None:
        self.assertIn("validate", server.tokenize("validateInput"))
        self.assertIn("input", server.tokenize("validateInput"))

    def test_tokenization_splits_snake_and_dotted_paths(self) -> None:
        tokens = server.tokenize("src.auth.validate_input")
        self.assertTrue({"src", "auth", "validate", "input"} <= tokens)

    def test_tokenization_handles_mixed_separators(self) -> None:
        tokens = server.tokenize("api/v1:renderView-handler")
        self.assertTrue({"api", "v1", "render", "view", "handler"} <= tokens)

    def test_suffix_stripper_emits_simple_variants(self) -> None:
        self.assertIn("validat", server.tokenize("validating"))
        self.assertIn("command", server.tokenize("commands"))

    def test_tokenization_bridges_validate_and_validating(self) -> None:
        self.assertTrue(server.tokenize("validate") & server.tokenize("validating"))

    def test_tokenization_bridges_create_and_creates_and_created(self) -> None:
        common = server.tokenize("create") & server.tokenize("creates") & server.tokenize("created")
        self.assertTrue(common)

    def test_tokenization_does_not_overstem_short_words(self) -> None:
        self.assertIn("tree", server.tokenize("tree"))
        self.assertIn("time", server.tokenize("time"))
        self.assertIn("like", server.tokenize("like"))
        self.assertIn("the", server.tokenize("the"))

    def test_scoring_short_memory_beats_long_grazing_memory(self) -> None:
        query = server.tokenize("auth")
        short = {"text": "auth", "tags": [], "source": "", "kind": "note"}
        long = {"text": "auth " + " ".join(f"noise{i}" for i in range(100)), "tags": [], "source": "", "kind": "note"}
        self.assertGreater(server.score_memory(query, short), server.score_memory(query, long))

    def test_scoring_tag_exact_match_bonus_applies(self) -> None:
        score = server.score_memory(server.tokenize("auth"), {"text": "", "tags": ["auth"], "source": "", "kind": ""})
        self.assertGreaterEqual(score, 2.0)

    def test_scoring_field_weights_are_respected(self) -> None:
        query = server.tokenize("auth")
        tag = {"text": "", "tags": ["auth"], "source": "", "kind": "note"}
        source = {"text": "", "tags": [], "source": "auth", "kind": "note"}
        self.assertGreater(server.score_memory(query, tag), server.score_memory(query, source))


class MemoryMutationTests(MnemoTestCase):
    def test_exact_duplicate_is_not_appended(self) -> None:
        first = self.record("Run validation before handoff.", kind="decision")
        dup = server.record_memory({"kind": "decision", "text": "  run validation before handoff.  "})
        self.assertFalse(dup["isError"])
        self.assertTrue(dup["structuredContent"]["duplicate"])
        self.assertEqual(dup["structuredContent"]["memory"]["id"], first["id"])
        self.assertEqual(len(self.read_store()["memories"]), 1)

    def test_near_duplicate_is_flagged_and_appended(self) -> None:
        first = self.record("Run validation commands before handoff for PHP frontend edits.", kind="decision")
        second = server.record_memory({"kind": "decision", "text": "Run validation command before handoff for PHP frontend edits."})
        self.assertFalse(second["isError"])
        self.assertIn(first["id"], second["structuredContent"]["near_duplicate_of"])
        self.assertEqual(len(self.read_store()["memories"]), 2)

    def test_update_preserves_id_and_created_at_sets_updated_at(self) -> None:
        memory = self.record("Use auth middleware.", kind="decision")
        result = server.update_memory({"id": memory["id"], "text": "Use the auth middleware.", "tags": ["auth"]})
        self.assertFalse(result["isError"])
        updated = result["structuredContent"]["memory"]
        self.assertEqual(updated["id"], memory["id"])
        self.assertEqual(updated["created_at"], memory["created_at"])
        self.assertIsNotNone(updated["updated_at"])

    def test_delete_hidden_by_default_visible_when_requested(self) -> None:
        memory = self.record("Delete hidden marker.", kind="note")
        deleted = server.delete_memory({"id": memory["id"], "reason": "test"})
        self.assertFalse(deleted["isError"])
        hidden = server.search_memories({"query": "hidden marker"})
        self.assertEqual(hidden["structuredContent"]["matches"], [])
        visible = server.search_memories({"query": "hidden marker", "include_deleted": True})
        self.assertEqual(visible["structuredContent"]["matches"][0]["id"], memory["id"])

    def test_supersede_hides_old_and_links_it(self) -> None:
        old = self.record("Use legacy auth.", kind="decision")
        new = server.record_memory({"kind": "decision", "text": "Use modern auth middleware.", "supersedes": old["id"]})
        self.assertFalse(new["isError"])
        current = server.search_memories({"query": "modern auth"})
        self.assertEqual(current["structuredContent"]["matches"][0]["id"], new["structuredContent"]["memory"]["id"])
        audit = server.search_memories({"query": "legacy auth", "include_superseded": True})
        self.assertEqual(audit["structuredContent"]["matches"][0]["superseded_by"], new["structuredContent"]["memory"]["id"])
        self.assertIsNone(audit["structuredContent"]["matches"][0]["deleted_at"])

    def test_size_cap_archives_only_retired_entries(self) -> None:
        os.environ["MNEMO_MAX_MEMORIES"] = "2"
        active1 = server.new_memory("a1", "note", "active one", "", [])
        active2 = server.new_memory("a2", "note", "active two", "", [])
        deleted = server.new_memory("d1", "note", "deleted one", "", [])
        deleted["deleted_at"] = "2026-01-01T00:00:00Z"
        superseded = server.new_memory("s1", "note", "superseded one", "", [])
        superseded["superseded_by"] = "newer"
        self.write_store([active1, active2, deleted, superseded])
        result = server.record_memory({"text": "overflow", "kind": "note"})
        self.assertTrue(result["isError"])
        self.assertIn("memory cap 2 reached", result["content"][0]["text"])
        archive = self.memory_file.with_name("memory.archive.jsonl")
        self.assertTrue(archive.exists())
        archived_rows = [json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(row["deleted_at"] or row["superseded_by"] for row in archived_rows))
        remaining_ids = {m["id"] for m in self.read_store()["memories"]}
        self.assertTrue({"a1", "a2"} <= remaining_ids)

    def test_size_cap_archive_makes_room(self) -> None:
        os.environ["MNEMO_MAX_MEMORIES"] = "3"
        active = server.new_memory("a1", "note", "active one", "", [])
        deleted = server.new_memory("d1", "note", "deleted one", "", [])
        deleted["created_at"] = "2026-01-01T00:00:00Z"
        deleted["deleted_at"] = "2026-01-02T00:00:00Z"
        superseded = server.new_memory("s1", "note", "superseded one", "", [])
        superseded["created_at"] = "2026-01-03T00:00:00Z"
        superseded["superseded_by"] = "newer"
        self.write_store([active, deleted, superseded])

        result = server.record_memory({"text": "new active", "kind": "note"})

        self.assertFalse(result["isError"], result)
        archive = self.memory_file.with_name("memory.archive.jsonl")
        self.assertTrue(archive.exists())
        store = self.read_store()
        self.assertEqual(len(store["memories"]), 3)
        self.assertIn("new active", {memory["text"] for memory in store["memories"]})

    def test_live_overflow_refuses_cleanly(self) -> None:
        os.environ["MNEMO_MAX_MEMORIES"] = "1"
        self.record("active", kind="note")
        result = server.record_memory({"text": "second active", "kind": "note"})
        self.assertTrue(result["isError"])
        self.assertIn("memory cap 1 reached", result["content"][0]["text"])


class LifecycleSemanticsTests(MnemoTestCase):
    def test_normal_supersede_does_not_mark_deleted(self) -> None:
        old = self.record("lineage legacy marker", kind="decision")
        new = self.record("lineage modern marker", kind="decision", supersedes=old["id"])
        stored_old = next(memory for memory in self.read_store()["memories"] if memory["id"] == old["id"])
        self.assertEqual(stored_old["superseded_by"], new["id"])
        self.assertIsNone(stored_old["deleted_at"])
        self.assertIsNone(stored_old["deletion_reason"])

    def test_explicit_delete_marks_deleted_only(self) -> None:
        memory = self.record("explicit delete marker", kind="note")
        server.delete_memory({"id": memory["id"], "reason": "user removed"})
        stored = self.read_store()["memories"][0]
        self.assertIsNotNone(stored["deleted_at"])
        self.assertIsNone(stored["superseded_by"])
        self.assertEqual(stored["deletion_reason"], "user removed")

    def test_search_flags_separate_deleted_and_superseded(self) -> None:
        active = self.record("state marker active", kind="note")
        deleted = self.record("state marker deleted", kind="note")
        superseded = self.record("state marker superseded", kind="note")
        replacement = self.record("state marker replacement", kind="note", supersedes=superseded["id"])
        server.delete_memory({"id": deleted["id"], "reason": "done"})

        normal = server.search_memories({"query": "state marker", "limit": 10})
        only_superseded = server.search_memories({"query": "state marker", "limit": 10, "include_superseded": True})
        only_deleted = server.search_memories({"query": "state marker", "limit": 10, "include_deleted": True})
        both = server.search_memories(
            {"query": "state marker", "limit": 10, "include_deleted": True, "include_superseded": True}
        )

        normal_ids = {match["id"] for match in normal["structuredContent"]["matches"]}
        superseded_ids = {match["id"] for match in only_superseded["structuredContent"]["matches"]}
        deleted_ids = {match["id"] for match in only_deleted["structuredContent"]["matches"]}
        both_ids = {match["id"] for match in both["structuredContent"]["matches"]}

        self.assertEqual(normal_ids, {active["id"], replacement["id"]})
        self.assertEqual(superseded_ids, {active["id"], replacement["id"], superseded["id"]})
        self.assertEqual(deleted_ids, {active["id"], replacement["id"], deleted["id"]})
        self.assertEqual(both_ids, {active["id"], replacement["id"], deleted["id"], superseded["id"]})

    def test_legacy_superseded_deleted_record_visible_with_superseded_only(self) -> None:
        legacy = server.new_memory("legacy", "note", "legacy superseded marker", "", [])
        legacy["superseded_by"] = "newer"
        legacy["deleted_at"] = "2026-01-01T00:00:00Z"
        legacy["deletion_reason"] = "superseded by newer"
        self.write_store([legacy])
        result = server.search_memories({"query": "legacy superseded marker", "include_superseded": True})
        self.assertEqual(result["structuredContent"]["matches"][0]["id"], "legacy")

    def test_explicitly_deleted_superseded_record_needs_both_flags(self) -> None:
        memory = server.new_memory("both", "note", "both state marker", "", [])
        memory["superseded_by"] = "newer"
        memory["deleted_at"] = "2026-01-01T00:00:00Z"
        memory["deletion_reason"] = "user removed"
        self.write_store([memory])
        superseded_only = server.search_memories({"query": "both state marker", "include_superseded": True})
        both = server.search_memories(
            {"query": "both state marker", "include_superseded": True, "include_deleted": True}
        )
        self.assertEqual(superseded_only["structuredContent"]["matches"], [])
        self.assertEqual(both["structuredContent"]["matches"][0]["id"], "both")

    def test_history_of_superseded_record_has_supersede_not_delete(self) -> None:
        old = self.record("history superseded old", kind="decision")
        new = self.record("history superseded new", kind="decision", supersedes=old["id"])
        result = server.memory_inspect({"id": old["id"], "mode": "history"})
        events = [event["event"] for event in result["structuredContent"]["events"]]
        self.assertEqual(events, ["create", "supersede"])
        self.assertEqual(result["structuredContent"]["events"][-1]["details"]["superseded_by"], new["id"])


class QueryLogAndSymbolTests(MnemoTestCase):
    def test_queries_log_is_written(self) -> None:
        self.record("Log search marker.", kind="note")
        server.search_memories({"query": "marker"})
        log = self.memory_file.with_name("queries.jsonl")
        self.assertTrue(log.exists())
        row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(row["tool"], "mnemo_search")
        self.assertEqual(row["n_results"], 1)

    def test_queries_log_can_be_disabled(self) -> None:
        os.environ["MNEMO_LOG_QUERIES"] = "0"
        self.record("Disabled log marker.", kind="note")
        server.search_memories({"query": "marker"})
        self.assertFalse(self.memory_file.with_name("queries.jsonl").exists())

    def test_queries_log_rotates_once(self) -> None:
        old_cap = server.QUERY_LOG_MAX_BYTES
        server.QUERY_LOG_MAX_BYTES = 10
        try:
            log = self.memory_file.with_name("queries.jsonl")
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("x" * 20, encoding="utf-8")
            server.recent_memories({"limit": 1})
            self.assertTrue(self.memory_file.with_name("queries.1.jsonl").exists())
            self.assertTrue(log.exists())
        finally:
            server.QUERY_LOG_MAX_BYTES = old_cap

    def test_lookup_symbol_finds_known_definitions_and_ignores_node_modules(self) -> None:
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "auth.py").write_text("def authenticate(user):\n    return user\n", encoding="utf-8")
        (self.workspace / "src" / "view.js").write_text("function renderView() {}\n", encoding="utf-8")
        (self.workspace / "src" / "types.ts").write_text("interface UserSession { id: string }\n", encoding="utf-8")
        (self.workspace / "node_modules").mkdir()
        (self.workspace / "node_modules" / "bad.js").write_text("function ignoredSymbol() {}\n", encoding="utf-8")
        self.assertEqual(server.lookup_symbol({"name": "authenticate"})["structuredContent"]["matches"][0]["kind"], "def")
        self.assertEqual(server.lookup_symbol({"name": "renderView"})["structuredContent"]["matches"][0]["kind"], "function")
        self.assertEqual(server.lookup_symbol({"name": "UserSession"})["structuredContent"]["matches"][0]["kind"], "interface")
        self.assertEqual(server.lookup_symbol({"name": "ignoredSymbol"})["structuredContent"]["matches"], [])

    def test_lookup_symbol_returns_cache_hit_within_ttl(self) -> None:
        (self.workspace / "auth.py").write_text("def authenticate(user):\n    return user\n", encoding="utf-8")
        first = server.lookup_symbol({"name": "authenticate"})
        second = server.lookup_symbol({"name": "authenticate"})
        self.assertFalse(first["structuredContent"]["cache_hit"])
        self.assertTrue(second["structuredContent"]["cache_hit"])

    def test_lookup_symbol_ttl_zero_forces_rewalk(self) -> None:
        os.environ["MNEMO_SYMBOL_TTL_SECONDS"] = "0"
        (self.workspace / "auth.py").write_text("def authenticate(user):\n    return user\n", encoding="utf-8")
        first = server.lookup_symbol({"name": "authenticate"})
        second = server.lookup_symbol({"name": "authenticate"})
        self.assertFalse(first["structuredContent"]["cache_hit"])
        self.assertFalse(second["structuredContent"]["cache_hit"])

    def test_lookup_symbol_signature_unchanged_past_ttl_returns_cache_hit(self) -> None:
        os.environ["MNEMO_SYMBOL_TTL_SECONDS"] = "0.01"
        (self.workspace / "auth.py").write_text("def authenticate(user):\n    return user\n", encoding="utf-8")
        first = server.lookup_symbol({"name": "authenticate"})
        time.sleep(0.03)
        second = server.lookup_symbol({"name": "authenticate"})
        self.assertFalse(first["structuredContent"]["cache_hit"])
        self.assertTrue(second["structuredContent"]["cache_hit"])

    def test_lookup_symbol_signature_changed_rebuilds(self) -> None:
        os.environ["MNEMO_SYMBOL_TTL_SECONDS"] = "0.01"
        path = self.workspace / "auth.py"
        path.write_text("def authenticate(user):\n    return user\n", encoding="utf-8")
        first = server.lookup_symbol({"name": "authenticate"})
        time.sleep(0.03)
        path.write_text("def authorize(user):\n    return user\n", encoding="utf-8")
        time.sleep(0.03)
        second = server.lookup_symbol({"name": "authorize"})
        self.assertFalse(first["structuredContent"]["cache_hit"])
        self.assertFalse(second["structuredContent"]["cache_hit"])
        self.assertEqual(second["structuredContent"]["matches"][0]["file"], "auth.py")

    def test_lookup_symbol_skips_non_code_files(self) -> None:
        (self.workspace / "notes.md").write_text("# widgetA\nwidgetA details\n", encoding="utf-8")
        (self.workspace / "package.json").write_text('{"name":"widgetA"}\n', encoding="utf-8")
        result = server.lookup_symbol({"name": "widgetA"})
        self.assertEqual(result["structuredContent"]["matches"], [])
        self.assertEqual(result["structuredContent"]["indexed_files"], 0)

    def test_lookup_symbol_uses_fallback_for_c_files(self) -> None:
        (self.workspace / "lib.c").write_text("int compute_value(int x) { return x; }\n", encoding="utf-8")
        result = server.lookup_symbol({"name": "compute_value"})
        self.assertEqual(result["structuredContent"]["matches"][0]["kind"], "match")
        self.assertEqual(result["structuredContent"]["matches"][0]["file"], "lib.c")

    def test_lookup_symbol_fallback_skips_oversized_files(self) -> None:
        old_cap = server.FALLBACK_MAX_BYTES
        server.FALLBACK_MAX_BYTES = 1024
        try:
            (self.workspace / "large.c").write_text("x" * 1100 + "\nint huge_symbol(void) { return 1; }\n", encoding="utf-8")
            result = server.lookup_symbol({"name": "huge_symbol"})
            self.assertEqual(result["structuredContent"]["matches"], [])
            self.assertEqual(result["structuredContent"]["indexed_files"], 0)
        finally:
            server.FALLBACK_MAX_BYTES = old_cap


class MnemoSafetyLimitTests(MnemoTestCase):
    def test_search_respects_max_search_results_env(self) -> None:
        os.environ["MNEMO_MAX_SEARCH_RESULTS"] = "2"
        for index in range(3):
            self.record(f"search cap marker {index}", kind="note")
        result = server.search_memories({"query": "search cap marker", "limit": 10})
        self.assertEqual(len(result["structuredContent"]["matches"]), 2)

    def test_recent_respects_max_recent_results_env(self) -> None:
        os.environ["MNEMO_MAX_RECENT_RESULTS"] = "2"
        for index in range(3):
            self.record(f"recent cap marker {index}", kind="note")
        result = server.recent_memories({"limit": 10})
        self.assertEqual(len(result["structuredContent"]["memories"]), 2)

    def test_lookup_symbol_skips_large_file_with_warning(self) -> None:
        os.environ["MNEMO_MAX_FILE_BYTES"] = "40"
        server._SYMBOL_CACHE.clear()
        (self.workspace / "large.py").write_text("def huge_symbol():\n    pass\n" + "x" * 100, encoding="utf-8")
        result = server.lookup_symbol({"name": "huge_symbol"})
        structured = result["structuredContent"]
        self.assertEqual(structured["matches"], [])
        self.assertEqual(structured["skipped_files"], 1)
        self.assertTrue(structured["warnings"])

    def test_lookup_symbol_max_files_limit_visible(self) -> None:
        os.environ["MNEMO_MAX_FILES_SCANNED"] = "1"
        server._SYMBOL_CACHE.clear()
        (self.workspace / "a.py").write_text("def first_symbol():\n    pass\n", encoding="utf-8")
        (self.workspace / "b.py").write_text("def second_symbol():\n    pass\n", encoding="utf-8")
        result = server.lookup_symbol({"name": "second_symbol"})
        structured = result["structuredContent"]
        self.assertEqual(structured["indexed_files"], 1)
        self.assertTrue(any("max files" in warning for warning in structured["warnings"]))

    def test_lookup_symbol_max_total_bytes_limit_visible(self) -> None:
        os.environ["MNEMO_MAX_TOTAL_BYTES"] = "35"
        server._SYMBOL_CACHE.clear()
        (self.workspace / "a.py").write_text("def first_symbol():\n    pass\n", encoding="utf-8")
        (self.workspace / "b.py").write_text("def second_symbol():\n    pass\n", encoding="utf-8")
        result = server.lookup_symbol({"name": "second_symbol"})
        structured = result["structuredContent"]
        self.assertTrue(any("max total bytes" in warning for warning in structured["warnings"]))
        self.assertGreaterEqual(structured["skipped_files"], 1)


class PinningTests(MnemoTestCase):
    def test_record_defaults_to_unpinned(self) -> None:
        memory = self.record("normal unpinned memory", kind="note")
        self.assertFalse(memory["pinned"])

    def test_record_can_create_pinned_memory(self) -> None:
        memory = self.record("created pinned memory", kind="invariant", pinned=True)
        self.assertTrue(memory["pinned"])
        result = server.search_memories({"query": "created pinned", "pinned": True})
        self.assertEqual(result["structuredContent"]["matches"][0]["id"], memory["id"])

    def test_record_rejects_invalid_pinned_value(self) -> None:
        result = server.record_memory({"text": "bad pinned value", "pinned": "true"})
        self.assertTrue(result["isError"])
        self.assertIn("pinned must be a boolean", result["content"][0]["text"])

    def test_pinned_memory_gets_score_bonus(self) -> None:
        query = server.tokenize("stable decision")
        base = {"text": "stable decision", "tags": [], "source": "", "kind": "decision"}
        pinned = dict(base, pinned=True)
        self.assertAlmostEqual(server.score_memory(query, pinned) - server.score_memory(query, base), 0.3)

    def test_pinned_memory_is_not_archived_when_retired(self) -> None:
        os.environ["MNEMO_MAX_MEMORIES"] = "2"
        active = server.new_memory("a1", "note", "active", "", [])
        pinned = server.new_memory("p1", "note", "pinned retired", "", [])
        pinned["deleted_at"] = "2026-01-01T00:00:00Z"
        pinned["pinned"] = True
        unpinned = server.new_memory("u1", "note", "unpinned retired", "", [])
        unpinned["deleted_at"] = "2026-01-01T00:00:00Z"
        self.write_store([active, pinned, unpinned])
        result = server.record_memory({"text": "new entry", "kind": "note"})
        self.assertTrue(result["isError"])
        archive_rows = [json.loads(line) for line in self.memory_file.with_name("memory.archive.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["id"] for row in archive_rows], ["u1"])
        self.assertIn("p1", {memory["id"] for memory in self.read_store()["memories"]})

    def test_pinned_filter_excludes_unpinned(self) -> None:
        pinned = self.record("shared marker pinned", kind="note")
        self.record("shared marker unpinned", kind="note")
        server.update_memory({"id": pinned["id"], "pinned": True})
        result = server.search_memories({"query": "shared marker", "pinned": True})
        matches = result["structuredContent"]["matches"]
        self.assertEqual([match["id"] for match in matches], [pinned["id"]])

    def test_memory_update_can_pin_and_unpin(self) -> None:
        memory = self.record("pin target", kind="note")
        pinned = server.update_memory({"id": memory["id"], "pinned": True})
        self.assertTrue(pinned["structuredContent"]["memory"]["pinned"])
        unpinned = server.update_memory({"id": memory["id"], "pinned": False})
        self.assertFalse(unpinned["structuredContent"]["memory"]["pinned"])


class DecayTests(MnemoTestCase):
    def test_decay_reduces_score_for_old_note(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        query = server.tokenize("decay marker")
        recent = server.new_memory("r1", "note", "decay marker", "", [])
        recent["created_at"] = "2026-01-01T00:00:00Z"
        old = server.new_memory("o1", "note", "decay marker", "", [])
        old["created_at"] = "2000-01-01T00:00:00Z"
        self.assertLess(server.score_memory(query, old, now=now), server.score_memory(query, recent, now=now))

    def test_decay_does_not_apply_to_invariant(self) -> None:
        query = server.tokenize("stable invariant")
        recent = server.new_memory("r1", "invariant", "stable invariant", "", [])
        old = server.new_memory("o1", "invariant", "stable invariant", "", [])
        old["created_at"] = "2000-01-01T00:00:00Z"
        self.assertAlmostEqual(server.score_memory(query, old), server.score_memory(query, recent))

    def test_decay_does_not_apply_to_pinned(self) -> None:
        query = server.tokenize("pinned note")
        recent = server.new_memory("r1", "note", "pinned note", "", [])
        recent["pinned"] = True
        old = server.new_memory("o1", "note", "pinned note", "", [])
        old["pinned"] = True
        old["created_at"] = "2000-01-01T00:00:00Z"
        self.assertAlmostEqual(server.score_memory(query, old), server.score_memory(query, recent))

    def test_decay_uses_updated_at_when_present(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        query = server.tokenize("updated note")
        recent = server.new_memory("r1", "note", "updated note", "", [])
        recent["created_at"] = "2026-01-01T00:00:00Z"
        old_updated = server.new_memory("o1", "note", "updated note", "", [])
        old_updated["created_at"] = "2000-01-01T00:00:00Z"
        old_updated["updated_at"] = "2026-01-01T00:00:00Z"
        self.assertAlmostEqual(
            server.score_memory(query, old_updated, now=now),
            server.score_memory(query, recent, now=now),
            places=6,
        )

    def test_decay_disabled_via_env_var(self) -> None:
        os.environ["MNEMO_DECAY"] = "0"
        query = server.tokenize("disabled decay")
        recent = server.new_memory("r1", "note", "disabled decay", "", [])
        old = server.new_memory("o1", "note", "disabled decay", "", [])
        old["created_at"] = "2000-01-01T00:00:00Z"
        self.assertAlmostEqual(server.score_memory(query, old), server.score_memory(query, recent))


class ReferencesTests(MnemoTestCase):
    def test_record_persists_references(self) -> None:
        memory = self.record("reference source", references=["mem_a", "mem_b"])
        self.assertEqual(memory["references"], ["mem_a", "mem_b"])

    def test_update_replaces_references(self) -> None:
        memory = self.record("reference update", references=["mem_a"])
        updated = server.update_memory({"id": memory["id"], "references": ["mem_b", "mem_c"]})
        self.assertEqual(updated["structuredContent"]["memory"]["references"], ["mem_b", "mem_c"])

    def test_compact_context_renders_refs_indicator(self) -> None:
        pinned = self.record("context reference marker pinned", references=["mem_a", "mem_b"])
        server.update_memory({"id": pinned["id"], "pinned": True})
        self.record("context reference marker unpinned", references=["mem_c"])
        result = server.compact_context({"query": "context reference marker"})
        self.assertIn("refs: 2", result["content"][0]["text"])
        self.assertEqual(result["content"][0]["text"].count("★"), 1)


class EventsAndHistoryTests(MnemoTestCase):
    def test_create_event_logged(self) -> None:
        memory = self.record("event create", kind="decision")
        rows = self.read_events()
        self.assertEqual(rows[-1]["event"], "create")
        self.assertEqual(rows[-1]["id"], memory["id"])
        self.assertEqual(rows[-1]["details"]["kind"], "decision")

    def test_update_event_logs_changed_fields(self) -> None:
        memory = self.record("event update", kind="note")
        server.update_memory({"id": memory["id"], "text": "event update changed", "tags": ["event"], "pinned": True})
        changed = self.read_events()[-1]["details"]["changed"]
        self.assertEqual(changed, ["text", "tags", "pinned"])

    def test_supersede_emits_two_events(self) -> None:
        old = self.record("old event memory", kind="decision")
        new = self.record("new event memory", kind="decision", supersedes=old["id"])
        rows = self.read_events()[-2:]
        self.assertEqual(rows[0]["event"], "create")
        self.assertEqual(rows[0]["id"], new["id"])
        self.assertEqual(rows[0]["details"]["supersedes"], old["id"])
        self.assertEqual(rows[1]["event"], "supersede")
        self.assertEqual(rows[1]["id"], old["id"])

    def test_archive_emits_event_per_memory(self) -> None:
        os.environ["MNEMO_MAX_MEMORIES"] = "3"
        active = server.new_memory("a1", "note", "active", "", [])
        retired1 = server.new_memory("r1", "note", "retired one", "", [])
        retired1["deleted_at"] = "2026-01-01T00:00:00Z"
        retired2 = server.new_memory("r2", "note", "retired two", "", [])
        retired2["deleted_at"] = "2026-01-01T00:00:00Z"
        self.write_store([active, retired1, retired2])
        result = server.record_memory({"text": "archive event new", "kind": "note"})
        self.assertFalse(result["isError"], result)
        archive_events = [row for row in self.read_events() if row["event"] == "archive"]
        self.assertEqual(len(archive_events), 1)
        self.assertEqual(archive_events[0]["details"]["archived_to"], "memory.archive.jsonl")

    def test_memory_history_returns_chronological_events(self) -> None:
        memory = self.record("history target", kind="note")
        server.update_memory({"id": memory["id"], "tags": ["history"]})
        result = server.memory_inspect({"id": memory["id"], "mode": "history"})
        events = result["structuredContent"]["events"]
        self.assertEqual([event["event"] for event in events], ["create", "update"])

    def test_event_logging_can_be_disabled(self) -> None:
        os.environ["MNEMO_LOG_EVENTS"] = "0"
        memory = self.record("disabled event logging", kind="note")
        self.assertFalse(self.memory_file.with_name("events.jsonl").exists())
        result = server.memory_inspect({"id": memory["id"], "mode": "history"})
        self.assertEqual(result["structuredContent"]["events"], [])
        self.assertIn("No event log available", result["content"][0]["text"])


class RelatedTests(MnemoTestCase):
    def write_graph(self, memories: list[dict]) -> None:
        self.write_store(memories)

    def test_outgoing_references_returned(self) -> None:
        a = server.new_memory("a", "note", "A", "", ["graph"], ["b"])
        b = server.new_memory("b", "note", "B", "", ["graph"])
        self.write_graph([a, b])
        result = server.memory_inspect({"id": "a", "mode": "related"})
        self.assertEqual(result["structuredContent"]["related"][0]["id"], "b")
        self.assertEqual(result["structuredContent"]["related"][0]["direction"], "outgoing")

    def test_incoming_references_returned(self) -> None:
        a = server.new_memory("a", "note", "A", "", ["graph"], ["b"])
        b = server.new_memory("b", "note", "B", "", ["graph"])
        self.write_graph([a, b])
        result = server.memory_inspect({"id": "b", "mode": "related"})
        self.assertEqual(result["structuredContent"]["related"][0]["id"], "a")
        self.assertEqual(result["structuredContent"]["related"][0]["direction"], "incoming")

    def test_depth_two_walks_chain(self) -> None:
        a = server.new_memory("a", "note", "A", "", [], ["b"])
        b = server.new_memory("b", "note", "B", "", [], ["c"])
        c = server.new_memory("c", "note", "C", "", [])
        self.write_graph([a, b, c])
        result = server.memory_inspect({"id": "a", "mode": "related", "depth": 2})
        self.assertEqual([item["id"] for item in result["structuredContent"]["related"]], ["b", "c"])

    def test_cycle_does_not_infinite_loop(self) -> None:
        a = server.new_memory("a", "note", "A", "", [], ["b"])
        b = server.new_memory("b", "note", "B", "", [], ["a"])
        self.write_graph([a, b])
        result = server.memory_inspect({"id": "a", "mode": "related", "depth": 3})
        self.assertEqual([item["id"] for item in result["structuredContent"]["related"]], ["b"])

    def test_broken_references_skipped(self) -> None:
        a = server.new_memory("a", "note", "A", "", [], ["missing"])
        self.write_graph([a])
        result = server.memory_inspect({"id": "a", "mode": "related"})
        self.assertEqual(result["structuredContent"]["related"], [])

    def test_deleted_excluded_by_default_and_traversed_through(self) -> None:
        a = server.new_memory("a", "note", "A", "", [], ["b"])
        b = server.new_memory("b", "note", "B", "", [], ["c"])
        b["deleted_at"] = "2026-01-01T00:00:00Z"
        c = server.new_memory("c", "note", "C", "", [])
        self.write_graph([a, b, c])
        result = server.memory_inspect({"id": "a", "mode": "related", "depth": 2})
        self.assertEqual([item["id"] for item in result["structuredContent"]["related"]], ["c"])


class PhaseTests(MnemoTestCase):
    def test_infer_phase_detects_debugging_keywords(self) -> None:
        self.assertEqual(server.infer_phase("the test is failing with a traceback"), "debugging")

    def test_infer_phase_detects_implementation_keywords(self) -> None:
        self.assertEqual(server.infer_phase("implement the auth middleware"), "implementation")

    def test_infer_phase_detects_exploration_keywords(self) -> None:
        self.assertEqual(server.infer_phase("where is the user model defined"), "exploration")

    def test_infer_phase_returns_none_on_neutral_query(self) -> None:
        self.assertIsNone(server.infer_phase("hello"))

    def test_phase_bias_boosts_failed_approach_in_debugging(self) -> None:
        query = server.tokenize("auth bug marker")
        memory = server.new_memory("f1", "failed_approach", "auth bug marker", "", [])
        self.assertGreater(server.score_memory(query, memory, "debugging"), server.score_memory(query, memory))

    def test_explicit_phase_none_disables_bias(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        label, phase = server.resolve_phase({"phase": "none"}, "failing traceback")
        query = server.tokenize("auth bug marker")
        memory = server.new_memory("f1", "failed_approach", "auth bug marker", "", [])
        memory["created_at"] = "2026-01-01T00:00:00Z"
        self.assertEqual(label, "none")
        self.assertIsNone(phase)
        explicit_none = server.score_memory(query, memory, phase, now=now)
        omitted = server.score_memory(query, memory, now=now)
        inferred_debugging = server.score_memory(query, memory, "debugging", now=now)
        self.assertAlmostEqual(explicit_none, omitted, places=9)
        self.assertGreater(inferred_debugging, explicit_none)

    def test_inferred_phase_appears_in_structured_content(self) -> None:
        self.record("traceback failing marker", kind="failed_approach")
        result = server.search_memories({"query": "test failing traceback"})
        self.assertEqual(result["structuredContent"]["inferred_phase"], "debugging")


class DriftTests(MnemoTestCase):
    def drift_memory(self, memory_id: str, text: str, created_at: str, kind: str = "note", pinned: bool = False) -> dict:
        memory = server.new_memory(memory_id, kind, text, "", [])
        memory["created_at"] = created_at
        memory["pinned"] = pinned
        return memory

    def test_drift_zero_when_recent_and_older_overlap_completely(self) -> None:
        self.write_store(
            [
                self.drift_memory("m1", "shared alpha beta", "2026-01-01T00:00:00Z"),
                self.drift_memory("m2", "shared alpha beta", "2026-01-02T00:00:00Z"),
                self.drift_memory("m3", "shared alpha beta", "2026-01-03T00:00:00Z"),
                self.drift_memory("m4", "shared alpha beta", "2026-01-04T00:00:00Z"),
            ]
        )
        result = server.mnemo_doctor({})
        self.assertEqual(float(result["structuredContent"]["drift"]["value"]), 0.0)

    def test_drift_high_when_recent_and_older_disjoint(self) -> None:
        self.write_store(
            [
                self.drift_memory("m1", "alpha beta", "2026-01-01T00:00:00Z"),
                self.drift_memory("m2", "alpha beta", "2026-01-02T00:00:00Z"),
                self.drift_memory("m3", "gamma delta", "2026-01-03T00:00:00Z"),
                self.drift_memory("m4", "gamma delta", "2026-01-04T00:00:00Z"),
            ]
        )
        result = server.mnemo_doctor({})
        self.assertGreater(float(result["structuredContent"]["drift"]["value"]), 0.7)
        self.assertEqual(result["structuredContent"]["drift"]["interpretation"], "high")

    def test_drift_excludes_pinned_and_invariant_anchors(self) -> None:
        self.write_store(
            [
                self.drift_memory("p1", "stable anchor", "2026-01-01T00:00:00Z", pinned=True),
                self.drift_memory("i1", "stable anchor", "2026-01-02T00:00:00Z", kind="invariant"),
                self.drift_memory("m1", "legacy alpha", "2026-01-03T00:00:00Z"),
                self.drift_memory("m2", "legacy alpha", "2026-01-04T00:00:00Z"),
                self.drift_memory("m3", "modern beta", "2026-01-05T00:00:00Z"),
                self.drift_memory("m4", "modern beta", "2026-01-06T00:00:00Z"),
            ]
        )
        result = server.mnemo_doctor({})
        self.assertGreater(float(result["structuredContent"]["drift"]["value"]), 0.7)

    def test_drift_returns_zero_when_history_too_small(self) -> None:
        self.write_store(
            [
                self.drift_memory("m1", "alpha", "2026-01-01T00:00:00Z"),
                self.drift_memory("m2", "beta", "2026-01-02T00:00:00Z"),
                self.drift_memory("m3", "gamma", "2026-01-03T00:00:00Z"),
            ]
        )
        result = server.mnemo_doctor({})
        self.assertEqual(float(result["structuredContent"]["drift"]["value"]), 0.0)
        self.assertEqual(result["structuredContent"]["drift"]["interpretation"], "insufficient_history")

    def test_drift_interpretation_buckets(self) -> None:
        self.assertEqual(server.drift_interpretation(0.29), "low")
        self.assertEqual(server.drift_interpretation(0.3), "medium")
        self.assertEqual(server.drift_interpretation(0.71), "high")


class ConsolidateTests(MnemoTestCase):
    def duplicate_memory(self, memory_id: str, kind: str, text: str, created_at: str, pinned: bool = False) -> dict:
        memory = server.new_memory(memory_id, kind, text, "", [])
        memory["created_at"] = created_at
        memory["pinned"] = pinned
        return memory

    def test_consolidate_dry_run_surfaces_cluster_with_survivor(self) -> None:
        self.write_store(
            [
                self.duplicate_memory("old", "decision", "use auth middleware before handling route requests securely", "2026-01-01T00:00:00Z"),
                self.duplicate_memory("new", "decision", "use auth middleware before handling route requests now", "2026-01-02T00:00:00Z"),
            ]
        )
        result = server.memory_maintenance({"action": "consolidate", "dry_run": True})
        cluster = result["structuredContent"]["clusters"][0]
        self.assertFalse(result["structuredContent"]["applied"])
        self.assertEqual(cluster["survivor"], "new")
        self.assertEqual(cluster["to_retire"], ["old"])

    def test_consolidate_skips_pinned_entries(self) -> None:
        self.write_store(
            [
                self.duplicate_memory("pinned", "decision", "use auth middleware before handling route requests securely", "2026-01-01T00:00:00Z", pinned=True),
                self.duplicate_memory("new", "decision", "use auth middleware before handling route requests securely", "2026-01-02T00:00:00Z"),
            ]
        )
        result = server.memory_maintenance({"action": "consolidate", "dry_run": True})
        self.assertEqual(result["structuredContent"]["clusters"], [])

    def test_consolidate_does_not_cluster_across_kinds(self) -> None:
        self.write_store(
            [
                self.duplicate_memory("d1", "decision", "use auth middleware before handling route requests securely", "2026-01-01T00:00:00Z"),
                self.duplicate_memory("n1", "note", "use auth middleware before handling route requests securely", "2026-01-02T00:00:00Z"),
            ]
        )
        result = server.memory_maintenance({"action": "consolidate", "dry_run": True})
        self.assertEqual(result["structuredContent"]["clusters"], [])

    def test_consolidate_apply_writes_supersede_chain_and_events(self) -> None:
        self.write_store(
            [
                self.duplicate_memory("old", "decision", "use auth middleware before handling route requests securely", "2026-01-01T00:00:00Z"),
                self.duplicate_memory("new", "decision", "use auth middleware before handling route requests now", "2026-01-02T00:00:00Z"),
            ]
        )
        result = server.memory_maintenance({"action": "consolidate", "dry_run": False})
        self.assertTrue(result["structuredContent"]["applied"])
        old = next(memory for memory in self.read_store()["memories"] if memory["id"] == "old")
        self.assertEqual(old["superseded_by"], "new")
        self.assertIsNone(old["deleted_at"])
        self.assertIsNone(old["deletion_reason"])
        self.assertEqual(self.read_events()[-1]["event"], "supersede")

    def test_consolidate_threshold_env_var_respected(self) -> None:
        os.environ["MNEMO_CONSOLIDATE_THRESHOLD"] = "0.9"
        self.write_store(
            [
                self.duplicate_memory("old", "decision", "use auth middleware before handling route requests securely", "2026-01-01T00:00:00Z"),
                self.duplicate_memory("new", "decision", "use auth middleware before handling route requests now", "2026-01-02T00:00:00Z"),
            ]
        )
        result = server.memory_maintenance({"action": "consolidate", "dry_run": True})
        self.assertEqual(result["structuredContent"]["clusters"], [])

    def test_consolidate_singletons_not_returned(self) -> None:
        self.write_store([self.duplicate_memory("solo", "decision", "unique decision text that stands alone", "2026-01-01T00:00:00Z")])
        result = server.memory_maintenance({"action": "consolidate", "dry_run": True})
        self.assertEqual(result["structuredContent"]["clusters"], [])


class MaxTokensTests(MnemoTestCase):
    def seed_search_matches(self) -> None:
        self.record("validation handoff alpha " + "x" * 60, kind="note")
        self.record("validation handoff beta " + "y" * 60, kind="note")
        self.record("validation handoff gamma " + "z" * 60, kind="note")

    def test_search_max_tokens_caps_text_but_not_structured_matches(self) -> None:
        self.seed_search_matches()
        result = server.search_memories({"query": "validation handoff", "limit": 3, "max_tokens": 25})
        self.assertTrue(result["structuredContent"]["truncated"])
        self.assertEqual(len(result["structuredContent"]["matches"]), 3)

    def test_search_max_tokens_emits_truncation_marker(self) -> None:
        self.seed_search_matches()
        result = server.search_memories({"query": "validation handoff", "limit": 3, "max_tokens": 25})
        self.assertIn("[truncated:", result["content"][0]["text"])

    def test_search_max_tokens_omitted_means_no_cap(self) -> None:
        self.seed_search_matches()
        result = server.search_memories({"query": "validation handoff", "limit": 3})
        self.assertFalse(result["structuredContent"]["truncated"])
        self.assertNotIn("[truncated:", result["content"][0]["text"])

    def test_search_max_tokens_too_small_emits_header_only(self) -> None:
        self.seed_search_matches()
        result = server.search_memories({"query": "validation handoff", "limit": 3, "max_tokens": 1})
        lines = result["content"][0]["text"].splitlines()
        self.assertEqual(lines[0], "Relevant project memories:")
        self.assertTrue(lines[1].startswith("[truncated:"))

    def test_compact_context_max_tokens_caps_text(self) -> None:
        self.seed_search_matches()
        result = server.compact_context({"query": "validation handoff", "limit": 3, "max_tokens": 20})
        self.assertTrue(result["structuredContent"]["truncated"])
        self.assertEqual(len(result["structuredContent"]["matches"]), 3)

    def test_compact_context_max_tokens_truncation_marker(self) -> None:
        self.seed_search_matches()
        result = server.compact_context({"query": "validation handoff", "limit": 3, "max_tokens": 20})
        self.assertIn("[truncated:", result["content"][0]["text"])


class ConcurrencyTests(MnemoTestCase):
    def rpc(self, proc: subprocess.Popen[str], request: dict) -> dict:
        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    def writer(self, prefix: str, errors: list[BaseException]) -> None:
        env = dict(os.environ)
        proc = subprocess.Popen(
            [sys.executable, str(Path(server.__file__).resolve())],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            self.rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            for i in range(50):
                response = self.rpc(
                    proc,
                    {
                        "jsonrpc": "2.0",
                        "id": i + 2,
                        "method": "tools/call",
                        "params": {
                            "name": "mnemo",
                            "arguments": {
                                "action": "record",
                                "params": {"kind": "note", "text": f"{prefix} concurrent record {i}"},
                            },
                        },
                    },
                )
                if response["result"]["isError"]:
                    raise AssertionError(response)
        except BaseException as exc:
            errors.append(exc)
        finally:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=10)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

    def test_concurrent_writers_produce_valid_unique_records(self) -> None:
        errors: list[BaseException] = []
        t1 = threading.Thread(target=self.writer, args=("a", errors))
        t2 = threading.Thread(target=self.writer, args=("b", errors))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(errors, [])
        memories = self.read_store()["memories"]
        ids = [memory["id"] for memory in memories]
        self.assertEqual(len(memories), 100)
        self.assertEqual(len(set(ids)), 100)


class SeedBootstrapTests(MnemoTestCase):
    def seed_memory(self, memory_id: str, text: str) -> dict:
        return {
            "id": memory_id,
            "kind": "decision",
            "text": text,
            "source": "test",
            "tags": ["seed"],
            "created_at": "2026-05-09T00:00:00Z",
            "updated_at": None,
            "deleted_at": None,
            "deletion_reason": None,
            "superseded_by": None,
        }

    def write_json_store(self, path: Path, memories: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump({"version": 1, "memories": memories}, f)

    def test_first_load_bootstraps_from_example(self) -> None:
        seed = self.seed_memory("seed-a", "Seed A.")
        example = self.memory_file.with_name("memory.example.json")
        self.write_json_store(example, [seed])

        store = server.load_store()

        self.assertTrue(self.memory_file.exists())
        self.assertEqual([memory["id"] for memory in store["memories"]], ["seed-a"])
        self.assertEqual(self.read_store()["memories"], [server.migrate_memory(seed)])

    def test_existing_memory_file_is_not_overwritten_by_example(self) -> None:
        example_seed = self.seed_memory("seed-a", "Seed A.")
        local_seed = self.seed_memory("seed-b", "Seed B.")
        example = self.memory_file.with_name("memory.example.json")
        self.write_json_store(example, [example_seed])
        self.write_json_store(self.memory_file, [local_seed])
        before = self.memory_file.read_text(encoding="utf-8")

        store = server.load_store()

        self.assertEqual([memory["id"] for memory in store["memories"]], ["seed-b"])
        self.assertEqual(self.memory_file.read_text(encoding="utf-8"), before)


class LogArchiveTests(MnemoTestCase):
    def write_rows(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")

    def read_rows(self, path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def force_query_rotation(self) -> None:
        log = self.memory_file.with_name("queries.jsonl")
        self.write_rows(log, [{"tool": "old-current", "pad": "x" * 300}])
        server.append_query_log("mnemo_recent", {"limit": 1}, [])

    def force_event_rotation(self) -> None:
        log = self.memory_file.with_name("events.jsonl")
        self.write_rows(log, [{"event": "old-current", "id": "current", "details": {"pad": "x" * 300}}])
        server.append_event_log("create", "trigger", {"kind": "note"})

    def test_query_log_rotation_appends_to_archive_when_enabled(self) -> None:
        old_cap = server.QUERY_LOG_MAX_BYTES
        server.QUERY_LOG_MAX_BYTES = 256
        try:
            rotated = self.memory_file.with_name("queries.1.jsonl")
            self.write_rows(rotated, [{"tool": "old-rotated", "id": "q1"}])
            self.force_query_rotation()
            archive = self.memory_file.with_name("queries.archive.jsonl")
            self.assertTrue(archive.exists())
            self.assertEqual(self.read_rows(archive)[0]["tool"], "old-rotated")
        finally:
            server.QUERY_LOG_MAX_BYTES = old_cap

    def test_query_log_rotation_skips_archive_when_disabled(self) -> None:
        old_cap = server.QUERY_LOG_MAX_BYTES
        server.QUERY_LOG_MAX_BYTES = 256
        os.environ["MNEMO_LOG_ARCHIVE"] = "0"
        try:
            rotated = self.memory_file.with_name("queries.1.jsonl")
            self.write_rows(rotated, [{"tool": "old-rotated", "id": "q1"}])
            self.force_query_rotation()
            self.assertFalse(self.memory_file.with_name("queries.archive.jsonl").exists())
        finally:
            server.QUERY_LOG_MAX_BYTES = old_cap

    def test_event_log_rotation_appends_to_archive_when_enabled(self) -> None:
        old_cap = server.EVENT_LOG_MAX_BYTES
        server.EVENT_LOG_MAX_BYTES = 256
        try:
            rotated = self.memory_file.with_name("events.1.jsonl")
            self.write_rows(rotated, [{"event": "create", "id": "archived", "details": {"kind": "note"}}])
            self.force_event_rotation()
            archive = self.memory_file.with_name("events.archive.jsonl")
            self.assertTrue(archive.exists())
            self.assertEqual(self.read_rows(archive)[0]["id"], "archived")
        finally:
            server.EVENT_LOG_MAX_BYTES = old_cap

    def test_event_log_rotation_skips_archive_when_disabled(self) -> None:
        old_cap = server.EVENT_LOG_MAX_BYTES
        server.EVENT_LOG_MAX_BYTES = 256
        os.environ["MNEMO_LOG_ARCHIVE"] = "0"
        try:
            rotated = self.memory_file.with_name("events.1.jsonl")
            self.write_rows(rotated, [{"event": "create", "id": "archived", "details": {"kind": "note"}}])
            self.force_event_rotation()
            self.assertFalse(self.memory_file.with_name("events.archive.jsonl").exists())
        finally:
            server.EVENT_LOG_MAX_BYTES = old_cap

    def test_archive_append_preserves_row_order(self) -> None:
        old_cap = server.EVENT_LOG_MAX_BYTES
        server.EVENT_LOG_MAX_BYTES = 256
        try:
            rotated = self.memory_file.with_name("events.1.jsonl")
            self.write_rows(
                rotated,
                [
                    {"event": "create", "id": "first", "details": {"kind": "note"}},
                    {"event": "create", "id": "second", "details": {"kind": "note"}},
                ],
            )
            self.force_event_rotation()
            rows = self.read_rows(self.memory_file.with_name("events.archive.jsonl"))
            self.assertEqual([row["id"] for row in rows], ["first", "second"])
        finally:
            server.EVENT_LOG_MAX_BYTES = old_cap

    def test_memory_history_excludes_archive_by_default(self) -> None:
        archive = self.memory_file.with_name("events.archive.jsonl")
        self.write_rows(archive, [{"ts": "2026-01-01T00:00:00Z", "event": "create", "id": "mem_old", "details": {"kind": "note"}}])
        result = server.memory_inspect({"id": "mem_old", "mode": "history"})
        self.assertEqual(result["structuredContent"]["events"], [])

    def test_memory_history_includes_archive_when_requested(self) -> None:
        archive = self.memory_file.with_name("events.archive.jsonl")
        self.write_rows(archive, [{"ts": "2026-01-01T00:00:00Z", "event": "create", "id": "mem_old", "details": {"kind": "note"}}])
        result = server.memory_inspect({"id": "mem_old", "mode": "history", "include_archive": True})
        self.assertEqual([row["id"] for row in result["structuredContent"]["events"]], ["mem_old"])


class SqliteStoreTests(MnemoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sqlite_file = self.root / "mnemo" / "mnemo.sqlite"
        os.environ["MNEMO_STORE"] = "sqlite"
        os.environ["MNEMO_SQLITE_FILE"] = str(self.sqlite_file)
        server._SQLITE_BOOTSTRAPPED.clear()

    def _write_json_store(self, path: Path, memories: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump({"version": 1, "memories": memories}, f)

    def test_sqlite_record_and_search(self) -> None:
        stored = self.record("sqlite marker alpha", kind="note")
        self.assertTrue(self.sqlite_file.exists())
        result = server.search_memories({"query": "sqlite marker alpha", "limit": 5})
        self.assertFalse(result["isError"], result)
        ids = [item["id"] for item in result["structuredContent"]["matches"]]
        self.assertIn(stored["id"], ids)

    def test_sqlite_record_syncs_fts_index(self) -> None:
        stored = self.record("fts sync marker", kind="note", tags=["fts", "sync"])
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            row = conn.execute("SELECT text, tags FROM memories_fts WHERE id = ?", (stored["id"],)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertIn("fts sync marker", str(row[0]))
        self.assertIn("fts", str(row[1]))

    def test_sqlite_search_falls_back_when_fts_index_missing(self) -> None:
        stored = self.record("fts fallback marker", kind="note")
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            conn.execute("DROP TABLE IF EXISTS memories_fts")
            conn.commit()
        finally:
            conn.close()

        result = server.search_memories({"query": "fts fallback marker", "limit": 5})
        self.assertFalse(result["isError"], result)
        ids = [item["id"] for item in result["structuredContent"]["matches"]]
        self.assertIn(stored["id"], ids)

    def test_sqlite_bootstrap_builds_fts_index_for_seeded_memories(self) -> None:
        seed = {
            "id": "seed-for-fts",
            "kind": "note",
            "text": "seeded fts memory",
            "source": "legacy",
            "tags": ["seed"],
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": None,
            "deleted_at": None,
            "deletion_reason": None,
            "superseded_by": None,
        }
        self._write_json_store(self.memory_file, [seed])
        server.load_store()
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            row = conn.execute("SELECT id FROM memories_fts WHERE id = ?", (seed["id"],)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)

    def test_sqlite_bootstrap_imports_memory_json(self) -> None:
        seed = {
            "id": "legacy-seed",
            "kind": "note",
            "text": "legacy seed",
            "source": "legacy",
            "tags": ["legacy"],
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": None,
            "deleted_at": None,
            "deletion_reason": None,
            "superseded_by": None,
        }
        self._write_json_store(self.memory_file, [seed])
        store = server.load_store()
        self.assertEqual([memory["id"] for memory in store["memories"]], [seed["id"]])
        self.assertTrue(self.sqlite_file.exists())

    def test_sqlite_bootstrap_uses_memory_example_when_missing_memory_json(self) -> None:
        seed = {
            "id": "example-seed",
            "kind": "note",
            "text": "example seed",
            "source": "example",
            "tags": ["example"],
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": None,
            "deleted_at": None,
            "deletion_reason": None,
            "superseded_by": None,
        }
        example = self.memory_file.with_name("memory.example.json")
        self._write_json_store(example, [seed])
        store = server.load_store()
        self.assertEqual([memory["id"] for memory in store["memories"]], [seed["id"]])
        self.assertTrue(self.sqlite_file.exists())

    def test_sqlite_mode_query_events_go_to_sqlite_not_jsonl(self) -> None:
        self.record("query events marker", kind="note")
        result = server.search_memories({"query": "query events marker"})
        self.assertFalse(result["isError"], result)
        self.assertFalse(self.memory_file.with_name("queries.jsonl").exists())
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            row = conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'query'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertGreater(int(row[0]), 0)

    def test_sqlite_bootstrap_ingests_legacy_event_and_query_logs(self) -> None:
        self.memory_file.parent.mkdir(parents=True, exist_ok=True)
        self.memory_file.with_name("events.jsonl").write_text(
            json.dumps({"ts": "2026-01-01T00:00:00Z", "event": "create", "id": "mem_evt", "details": {"kind": "note"}})
            + "\n",
            encoding="utf-8",
        )
        self.memory_file.with_name("queries.jsonl").write_text(
            json.dumps({"ts": "2026-01-01T00:00:10Z", "tool": "mnemo_search", "query": "evt"}) + "\n",
            encoding="utf-8",
        )
        server.load_store()
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            non_query = conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'create'").fetchone()
            query = conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'query'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(non_query)
        self.assertIsNotNone(query)
        self.assertGreaterEqual(int(non_query[0]), 1)
        self.assertGreaterEqual(int(query[0]), 1)

    def test_sqlite_mode_does_not_write_new_jsonl_logs(self) -> None:
        memory = self.record("sqlite no jsonl logs", kind="note")
        self.assertIsNotNone(memory["id"])
        server.search_memories({"query": "sqlite no jsonl logs"})
        self.assertFalse(self.memory_file.with_name("events.jsonl").exists())
        self.assertFalse(self.memory_file.with_name("queries.jsonl").exists())

    def test_mnemo_export_writes_readable_files(self) -> None:
        self.record("hippocampus export marker", kind="hippocampus_entry")
        self.record("feedback export marker", kind="agent_feedback", role="coordinator")
        jsonl_result = server.memory_export({"format": "jsonl"})
        hippo_result = server.memory_export({"format": "hippocampus_markdown"})
        feedback_result = server.memory_export({"format": "agent_feedback_markdown"})
        self.assertFalse(jsonl_result["isError"], jsonl_result)
        self.assertFalse(hippo_result["isError"], hippo_result)
        self.assertFalse(feedback_result["isError"], feedback_result)
        self.assertTrue((self.root / "mnemo" / "exports" / "memory.jsonl").exists())
        self.assertTrue((self.root / "mnemo" / "exports" / "hippocampus.md").exists())
        self.assertTrue((self.root / "mnemo" / "exports" / "agent_feedback.md").exists())

    def test_sqlite_maintenance_import_json(self) -> None:
        import_path = self.root / "imports" / "legacy_memories.json"
        payload = {
            "version": 1,
            "memories": [
                {
                    "id": "import-one",
                    "kind": "decision",
                    "text": "imported decision",
                    "source": "import",
                    "tags": ["import"],
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ],
        }
        import_path.parent.mkdir(parents=True, exist_ok=True)
        import_path.write_text(json.dumps(payload), encoding="utf-8")
        result = server.memory_maintenance({"action": "import_json", "path": str(import_path), "dry_run": False})
        self.assertFalse(result["isError"], result)
        structured = result["structuredContent"]
        self.assertEqual(structured["imported_count"], 1)
        ids = [memory["id"] for memory in self.read_store()["memories"]]
        self.assertIn("import-one", ids)

    def test_mnemo_get_full_returns_complete_text(self) -> None:
        text = "full retrieval marker " + ("x" * 1600)
        memory = self.record(text, kind="context_block")
        result = server.memory_get({"id": memory["id"], "full": True})
        self.assertFalse(result["isError"], result)
        got = result["structuredContent"]["memory"]
        self.assertEqual(got["id"], memory["id"])
        self.assertEqual(got["text"], text)

    def test_gateway_tool_count_sqlite_mode(self) -> None:
        os.environ["MNEMO_MCP_PROFILE"] = "core"
        self.assertEqual(len(server.copilot_safe_tools()), 1)
        os.environ["MNEMO_MCP_PROFILE"] = "full"
        self.assertEqual(len(server.copilot_safe_tools()), 1)

    def test_doctor_reports_sqlite_backend(self) -> None:
        self.record("doctor sqlite marker", kind="note")
        result = server.mnemo_doctor({})
        self.assertFalse(result["isError"], result)
        structured = result["structuredContent"]
        self.assertEqual(structured["backend"], "sqlite")
        self.assertTrue(structured["sqlite_file_exists"])
        self.assertIn("count_by_kind", structured)
        self.assertIn("export_files", structured)
        self.assertIn("fts_available", structured)
        self.assertIn("search_backend", structured)
        self.assertIn("event_count", structured)
        self.assertIn("events_fts_enabled", structured)

    def test_events_schema_typed_columns_and_migration_idempotent(self) -> None:
        self.record("typed event schema marker", kind="note")
        expected = {
            "event_id",
            "ts",
            "action",
            "memory_id",
            "source_id",
            "target_id",
            "relation",
            "query_text",
            "result_count",
            "top_score",
            "success",
            "agent_id",
            "role",
            "domain",
            "kind",
            "summary",
            "salience_text",
            "include_in_salience",
            "data_json",
        }
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        finally:
            conn.close()
        self.assertTrue(expected.issubset(cols))
        # Migration should stay idempotent on repeated loads.
        server.load_store()
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            cols2 = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        finally:
            conn.close()
        self.assertTrue(expected.issubset(cols2))

    def test_recent_events_returns_compact_rows(self) -> None:
        self.record("recent events marker", kind="note", domain="payments", role="specialist", agent_id="spec_pay")
        server.search_memories({"query": "recent events marker", "limit": 3})
        result = server.recent_events({"limit": 20})
        self.assertFalse(result["isError"], result)
        events = result["structuredContent"]["events"]
        self.assertGreater(len(events), 0)
        first = events[0]
        self.assertIn("event_id", first)
        self.assertIn("timestamp", first)
        self.assertIn("action", first)

    def test_search_events_returns_matches(self) -> None:
        self.record("IBAN validation historical note", kind="note", domain="payments")
        server.search_memories({"query": "IBAN validation", "limit": 5})
        result = server.search_events({"query": "IBAN validation", "limit": 20})
        self.assertFalse(result["isError"], result)
        events = result["structuredContent"]["events"]
        self.assertGreater(len(events), 0)
        actions = {str(item.get("action")) for item in events}
        self.assertTrue("mnemo_search" in actions or "query" in actions)

    def test_get_event_returns_full_detail(self) -> None:
        self.record("get event marker", kind="note")
        recent = server.recent_events({"limit": 1})
        self.assertFalse(recent["isError"], recent)
        event_id = recent["structuredContent"]["events"][0]["event_id"]
        result = server.get_event({"event_id": event_id})
        self.assertFalse(result["isError"], result)
        payload = result["structuredContent"]["event"]
        self.assertEqual(payload["event_id"], event_id)
        self.assertIn("data", payload)

    def test_memory_events_returns_memory_scoped_rows(self) -> None:
        memory = self.record("memory scoped events marker", kind="note")
        server.update_memory({"id": memory["id"], "text": "memory scoped events marker updated"})
        result = server.memory_events({"memory_id": memory["id"], "limit": 50})
        self.assertFalse(result["isError"], result)
        events = result["structuredContent"]["events"]
        self.assertGreater(len(events), 0)
        self.assertTrue(all(item.get("memory_id") == memory["id"] for item in events))
        actions = {str(item.get("action")) for item in events}
        self.assertIn("create", actions)
        self.assertIn("update", actions)

    def test_search_events_fts_fallback_when_unavailable(self) -> None:
        self.record("fts fallback event marker", kind="note")
        server.search_memories({"query": "fts fallback event marker", "limit": 5})
        with mock.patch("server._sqlite_events_fts_flag", return_value=False):
            result = server.search_events({"query": "fallback event marker", "limit": 20})
        self.assertFalse(result["isError"], result)
        self.assertGreater(len(result["structuredContent"]["events"]), 0)

    def test_typed_event_columns_populated(self) -> None:
        memory = self.record("typed population marker", kind="note", domain="auth", role="specialist", agent_id="spec_auth")
        server.search_memories({"query": "typed population marker", "limit": 5})
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            row = conn.execute(
                """
                SELECT action, query_text, result_count, success, top_score, salience_text, include_in_salience
                FROM events
                WHERE event_type = 'query'
                ORDER BY rowid DESC
                LIMIT 1
                """
            ).fetchone()
            create_row = conn.execute(
                "SELECT kind, domain, role, agent_id FROM events WHERE memory_id = ? AND event_type = 'create' LIMIT 1",
                (memory["id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertIn("mnemo_search", str(row[0]))
        self.assertIn("typed population marker", str(row[1]))
        self.assertGreaterEqual(int(row[2]), 0)
        self.assertIn(int(row[3]), {0, 1})
        self.assertGreaterEqual(float(row[4]), 0.0)
        self.assertTrue(str(row[5]))
        self.assertIn(int(row[6]), {0, 1})
        self.assertIsNotNone(create_row)
        assert create_row is not None
        self.assertEqual(str(create_row[0]), "note")
        self.assertEqual(str(create_row[1]), "auth")
        self.assertEqual(str(create_row[2]), "specialist")
        self.assertEqual(str(create_row[3]), "spec_auth")

    def test_doctor_reports_event_stats(self) -> None:
        self.record("doctor event stats marker", kind="note")
        server.search_memories({"query": "doctor event stats marker", "limit": 3})
        result = server.mnemo_doctor({})
        self.assertFalse(result["isError"], result)
        structured = result["structuredContent"]
        self.assertGreaterEqual(int(structured["event_count"]), 1)
        self.assertGreaterEqual(int(structured["recent_event_count"]), 1)
        self.assertIn("events_fts_enabled", structured)
        events_log = structured["events_log"]
        self.assertIn("event_count", events_log)
        self.assertIn("recent_event_count", events_log)
        self.assertIn("events_fts_enabled", events_log)

    def test_existing_record_search_get_behavior_unaffected(self) -> None:
        memory = self.record("existing behavior marker", kind="decision")
        searched = server.search_memories({"query": "existing behavior marker", "limit": 5})
        self.assertFalse(searched["isError"], searched)
        ids = [item["id"] for item in searched["structuredContent"]["matches"]]
        self.assertIn(memory["id"], ids)
        got = server.memory_get({"id": memory["id"], "full": True})
        self.assertFalse(got["isError"], got)
        self.assertEqual(got["structuredContent"]["memory"]["id"], memory["id"])


class IdfActivationTests(MnemoTestCase):
    class _FakeBreakdown:
        def __init__(
            self,
            *,
            cosine: float,
            jaccard: float,
            idf_cosine: float,
            idf_jaccard: float,
            final: float,
            weights: dict[str, float],
            idf_status: str,
            idf_used: bool,
        ) -> None:
            self.cosine = cosine
            self.jaccard = jaccard
            self.idf_cosine = idf_cosine
            self.idf_jaccard = idf_jaccard
            self.repetition = 0.0
            self.recency = 0.0
            self.novelty = 0.0
            self.drift = 0.0
            self.final = final
            self.weights = weights
            self.idf_status = idf_status
            self.idf_used = idf_used

        def to_dict(self) -> dict:
            return {
                "cosine": self.cosine,
                "jaccard": self.jaccard,
                "idf_cosine": self.idf_cosine,
                "idf_jaccard": self.idf_jaccard,
                "repetition": self.repetition,
                "recency": self.recency,
                "novelty": self.novelty,
                "drift": self.drift,
                "final": self.final,
                "weights": self.weights,
                "idf_status": self.idf_status,
                "idf_used": self.idf_used,
            }

    class _FakeIdfProfile:
        def __init__(
            self,
            *,
            domain: str | None,
            doc_count: int,
            unique_terms: int,
            total_tokens: int,
            min_documents: int,
            min_unique_terms: int,
            min_total_tokens: int,
            idf_values: dict[str, float],
        ) -> None:
            ready = (
                doc_count >= min_documents
                and unique_terms >= min_unique_terms
                and total_tokens >= min_total_tokens
            )
            self._payload = {
                "domain": domain,
                "doc_count": doc_count,
                "unique_terms": unique_terms,
                "total_tokens": total_tokens,
                "status": "ready" if ready else "cold",
                "ready": ready,
                "idf": idf_values,
                "min_documents": min_documents,
                "min_unique_terms": min_unique_terms,
                "min_total_tokens": min_total_tokens,
                "version": 1,
            }

        def to_dict(self) -> dict:
            return dict(self._payload)

    class _FakeSalience:
        __version__ = "0.2.fake"
        __file__ = "fake_agent_salience.py"

        def __init__(self) -> None:
            self.build_calls = 0
            self.signal_calls: list[dict] = []

        def _tokens(self, text: str) -> list[str]:
            return [token for token in text.lower().split() if token]

        def build_idf_profile(
            self,
            documents,
            *,
            domain=None,
            min_documents=200,
            min_unique_terms=1000,
            min_total_tokens=10000,
        ):
            import math

            self.build_calls += 1
            docs = [self._tokens(str(doc)) for doc in documents if str(doc).strip()]
            doc_count = len(docs)
            total_tokens = sum(len(doc) for doc in docs)
            doc_freq: dict[str, int] = {}
            for doc in docs:
                for token in set(doc):
                    doc_freq[token] = doc_freq.get(token, 0) + 1
            unique_terms = len(doc_freq)
            idf_values = {
                token: math.log((1.0 + doc_count) / (1.0 + freq)) + 1.0
                for token, freq in sorted(doc_freq.items())
            }
            return IdfActivationTests._FakeIdfProfile(
                domain=domain,
                doc_count=doc_count,
                unique_terms=unique_terms,
                total_tokens=total_tokens,
                min_documents=int(min_documents),
                min_unique_terms=int(min_unique_terms),
                min_total_tokens=int(min_total_tokens),
                idf_values=idf_values,
            )

        def signal_score(self, source_text: str, target_text: str, **kwargs):
            self.signal_calls.append(dict(kwargs))
            source = set(self._tokens(source_text))
            target = set(self._tokens(target_text))
            intersection = source & target
            union = source | target
            overlap = 1.0 if intersection else 0.0
            mode = str(kwargs.get("mode", "lexical"))
            idf_profile = kwargs.get("idf_profile")
            idf_status = "not_requested"
            idf_used = False
            idf_cosine = 0.0
            idf_jaccard = 0.0
            cosine = float(len(intersection) / ((len(source) * len(target)) ** 0.5)) if source and target else 0.0
            jaccard = float(len(intersection) / len(union)) if union else 0.0
            if mode in {"auto", "idf"}:
                if isinstance(idf_profile, dict):
                    idf_status = str(idf_profile.get("status", "cold"))
                    idf_used = idf_status == "ready"
                    if idf_used:
                        idf_map = {str(k): float(v) for k, v in dict(idf_profile.get("idf", {})).items()}
                        unknown = min(idf_map.values()) * 0.5 if idf_map else 0.1
                        def _w(term: str) -> float:
                            return float(idf_map.get(term, unknown))
                        source_vec = {t: _w(t) for t in source}
                        target_vec = {t: _w(t) for t in target}
                        dot = sum(source_vec[t] * target_vec[t] for t in intersection)
                        left_norm = sum(v * v for v in source_vec.values()) ** 0.5
                        right_norm = sum(v * v for v in target_vec.values()) ** 0.5
                        idf_cosine = (dot / (left_norm * right_norm)) if left_norm > 0 and right_norm > 0 else 0.0
                        idf_num = sum(_w(t) for t in intersection)
                        idf_den = sum(_w(t) for t in union)
                        idf_jaccard = (idf_num / idf_den) if idf_den > 0 else 0.0
                else:
                    idf_status = "missing"
            weights = kwargs.get("weights")
            if isinstance(weights, dict):
                w_cos = float(weights.get("cosine", 0.0))
                w_jac = float(weights.get("jaccard", 0.0))
                w_idf_cos = float(weights.get("idf_cosine", 0.0))
                w_idf_jac = float(weights.get("idf_jaccard", 0.0))
                total = w_cos + w_jac + w_idf_cos + w_idf_jac
                if total <= 0:
                    final = 0.0
                    norm = {"cosine": 0.0, "jaccard": 0.0, "idf_cosine": 0.0, "idf_jaccard": 0.0}
                else:
                    norm = {
                        "cosine": w_cos / total,
                        "jaccard": w_jac / total,
                        "idf_cosine": w_idf_cos / total,
                        "idf_jaccard": w_idf_jac / total,
                    }
                    final = (
                        cosine * norm["cosine"]
                        + jaccard * norm["jaccard"]
                        + idf_cosine * norm["idf_cosine"]
                        + idf_jaccard * norm["idf_jaccard"]
                    )
            else:
                norm = {"cosine": 0.7, "jaccard": 0.3, "idf_cosine": 0.0, "idf_jaccard": 0.0}
                final = (cosine * 0.7) + (jaccard * 0.3)
            return IdfActivationTests._FakeBreakdown(
                cosine=cosine,
                jaccard=jaccard,
                idf_cosine=idf_cosine,
                idf_jaccard=idf_jaccard,
                final=final,
                weights=norm,
                idf_status=idf_status,
                idf_used=idf_used,
            )

        def drift_score(self, _source_text: str, _target_text: str) -> float:
            return 0.0

    def setUp(self) -> None:
        super().setUp()
        self.sqlite_file = self.root / "mnemo" / "mnemo.sqlite"
        self.sqlite_file.parent.mkdir(parents=True, exist_ok=True)
        os.environ["MNEMO_STORE"] = "sqlite"
        os.environ["MNEMO_SQLITE_FILE"] = str(self.sqlite_file)
        server._SQLITE_BOOTSTRAPPED.clear()

    def _record_many(self, count: int, *, domain: str | None = None, prefix: str = "idf") -> None:
        for idx in range(count):
            self.record(
                f"{prefix} memory {idx} contains enough repeated tokens for idf activation checks",
                kind="note",
                domain=domain,
            )

    def test_doctor_reports_idf_object_even_when_cold(self) -> None:
        result = server.mnemo_doctor({})
        self.assertFalse(result["isError"], result)
        payload = result["structuredContent"]
        self.assertIn("idf", payload)
        self.assertIn("project", payload["idf"])
        self.assertIn(payload["idf"]["project"]["status"], {"cold", "unavailable", "disabled"})

    def test_cold_corpus_keeps_project_idf_inactive(self) -> None:
        fake = self._FakeSalience()
        with mock.patch("server.load_optional_agent_salience", return_value=(fake, None)):
            self.record("short corpus sample for cold idf behavior with five tokens", kind="note")
            doctor = server.mnemo_doctor({})
        project = doctor["structuredContent"]["idf"]["project"]
        self.assertEqual(project["status"], "cold")
        self.assertFalse(project["active"])

    def test_low_thresholds_activate_project_idf(self) -> None:
        os.environ["MNEMO_IDF_MIN_DOCUMENTS"] = "1"
        os.environ["MNEMO_IDF_MIN_UNIQUE_TERMS"] = "1"
        os.environ["MNEMO_IDF_MIN_TOTAL_TOKENS"] = "1"
        fake = self._FakeSalience()
        with mock.patch("server.load_optional_agent_salience", return_value=(fake, None)):
            self.record("project idf activation test data with enough lexical material", kind="note")
            doctor = server.mnemo_doctor({})
        project = doctor["structuredContent"]["idf"]["project"]
        self.assertEqual(project["status"], "ready")
        self.assertTrue(project["active"])

    def test_idf_mode_off_disables_even_if_mature(self) -> None:
        os.environ["MNEMO_IDF_MODE"] = "off"
        os.environ["MNEMO_IDF_MIN_DOCUMENTS"] = "1"
        os.environ["MNEMO_IDF_MIN_UNIQUE_TERMS"] = "1"
        os.environ["MNEMO_IDF_MIN_TOTAL_TOKENS"] = "1"
        fake = self._FakeSalience()
        with mock.patch("server.load_optional_agent_salience", return_value=(fake, None)):
            self.record("idf off mode should remain disabled regardless of corpus maturity", kind="note")
            doctor = server.mnemo_doctor({})
        project = doctor["structuredContent"]["idf"]["project"]
        self.assertEqual(project["status"], "disabled")
        self.assertFalse(project["active"])

    def test_idf_mode_off_salience_check_keeps_lexical_path(self) -> None:
        os.environ["MNEMO_IDF_MODE"] = "off"
        os.environ["MNEMO_IDF_MIN_DOCUMENTS"] = "1"
        os.environ["MNEMO_IDF_MIN_UNIQUE_TERMS"] = "1"
        os.environ["MNEMO_IDF_MIN_TOTAL_TOKENS"] = "1"
        fake = self._FakeSalience()
        with mock.patch("server.load_optional_agent_salience", return_value=(fake, None)):
            self.record("auth middleware guard clause before route handlers", kind="decision")
            result = server.memory_salience_check({"text": "auth middleware guard clause", "limit": 5})
        self.assertFalse(result["isError"], result)
        structured = result["structuredContent"]
        self.assertFalse(structured["idf_used"])
        self.assertEqual(structured["idf_scope_used"], "none")
        self.assertEqual(structured["idf_profile_status"], "disabled")

    def test_missing_salience_reports_unavailable_without_crash(self) -> None:
        with mock.patch("server.load_optional_agent_salience", return_value=(None, "forced missing")):
            doctor = server.mnemo_doctor({})
        self.assertFalse(doctor["isError"], doctor)
        idf_payload = doctor["structuredContent"]["idf"]
        self.assertFalse(idf_payload["available"])
        self.assertEqual(idf_payload["project"]["status"], "unavailable")

    def test_domain_profiles_activate_independently(self) -> None:
        os.environ["MNEMO_IDF_MIN_DOCUMENTS"] = "20"
        os.environ["MNEMO_IDF_MIN_UNIQUE_TERMS"] = "1000"
        os.environ["MNEMO_IDF_MIN_TOTAL_TOKENS"] = "10000"
        os.environ["MNEMO_IDF_DOMAIN_MIN_DOCUMENTS"] = "2"
        os.environ["MNEMO_IDF_DOMAIN_MIN_UNIQUE_TERMS"] = "1"
        os.environ["MNEMO_IDF_DOMAIN_MIN_TOTAL_TOKENS"] = "1"
        fake = self._FakeSalience()
        with mock.patch("server.load_optional_agent_salience", return_value=(fake, None)):
            self._record_many(2, domain="auth", prefix="auth")
            self._record_many(1, domain="billing", prefix="billing")
            doctor = server.mnemo_doctor({})
        domains = doctor["structuredContent"]["idf"]["domains"]
        self.assertIn("auth", domains)
        self.assertIn("billing", domains)
        self.assertTrue(domains["auth"]["active"])
        self.assertFalse(domains["billing"]["active"])
        self.assertFalse(doctor["structuredContent"]["idf"]["project"]["active"])

    def test_salience_check_uses_active_idf_profile(self) -> None:
        os.environ["MNEMO_IDF_MIN_DOCUMENTS"] = "1"
        os.environ["MNEMO_IDF_MIN_UNIQUE_TERMS"] = "1"
        os.environ["MNEMO_IDF_MIN_TOTAL_TOKENS"] = "1"
        fake = self._FakeSalience()
        with mock.patch("server.load_optional_agent_salience", return_value=(fake, None)):
            self.record("auth middleware marker token set for idf scoring pathway", kind="decision", domain="auth")
            result = server.memory_salience_check({"text": "auth middleware marker", "domain": "auth", "limit": 5})
        self.assertFalse(result["isError"], result)
        structured = result["structuredContent"]
        self.assertTrue(structured["idf_used"])
        self.assertEqual(structured["idf_scope_used"], "project")
        self.assertEqual(structured["idf_profile_status"], "ready")
        self.assertEqual(structured["idf"]["status"], "ready")
        self.assertIn("idf_jaccard", structured["score_breakdown"])
        self.assertGreaterEqual(float(structured["score_breakdown"]["idf_jaccard"]), 0.0)
        self.assertIn("idf_jaccard", structured["score_weights"])
        self.assertGreater(len(fake.signal_calls), 0)
        self.assertEqual(fake.signal_calls[-1].get("mode"), "auto")
        self.assertIsNotNone(fake.signal_calls[-1].get("idf_profile"))
        self.assertIn("idf_jaccard", fake.signal_calls[-1].get("weights", {}))

    def test_false_positive_pair_scores_low_with_active_idf(self) -> None:
        os.environ["MNEMO_IDF_MIN_DOCUMENTS"] = "1"
        os.environ["MNEMO_IDF_MIN_UNIQUE_TERMS"] = "1"
        os.environ["MNEMO_IDF_MIN_TOTAL_TOKENS"] = "1"
        fake = self._FakeSalience()
        with mock.patch("server.load_optional_agent_salience", return_value=(fake, None)):
            for idx in range(12):
                self.record(
                    f"and shared common filler token{idx} repeated for idf maturity",
                    kind="note",
                    domain="core",
                )
            self.record("MCP server entrypoint and tests", kind="decision", domain="core")
            self.record("Discuss apartment prices and kindergarten logistics", kind="note", domain="core")
            result = server.memory_salience_check(
                {
                    "text": "MCP server entrypoint and tests",
                    "limit": 50,
                    "domain": "core",
                    "use_fts": False,
                    "candidate_limit": 100,
                    "max_scored": 100,
                    "shingle_overlap_threshold": 0.0,
                }
            )
        self.assertFalse(result["isError"], result)
        structured = result["structuredContent"]
        self.assertTrue(structured["idf_used"])
        self.assertEqual(structured["idf_profile_status"], "ready")
        unrelated = next(
            (
                item
                for item in structured["matches"]
                if "apartment prices and kindergarten logistics" in item["text_preview"].lower()
            ),
            None,
        )
        self.assertIsNotNone(unrelated)
        assert unrelated is not None
        breakdown = unrelated["breakdown"]
        self.assertIn("idf_jaccard", breakdown)
        self.assertLess(float(breakdown["idf_jaccard"]), 0.10)
        self.assertLess(float(unrelated["score"]), 0.10)

    def test_cached_profile_reused_when_signature_unchanged(self) -> None:
        os.environ["MNEMO_IDF_MIN_DOCUMENTS"] = "1"
        os.environ["MNEMO_IDF_MIN_UNIQUE_TERMS"] = "1"
        os.environ["MNEMO_IDF_MIN_TOTAL_TOKENS"] = "1"
        fake = self._FakeSalience()
        with mock.patch("server.load_optional_agent_salience", return_value=(fake, None)):
            self.record("cached idf reuse test with stable corpus signature", kind="note")
            first = server.mnemo_doctor({})
            self.assertFalse(first["isError"], first)
            build_calls_after_first = fake.build_calls
            second = server.mnemo_doctor({})
            self.assertFalse(second["isError"], second)
        self.assertGreater(build_calls_after_first, 0)
        self.assertEqual(fake.build_calls, build_calls_after_first)


class SignatureHelpersTests(MnemoTestCase):
    def test_stable_hash_hex_determinism(self) -> None:
        h1 = server._stable_hash_hex("hello world")
        h2 = server._stable_hash_hex("hello world")
        self.assertEqual(h1, h2)
        h3 = server._stable_hash_hex("hello world!")
        self.assertNotEqual(h1, h3)

    def test_stable_hash_hex_is_blake2b_not_builtin(self) -> None:
        import hashlib
        expected = hashlib.blake2b("hello".encode("utf-8"), digest_size=8).hexdigest()
        self.assertEqual(server._stable_hash_hex("hello"), expected)

    def test_stable_hash_hex_length_is_16(self) -> None:
        h = server._stable_hash_hex("test value")
        self.assertEqual(len(h), 16)

    def test_build_memory_signature_has_all_fields(self) -> None:
        sig = server._build_memory_signature("auth middleware route handler checks")
        expected_keys = {
            "content_hash", "normalized_hash", "token_count", "unique_token_count",
            "top_terms_json", "shingle_hashes_json", "signature_version",
            "normalizer_version", "signature_updated_at",
        }
        self.assertEqual(set(sig.keys()), expected_keys)

    def test_build_memory_signature_token_counts_correct(self) -> None:
        sig = server._build_memory_signature("apple apple banana cherry")
        self.assertGreater(sig["token_count"], 0)
        self.assertLessEqual(sig["unique_token_count"], sig["token_count"])

    def test_build_memory_signature_truncation(self) -> None:
        long_text = "token " * 20_000  # 120000 chars, well over MAX
        sig_long = server._build_memory_signature(long_text)
        sig_long2 = server._build_memory_signature(long_text + " extra stuff that is beyond cap")
        # Token-based signatures are capped for safety, but content_hash covers full raw text.
        self.assertNotEqual(sig_long["content_hash"], sig_long2["content_hash"])
        self.assertEqual(sig_long["normalized_hash"], sig_long2["normalized_hash"])
        self.assertEqual(sig_long["shingle_hashes_json"], sig_long2["shingle_hashes_json"])

    def test_content_hash_uses_full_raw_text_beyond_signature_cap(self) -> None:
        prefix = "x" * server.MAX_SIGNATURE_TEXT_CHARS
        sig_a = server._build_memory_signature(prefix + " A")
        sig_b = server._build_memory_signature(prefix + " B")
        self.assertNotEqual(sig_a["content_hash"], sig_b["content_hash"])

    def test_build_memory_signature_top_terms_sorted_by_freq_desc(self) -> None:
        # "apple" appears 5x, "banana" 3x, "cherry" 1x
        sig = server._build_memory_signature("apple apple apple apple apple banana banana banana cherry")
        top = json.loads(sig["top_terms_json"])
        self.assertTrue(len(top) > 0)
        # apple should appear before banana (higher freq)
        if "apple" in top and "banana" in top:
            self.assertLess(top.index("apple"), top.index("banana"))
        if "banana" in top and "cherry" in top:
            self.assertLess(top.index("banana"), top.index("cherry"))

    def test_build_word_shingles_returns_empty_when_too_short(self) -> None:
        self.assertEqual(server._build_word_shingles(["a", "b"], n=3), [])
        self.assertEqual(server._build_word_shingles([], n=3), [])

    def test_build_word_shingles_count(self) -> None:
        tokens = ["a", "b", "c", "d"]
        shingles = server._build_word_shingles(tokens, n=3)
        self.assertEqual(len(shingles), 2)  # "a b c", "b c d"

    def test_build_min_k_shingle_hashes_sorted_ascending(self) -> None:
        tokens = ["the", "quick", "brown", "fox", "jumps", "over"]
        hashes = server._build_min_k_shingle_hashes(tokens, k=10)
        self.assertEqual(hashes, sorted(hashes))

    def test_build_min_k_shingle_hashes_capped_at_k(self) -> None:
        tokens = ["w" + str(i) for i in range(200)]
        hashes = server._build_min_k_shingle_hashes(tokens, k=50)
        self.assertLessEqual(len(hashes), 50)

    def test_signature_overlap_empty_both_is_zero(self) -> None:
        self.assertEqual(server._signature_overlap([], []), 0.0)

    def test_signature_overlap_one_empty_is_zero(self) -> None:
        hashes = ["aaa", "bbb", "ccc"]
        self.assertEqual(server._signature_overlap(hashes, []), 0.0)
        self.assertEqual(server._signature_overlap([], hashes), 0.0)

    def test_signature_overlap_identical_is_one(self) -> None:
        hashes = ["aaa", "bbb", "ccc"]
        self.assertAlmostEqual(server._signature_overlap(hashes, hashes), 1.0)

    def test_signature_overlap_disjoint_is_zero(self) -> None:
        self.assertAlmostEqual(server._signature_overlap(["aaa", "bbb"], ["ccc", "ddd"]), 0.0)

    def test_signature_overlap_partial(self) -> None:
        a = ["aaa", "bbb", "ccc"]
        b = ["bbb", "ccc", "ddd"]
        overlap = server._signature_overlap(a, b)
        # intersection=2, union=4 → 0.5
        self.assertAlmostEqual(overlap, 0.5)


class JaccardFallbackTests(MnemoTestCase):
    def test_empty_empty_is_zero(self) -> None:
        self.assertEqual(server._jaccard_similarity_fallback(set(), set()), 0.0)

    def test_one_empty_is_zero(self) -> None:
        self.assertEqual(server._jaccard_similarity_fallback({"a"}, set()), 0.0)
        self.assertEqual(server._jaccard_similarity_fallback(set(), {"a"}), 0.0)

    def test_identical_nonempty_is_one(self) -> None:
        self.assertAlmostEqual(server._jaccard_similarity_fallback({"a", "b"}, {"a", "b"}), 1.0)

    def test_disjoint_is_zero(self) -> None:
        self.assertAlmostEqual(server._jaccard_similarity_fallback({"a", "b"}, {"c", "d"}), 0.0)

    def test_partial_overlap(self) -> None:
        # intersection=1 ("b"), union=3 ("a","b","c") → 0.333...
        result = server._jaccard_similarity_fallback({"a", "b"}, {"b", "c"})
        self.assertAlmostEqual(result, 1 / 3, places=5)

    def test_jaccard_alias_matches_fallback(self) -> None:
        a = {"alpha", "beta", "gamma"}
        b = {"beta", "gamma", "delta"}
        self.assertEqual(server.jaccard(a, b), server._jaccard_similarity_fallback(a, b))

    def test_agent_salience_jaccard_parity_when_importable(self) -> None:
        try:
            import agent_salience  # type: ignore
        except Exception as exc:
            self.skipTest(f"agent_salience not importable: {exc}")
        cases = [
            (set(), set()),
            ({"a"}, set()),
            (set(), {"a"}),
            ({"a"}, {"a"}),
            ({"a", "b"}, {"b", "c"}),
            ({"a", "b"}, {"c", "d"}),
            ({"a"}, {"b"}),
        ]
        for left, right in cases:
            self.assertAlmostEqual(
                server._jaccard_similarity_fallback(left, right),
                float(agent_salience.jaccard_similarity(left, right)),
                places=8,
            )


class RecordDuplicateSignatureTests(MnemoTestCase):
    def setUp(self) -> None:
        super().setUp()
        os.environ["MNEMO_STORE"] = "sqlite"
        sqlite_file = self.root / "mnemo" / "mnemo.sqlite"
        os.environ["MNEMO_SQLITE_FILE"] = str(sqlite_file)
        server._SQLITE_BOOTSTRAPPED.clear()

    def test_content_hash_duplicate_rejected(self) -> None:
        text = "deploy with zero downtime using blue green strategy"
        self.record(text, kind="decision")
        result = server.record_memory({"text": text, "kind": "decision"})
        self.assertFalse(result["isError"], result)
        sc = result["structuredContent"]
        self.assertFalse(sc.get("recorded", True) if "recorded" in sc else False or sc.get("duplicate", False))
        # The second record call must not create a new memory
        store = server.load_store()
        matching = [m for m in store["memories"] if m["text"] == text and not m.get("deleted_at")]
        self.assertEqual(len(matching), 1)

    def test_same_text_different_kind_allowed(self) -> None:
        text = "deploy with zero downtime using blue green strategy"
        self.record(text, kind="decision")
        result = server.record_memory({"text": text, "kind": "note"})
        self.assertFalse(result["isError"], result)
        store = server.load_store()
        matching = [m for m in store["memories"] if m["text"] == text and not m.get("deleted_at")]
        self.assertEqual(len(matching), 2)

    def test_signature_fields_stored_in_sqlite(self) -> None:
        memory = self.record("all signature fields should be stored in sqlite columns", kind="note")
        sqlite_file = self.root / "mnemo" / "mnemo.sqlite"
        conn = sqlite3.connect(str(sqlite_file))
        try:
            row = conn.execute(
                "SELECT content_hash, normalized_hash, shingle_hashes_json, signature_version FROM memories WHERE id=?",
                (memory["id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row[0])  # content_hash
        self.assertIsNotNone(row[1])  # normalized_hash
        self.assertIsNotNone(row[2])  # shingle_hashes_json
        self.assertIsNotNone(row[3])  # signature_version


class BackfillSignaturesTests(MnemoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sqlite_file = self.root / "mnemo" / "mnemo.sqlite"
        os.environ["MNEMO_STORE"] = "sqlite"
        os.environ["MNEMO_SQLITE_FILE"] = str(self.sqlite_file)
        server._SQLITE_BOOTSTRAPPED.clear()

    def _insert_row_without_signature(self, memory_id: str, text: str) -> None:
        """Insert a row into SQLite manually, leaving all signature columns NULL."""
        server.load_store()  # ensure schema
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            conn.execute(
                """INSERT OR IGNORE INTO memories
                   (id, kind, text, source, tags_json, created_at, deleted)
                   VALUES (?, 'note', ?, 'test', '[]', '2026-01-01T00:00:00Z', 0)""",
                (memory_id, text),
            )
            conn.commit()
        finally:
            conn.close()

    def test_backfill_dry_run_counts_missing(self) -> None:
        self._insert_row_without_signature("nosig-1", "backfill test text for counting purposes")
        self._insert_row_without_signature("nosig-2", "another backfill test text for counting purposes")
        result = server.memory_maintenance({"action": "backfill_signatures", "dry_run": True})
        self.assertFalse(result["isError"], result)
        sc = result["structuredContent"]
        self.assertTrue(sc["dry_run"])
        self.assertGreaterEqual(sc["count_missing"], 2)

    def test_backfill_non_dry_run_updates_signatures(self) -> None:
        self._insert_row_without_signature("nosig-3", "backfill updates signatures for old rows in database")
        result = server.memory_maintenance({"action": "backfill_signatures", "dry_run": False})
        self.assertFalse(result["isError"], result)
        sc = result["structuredContent"]
        self.assertFalse(sc["dry_run"])
        self.assertGreaterEqual(sc["updated_count"], 1)
        # Verify the column was written
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            row = conn.execute("SELECT content_hash FROM memories WHERE id='nosig-3'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row[0])

    def test_backfill_doctor_warning_clears_after_backfill(self) -> None:
        # Insert enough unsigned rows to exceed 10% threshold
        for i in range(12):
            self._insert_row_without_signature(f"nosig-warn-{i}", f"backfill warning test text entry {i} with enough tokens")
        doctor_before = server.mnemo_doctor({})
        warnings_before = doctor_before["structuredContent"].get("warnings", [])
        self.assertTrue(any("signatures_outdated" in w for w in warnings_before))
        server.memory_maintenance({"action": "backfill_signatures", "dry_run": False})
        doctor_after = server.mnemo_doctor({})
        warnings_after = doctor_after["structuredContent"].get("warnings", [])
        self.assertFalse(any("signatures_outdated" in w for w in warnings_after))

    def test_backfill_json_mode_returns_error(self) -> None:
        os.environ["MNEMO_STORE"] = "json"
        result = server.memory_maintenance({"action": "backfill_signatures", "dry_run": True})
        self.assertTrue(result["isError"])

    def test_import_json_computes_signatures_for_imported_memories(self) -> None:
        import_path = self.root / "imports" / "legacy.json"
        import_path.parent.mkdir(parents=True, exist_ok=True)
        import_path.write_text(
            json.dumps({
                "version": 1,
                "memories": [{
                    "id": "imported-sig-test",
                    "kind": "note",
                    "text": "imported memory should get signature columns populated on import",
                    "source": "import",
                    "tags": [],
                    "created_at": "2026-01-01T00:00:00Z",
                }],
            }),
            encoding="utf-8",
        )
        result = server.memory_maintenance({"action": "import_json", "path": str(import_path), "dry_run": False})
        self.assertFalse(result["isError"], result)
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            row = conn.execute(
                "SELECT content_hash, signature_version FROM memories WHERE id='imported-sig-test'",
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row[0])


class ConsolidationV12Tests(MnemoTestCase):
    def _make_near_dup_pair(self) -> tuple[dict, dict]:
        m_old = server.new_memory(
            "v12-old", "decision",
            "use auth middleware before handling route requests securely in production",
            "", [],
        )
        m_old["created_at"] = "2026-01-01T00:00:00Z"
        m_new = server.new_memory(
            "v12-new", "decision",
            "use auth middleware before handling route requests now in production",
            "", [],
        )
        m_new["created_at"] = "2026-01-02T00:00:00Z"
        return m_old, m_new

    def test_consolidate_full_requires_confirmation(self) -> None:
        m_old, m_new = self._make_near_dup_pair()
        self.write_store([m_old, m_new])
        result = server.memory_maintenance({"action": "consolidate_full"})
        self.assertTrue(result["isError"])
        sc = result["structuredContent"]
        self.assertEqual(sc["error"], "full_scan_confirmation_required")
        self.assertIn("estimated_pair_count", sc)

    def test_consolidate_full_dry_run_returns_pair_count(self) -> None:
        m_old, m_new = self._make_near_dup_pair()
        self.write_store([m_old, m_new])
        result = server.memory_maintenance({
            "action": "consolidate_full",
            "confirm_full_scan": True,
            "dry_run": True,
        })
        self.assertFalse(result["isError"], result)
        sc = result["structuredContent"]
        self.assertTrue(sc["dry_run"])
        self.assertIn("estimated_pair_count", sc)
        self.assertEqual(sc["estimated_pair_count"], 1)  # n=2 → n*(n-1)/2 = 1

    def test_consolidate_full_apply_finds_duplicate(self) -> None:
        m_old, m_new = self._make_near_dup_pair()
        self.write_store([m_old, m_new])
        result = server.memory_maintenance({
            "action": "consolidate_full",
            "confirm_full_scan": True,
            "dry_run": False,
        })
        self.assertFalse(result["isError"], result)
        sc = result["structuredContent"]
        self.assertFalse(sc["dry_run"])
        self.assertGreaterEqual(sc["clusters_found"], 1)

    def test_consolidate_reports_similarity_calls(self) -> None:
        m_old, m_new = self._make_near_dup_pair()
        self.write_store([m_old, m_new])
        result = server.memory_maintenance({"action": "consolidate", "dry_run": True})
        self.assertFalse(result["isError"], result)
        sc = result["structuredContent"]
        self.assertIn("similarity_calls", sc)
        self.assertIn("candidates_examined", sc)

    def test_consolidate_tiny_text_skipped(self) -> None:
        # Two different single-word texts → no shingles (< 3 tokens), no near-dup clusters
        m1 = server.new_memory("tiny-1", "decision", "yes", "", [])
        m2 = server.new_memory("tiny-2", "decision", "no", "", [])
        self.write_store([m1, m2])
        result = server.memory_maintenance({"action": "consolidate", "dry_run": True})
        self.assertFalse(result["isError"], result)
        # No exact-hash cluster (texts differ) and shingle path skipped (< 3 tokens)
        near_dup = [c for c in result["structuredContent"]["clusters"] if c.get("duplicate_type") != "content_hash"]
        self.assertEqual(near_dup, [])

    def test_consolidate_exact_content_hash_detected(self) -> None:
        # Exact same text → content_hash duplicate should be found
        text = "auth middleware before route handler for security checks token verification"
        m1 = server.new_memory("exact-1", "decision", text, "", [])
        m1["created_at"] = "2026-01-01T00:00:00Z"
        m2 = server.new_memory("exact-2", "decision", text, "", [])
        m2["created_at"] = "2026-01-02T00:00:00Z"
        self.write_store([m1, m2])
        result = server.memory_maintenance({"action": "consolidate", "dry_run": True})
        self.assertFalse(result["isError"], result)
        clusters = result["structuredContent"]["clusters"]
        self.assertGreater(len(clusters), 0)
        exact = [c for c in clusters if c.get("duplicate_type") == "content_hash"]
        self.assertGreater(len(exact), 0)


class MigrationV12Tests(MnemoTestCase):
    """Tests that a pre-v0.12.0 SQLite database (no signature columns) loads cleanly."""

    def setUp(self) -> None:
        super().setUp()
        self.sqlite_file = self.root / "mnemo" / "mnemo.sqlite"
        self.sqlite_file.parent.mkdir(parents=True, exist_ok=True)
        os.environ["MNEMO_STORE"] = "sqlite"
        os.environ["MNEMO_SQLITE_FILE"] = str(self.sqlite_file)
        server._SQLITE_BOOTSTRAPPED.clear()

    def _create_v11_schema(self) -> None:
        """Create a SQLite file with v0.11.0 full schema (no v0.12.0 signature columns)."""
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    title TEXT,
                    preview TEXT,
                    source TEXT,
                    tags_json TEXT,
                    linked_ids_json TEXT,
                    agent_id TEXT,
                    role TEXT,
                    scope TEXT,
                    domain TEXT,
                    authority TEXT,
                    retention TEXT,
                    confidence TEXT,
                    parent_id TEXT,
                    source_run_id TEXT,
                    metadata_json TEXT,
                    pinned INTEGER DEFAULT 0,
                    deleted INTEGER DEFAULT 0,
                    superseded_by TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    token_estimate INTEGER,
                    content_hash TEXT
                )
            """)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS links (
                    source_id TEXT, target_id TEXT, relation TEXT, created_at TEXT,
                    PRIMARY KEY (source_id, target_id, relation)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, memory_id TEXT, event_type TEXT,
                    data_json TEXT, created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """)
            conn.execute(
                "INSERT INTO memories (id, kind, text, source, tags_json, created_at, deleted) "
                "VALUES ('legacy-v11', 'note', 'legacy memory from v11 schema without signatures', 'test', '[]', "
                "'2026-01-01T00:00:00Z', 0)"
            )
            conn.commit()
        finally:
            conn.close()

    def test_v11_sqlite_fixture_loads_cleanly(self) -> None:
        self._create_v11_schema()
        # load_store should migrate schema without error
        store = server.load_store()
        self.assertIsInstance(store, dict)
        ids = [m["id"] for m in store.get("memories", [])]
        self.assertIn("legacy-v11", ids)

    def test_v11_sqlite_record_after_migration(self) -> None:
        self._create_v11_schema()
        server.load_store()
        memory = self.record("new memory written after migration adds signature columns", kind="note")
        self.assertIsNotNone(memory["id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
        finally:
            conn.close()
        # All signature columns should now exist
        for col in ("normalized_hash", "token_count", "shingle_hashes_json", "signature_version"):
            self.assertIn(col, cols)

    def test_v11_sqlite_backfill_fills_legacy_row(self) -> None:
        self._create_v11_schema()
        server.load_store()
        result = server.memory_maintenance({"action": "backfill_signatures", "dry_run": False})
        self.assertFalse(result["isError"], result)
        sc = result["structuredContent"]
        self.assertGreaterEqual(sc["updated_count"], 1)


class MaintenanceActionEnumTests(MnemoTestCase):
    """Phase K / schema audit: verify the action enum in mnemo_maintenance is complete."""

    def _get_maintenance_schema(self) -> dict:
        captured: list[dict] = []

        def capture(message: dict) -> None:
            captured.append(message)

        with mock.patch("server.send", side_effect=capture):
            server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        tools = captured[-1]["result"]["tools"]
        for tool in tools:
            if tool["name"] == "mnemo":
                return tool["inputSchema"]
        return {}

    def test_gateway_tool_exists_in_tools_list(self) -> None:
        captured: list[dict] = []

        def capture(message: dict) -> None:
            captured.append(message)

        with mock.patch("server.send", side_effect=capture):
            server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        tools = captured[-1]["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertIn("mnemo", names)

    def test_maintenance_actions_documented_in_tools_description(self) -> None:
        captured: list[dict] = []

        def capture(message: dict) -> None:
            captured.append(message)

        with mock.patch("server.send", side_effect=capture):
            server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        tools = captured[-1]["result"]["tools"]
        mnemo_tool = next((t for t in tools if t["name"] == "mnemo"), None)
        self.assertIsNotNone(mnemo_tool)
        description = str(mnemo_tool.get("description", ""))
        # All maintenance actions must be documented
        for action in (
            "compact_logs",
            "consolidate",
            "consolidate_full",
            "import_json",
            "backfill_signatures",
            "propose_aliases",
            "list_alias_proposals",
            "approve_alias",
            "reject_alias_proposal",
            "list_aliases",
            "disable_alias",
            "disable_alias_concept",
        ):
            self.assertIn(action, description, f"action '{action}' missing from tool description")

    def test_valid_maintenance_actions_accepted(self) -> None:
        for action in (
            "compact_logs",
            "consolidate",
            "consolidate_full",
            "import_json",
            "backfill_signatures",
            "propose_aliases",
            "list_alias_proposals",
            "approve_alias",
            "reject_alias_proposal",
            "list_aliases",
            "disable_alias",
            "disable_alias_concept",
        ):
            # compact_logs and consolidate/consolidate_full should not return "action must be one of"
            if action == "import_json":
                # Needs a path arg; will error on missing path, not on invalid action
                result = server.memory_maintenance({"action": action, "dry_run": True})
                error_text = result["content"][0]["text"] if result.get("isError") else ""
                self.assertNotIn("action must be one of", error_text)
            elif action == "backfill_signatures":
                # SQLite required — in JSON mode returns backend error, not action error
                result = server.memory_maintenance({"action": action, "dry_run": True})
                error_text = result["content"][0]["text"] if result.get("isError") else ""
                self.assertNotIn("action must be one of", error_text)
            else:
                result = server.memory_maintenance({"action": action, "dry_run": True})
                error_text = result["content"][0]["text"] if result.get("isError") else ""
                self.assertNotIn("action must be one of", error_text)

    def test_unknown_maintenance_action_rejected(self) -> None:
        result = server.memory_maintenance({"action": "nonexistent_action_xyz"})
        self.assertTrue(result["isError"])
        self.assertIn("action must be one of", result["content"][0]["text"])


class MissTrackingAndAliasProposalTests(MnemoTestCase):
    class _FakeIdfProfile:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def to_dict(self) -> dict:
            return dict(self._payload)

    class _FakeBreakdown:
        def __init__(self, final: float) -> None:
            self.cosine = final
            self.jaccard = final
            self.idf_cosine = 0.0
            self.idf_jaccard = 0.0
            self.repetition = 0.0
            self.recency = 0.0
            self.novelty = 0.0
            self.drift = 0.0
            self.final = final
            self.weights = {"cosine": 0.7, "jaccard": 0.3, "idf_cosine": 0.0, "idf_jaccard": 0.0}
            self.idf_status = "ready"
            self.idf_used = False

        def to_dict(self) -> dict:
            return {
                "cosine": self.cosine,
                "jaccard": self.jaccard,
                "idf_cosine": self.idf_cosine,
                "idf_jaccard": self.idf_jaccard,
                "repetition": self.repetition,
                "recency": self.recency,
                "novelty": self.novelty,
                "drift": self.drift,
                "final": self.final,
                "weights": dict(self.weights),
                "idf_status": self.idf_status,
                "idf_used": self.idf_used,
            }

    class _FakeSalience:
        __version__ = "0.3.alias-fake"
        __file__ = "fake_agent_salience.py"

        def __init__(self, signal_final: float = 0.05) -> None:
            self.signal_final = signal_final

        @staticmethod
        def _tokens(text: str) -> list[str]:
            return [token for token in str(text).lower().split() if token]

        def build_idf_profile(
            self,
            documents,
            *,
            domain=None,
            min_documents=200,
            min_unique_terms=1000,
            min_total_tokens=10000,
        ):
            import math

            docs = [self._tokens(doc) for doc in documents if str(doc).strip()]
            doc_count = len(docs)
            total_tokens = sum(len(doc) for doc in docs)
            doc_freq: dict[str, int] = {}
            for doc in docs:
                for token in set(doc):
                    doc_freq[token] = doc_freq.get(token, 0) + 1
            unique_terms = len(doc_freq)
            idf_values = {
                token: math.log((1.0 + doc_count) / (1.0 + freq)) + 1.0 for token, freq in doc_freq.items()
            }
            ready = (
                doc_count >= int(min_documents)
                and unique_terms >= int(min_unique_terms)
                and total_tokens >= int(min_total_tokens)
            )
            payload = {
                "domain": domain,
                "doc_count": doc_count,
                "unique_terms": unique_terms,
                "total_tokens": total_tokens,
                "status": "ready" if ready else "cold",
                "ready": ready,
                "idf": idf_values,
                "min_documents": int(min_documents),
                "min_unique_terms": int(min_unique_terms),
                "min_total_tokens": int(min_total_tokens),
                "version": 1,
            }
            return MissTrackingAndAliasProposalTests._FakeIdfProfile(payload)

        def signal_score(self, left: str, right: str, **kwargs):  # noqa: ARG002
            return MissTrackingAndAliasProposalTests._FakeBreakdown(self.signal_final)

        def drift_score(self, left: str, right: str) -> float:  # noqa: ARG002
            return 0.0

    def setUp(self) -> None:
        super().setUp()
        self.sqlite_file = self.root / "mnemo" / "mnemo.sqlite"
        os.environ["MNEMO_STORE"] = "sqlite"
        os.environ["MNEMO_SQLITE_FILE"] = str(self.sqlite_file)
        server._SQLITE_BOOTSTRAPPED.clear()

    def _set_low_idf_thresholds(self) -> None:
        os.environ["MNEMO_IDF_MIN_DOCUMENTS"] = "1"
        os.environ["MNEMO_IDF_MIN_UNIQUE_TERMS"] = "1"
        os.environ["MNEMO_IDF_MIN_TOTAL_TOKENS"] = "1"
        os.environ["MNEMO_IDF_DOMAIN_MIN_DOCUMENTS"] = "1"
        os.environ["MNEMO_IDF_DOMAIN_MIN_UNIQUE_TERMS"] = "1"
        os.environ["MNEMO_IDF_DOMAIN_MIN_TOTAL_TOKENS"] = "1"

    def _latest_event_row(self, action: str) -> sqlite3.Row | None:
        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT * FROM events WHERE action = ? ORDER BY rowid DESC LIMIT 1",
                (action,),
            ).fetchone()
        finally:
            conn.close()

    def _weak_search_match(self, score: float = 0.05) -> list[dict]:
        return [
            {
                "id": "mem_weak",
                "kind": "note",
                "text": "weak lexical candidate",
                "source": "test",
                "score": score,
                "superseded_by": None,
                "deleted_at": None,
                "deletion_reason": None,
            }
        ]

    def test_search_zero_results_writes_miss_event(self) -> None:
        with mock.patch("server.search_rank", return_value=[]):
            result = server.search_memories({"query": "no hit query", "domain": "agentic", "limit": 5})
        self.assertFalse(result["isError"], result)
        row = self._latest_event_row("mnemo_search")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(int(row["success"]), 0)
        self.assertEqual(int(row["include_in_salience"]), 1)
        self.assertEqual(int(row["result_count"]), 0)
        self.assertEqual(float(row["top_score"]), 0.0)

    def test_search_weak_top_score_marks_miss(self) -> None:
        with mock.patch("server.search_rank", return_value=self._weak_search_match(0.05)):
            result = server.search_memories({"query": "weak top score", "domain": "agentic", "limit": 5})
        self.assertFalse(result["isError"], result)
        row = self._latest_event_row("mnemo_search")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(int(row["success"]), 0)
        self.assertEqual(int(row["include_in_salience"]), 1)
        self.assertLess(float(row["top_score"]), 0.15)

    def test_search_strong_top_score_marks_success(self) -> None:
        with mock.patch("server.search_rank", return_value=self._weak_search_match(0.85)):
            result = server.search_memories({"query": "strong top score", "domain": "agentic", "limit": 5})
        self.assertFalse(result["isError"], result)
        row = self._latest_event_row("mnemo_search")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(int(row["success"]), 1)
        self.assertGreaterEqual(float(row["top_score"]), 0.15)

    def test_salience_check_miss_writes_top_score_and_miss_flags(self) -> None:
        fake = self._FakeSalience(signal_final=0.05)
        self.record("auth middleware marker for salience miss test", kind="note", domain="agentic")
        with mock.patch("server.load_optional_agent_salience", return_value=(fake, None)):
            result = server.memory_salience_check({"text": "auth middleware marker", "domain": "agentic", "threshold": 0.80})
        self.assertFalse(result["isError"], result)
        row = self._latest_event_row("mnemo_salience_check")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(int(row["success"]), 0)
        self.assertEqual(int(row["include_in_salience"]), 1)
        self.assertEqual(int(row["result_count"]), 0)
        self.assertGreaterEqual(float(row["top_score"]), 0.0)

    def test_recall_query_miss_writes_miss_event(self) -> None:
        result = server.memory_recall(
            {"mode": "agent", "query": "missing recall phrase", "task": "missing recall phrase", "domain": "agentic"}
        )
        self.assertFalse(result["isError"], result)
        row = self._latest_event_row("mnemo_recall")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(int(row["success"]), 0)
        self.assertEqual(int(row["include_in_salience"]), 1)
        self.assertEqual(int(row["result_count"]), 0)
        self.assertEqual(float(row["top_score"]), 0.0)

    def test_alias_hint_event_recorded_and_counted_by_proposals(self) -> None:
        write = server.memory_alias_hint(
            {
                "domain": "agentic",
                "canonical": "memory recall pipeline",
                "candidate_alias": "hippocampus bridge",
                "original_query": "hippocampus bridge",
                "successful_query": "memory recall pipeline",
                "confidence": "high",
                "include_in_salience": True,
            }
        )
        self.assertFalse(write["isError"], write)
        result = server.memory_maintenance({"action": "propose_aliases", "window_days": 30, "include_hints": True})
        self.assertFalse(result["isError"], result)
        structured = result["structuredContent"]
        self.assertGreaterEqual(int(structured["hint_event_count"]), 1)

    def test_propose_aliases_empty_corpus_returns_no_misses(self) -> None:
        result = server.memory_maintenance({"action": "propose_aliases", "window_days": 30})
        self.assertFalse(result["isError"], result)
        structured = result["structuredContent"]
        self.assertEqual(structured["status"], "no_misses")
        self.assertEqual(int(structured["miss_event_count"]), 0)
        self.assertEqual(int(structured["cluster_count"]), 0)

    def test_repeated_miss_queries_produce_proposal_with_active_idf(self) -> None:
        self._set_low_idf_thresholds()
        fake = self._FakeSalience(signal_final=0.05)
        self.record(
            "hippocampus recall bridge canonical memory entry for alias proposal",
            kind="note",
            domain="agentic",
        )
        for idx in range(6):
            self.record(
                f"router budget ledger filler document {idx}",
                kind="note",
                domain="agentic",
            )
        with mock.patch("server.load_optional_agent_salience", return_value=(fake, None)):
            with mock.patch("server.search_rank", return_value=self._weak_search_match(0.05)):
                for idx in range(3):
                    server.search_memories({"query": "hippocampus recall bridge", "domain": "agentic", "limit": 5, "client_nonce": idx})
            result = server.memory_maintenance(
                {
                    "action": "propose_aliases",
                    "window_days": 30,
                    "domain": "agentic",
                    "min_recurrence": 3,
                    "include_hints": False,
                }
            )
        self.assertFalse(result["isError"], result)
        structured = result["structuredContent"]
        self.assertEqual(structured["status"], "ok")
        self.assertGreaterEqual(len(structured["proposals"]), 1)

    def test_low_idf_common_terms_are_not_proposed(self) -> None:
        self._set_low_idf_thresholds()
        fake = self._FakeSalience(signal_final=0.05)
        for idx in range(5):
            self.record(
                f"and tool common filler sequence {idx} repeated and tool baseline",
                kind="note",
                domain="agentic",
            )
        with mock.patch("server.load_optional_agent_salience", return_value=(fake, None)):
            with mock.patch("server.search_rank", return_value=self._weak_search_match(0.05)):
                for idx in range(4):
                    server.search_memories({"query": "and tool", "domain": "agentic", "limit": 5, "client_nonce": idx})
            result = server.memory_maintenance(
                {
                    "action": "propose_aliases",
                    "window_days": 30,
                    "domain": "agentic",
                    "min_recurrence": 3,
                }
            )
        self.assertFalse(result["isError"], result)
        structured = result["structuredContent"]
        self.assertEqual(structured["status"], "no_proposals")
        self.assertEqual(structured["proposals"], [])

    def test_alias_hint_bonus_outweighs_passive_miss_cluster(self) -> None:
        self._set_low_idf_thresholds()
        fake = self._FakeSalience(signal_final=0.05)
        self.record("hippocampus recall bridge canonical memory entry", kind="note", domain="agentic")
        self.record("cache ledger invalidation canonical memory entry", kind="note", domain="agentic")
        with mock.patch("server.load_optional_agent_salience", return_value=(fake, None)):
            with mock.patch("server.search_rank", return_value=self._weak_search_match(0.05)):
                for idx in range(3):
                    server.search_memories({"query": "hippocampus recall bridge", "domain": "agentic", "limit": 5, "client_nonce": idx})
                for idx in range(3):
                    server.search_memories({"query": "cache ledger invalidation", "domain": "agentic", "limit": 5, "client_nonce": 100 + idx})
            hint = server.memory_alias_hint(
                {
                    "domain": "agentic",
                    "canonical": "memory recall pipeline",
                    "candidate_alias": "hippocampus recall bridge",
                    "original_query": "hippocampus recall bridge",
                    "successful_query": "memory recall pipeline",
                    "confidence": "high",
                    "include_in_salience": True,
                }
            )
            self.assertFalse(hint["isError"], hint)
            result = server.memory_maintenance(
                {
                    "action": "propose_aliases",
                    "window_days": 30,
                    "domain": "agentic",
                    "min_recurrence": 3,
                    "include_hints": True,
                }
            )
        self.assertFalse(result["isError"], result)
        proposals = result["structuredContent"]["proposals"]
        self.assertGreaterEqual(len(proposals), 2)
        score_by_alias = {str(item["candidate_alias"]).lower(): float(item["score"]) for item in proposals}
        hinted_key = next((key for key in score_by_alias if "hippocampus recall bridge" in key), None)
        passive_key = next((key for key in score_by_alias if "cache ledger invalidation" in key), None)
        self.assertIsNotNone(hinted_key)
        self.assertIsNotNone(passive_key)
        assert hinted_key is not None and passive_key is not None
        self.assertGreater(
            score_by_alias[hinted_key],
            score_by_alias[passive_key],
        )

    def test_propose_aliases_dry_run_does_not_persist_proposals(self) -> None:
        self._set_low_idf_thresholds()
        fake = self._FakeSalience(signal_final=0.05)
        self.record("memory recall hippocampus bridge reference", kind="note", domain="agentic")
        with mock.patch("server.load_optional_agent_salience", return_value=(fake, None)):
            with mock.patch("server.search_rank", return_value=self._weak_search_match(0.05)):
                for idx in range(3):
                    server.search_memories({"query": "hippocampus recall bridge", "domain": "agentic", "client_nonce": idx})
            result = server.memory_maintenance(
                {
                    "action": "propose_aliases",
                    "window_days": 30,
                    "domain": "agentic",
                    "dry_run": True,
                }
            )
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["persisted_count"], 0)
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            count = int(conn.execute("SELECT COUNT(*) FROM alias_proposals").fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_alias_curation_workflow_prompt_exists(self) -> None:
        current = Path(__file__).resolve()
        found: Path | None = None
        for parent in [current.parent, *current.parents]:
            direct = parent / ".github" / "prompts" / "workflow.alias-curation.prompt.md"
            if direct.exists():
                found = direct
                break
            nested = parent / "agentic" / ".github" / "prompts" / "workflow.alias-curation.prompt.md"
            if nested.exists():
                found = nested
                break
        self.assertIsNotNone(found, "workflow.alias-curation.prompt.md was not found in prompt library")
        assert found is not None
        text = found.read_text(encoding="utf-8")
        self.assertIn("propose_aliases", text)
        self.assertIn("list_alias_proposals", text)
        self.assertIn("approve_alias", text)
        self.assertIn("reject_alias_proposal", text)
        self.assertNotIn("prepare a patch for `.agentic/vocabulary/aliases.json`", text)
        self.assertIn("Do not touch `.agentic/vocabulary/aliases.json`", text)
        self.assertIn(".agentic/vocabulary/aliases.example.json", text)


class AliasSqliteLifecycleTests(MnemoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sqlite_file = self.root / "mnemo" / "mnemo.sqlite"
        os.environ["MNEMO_STORE"] = "sqlite"
        os.environ["MNEMO_SQLITE_FILE"] = str(self.sqlite_file)
        server._SQLITE_BOOTSTRAPPED.clear()

    def _set_low_idf_thresholds(self) -> None:
        os.environ["MNEMO_IDF_MIN_DOCUMENTS"] = "1"
        os.environ["MNEMO_IDF_MIN_UNIQUE_TERMS"] = "1"
        os.environ["MNEMO_IDF_MIN_TOTAL_TOKENS"] = "1"
        os.environ["MNEMO_IDF_DOMAIN_MIN_DOCUMENTS"] = "1"
        os.environ["MNEMO_IDF_DOMAIN_MIN_UNIQUE_TERMS"] = "1"
        os.environ["MNEMO_IDF_DOMAIN_MIN_TOTAL_TOKENS"] = "1"

    def _weak_search_match(self, score: float = 0.05) -> list[dict]:
        return [
            {
                "id": "mem_weak",
                "kind": "note",
                "text": "weak lexical candidate",
                "source": "test",
                "score": score,
                "superseded_by": None,
                "deleted_at": None,
                "deletion_reason": None,
            }
        ]

    def _seed_alias_proposal_evidence(self, *, with_hint: bool = False) -> None:
        self._set_low_idf_thresholds()
        self.record(
            "hippocampus recall bridge canonical memory entry for alias proposal",
            kind="note",
            domain="agentic",
        )
        for idx in range(5):
            self.record(f"idf maturity filler memory {idx} for alias lifecycle tests", kind="note", domain="agentic")
        with mock.patch("server.search_rank", return_value=self._weak_search_match(0.05)):
            for idx in range(3):
                server.search_memories(
                    {
                        "query": "hippocampus recall bridge",
                        "domain": "agentic",
                        "limit": 5,
                        "client_nonce": idx,
                    }
                )
        if with_hint:
            write = server.memory_alias_hint(
                {
                    "domain": "agentic",
                    "canonical": "memory recall pipeline",
                    "candidate_alias": "hippocampus recall bridge",
                    "original_query": "hippocampus recall bridge",
                    "successful_query": "memory recall pipeline",
                    "confidence": "high",
                    "include_in_salience": True,
                }
            )
            self.assertFalse(write["isError"], write)

    def _insert_proposal(self, proposal_id: str, *, status: str = "pending", domain: str = "agentic") -> None:
        server.load_store()
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            now = server.now_iso()
            conn.execute(
                """
                INSERT INTO alias_proposals(
                    proposal_id, domain, language, canonical, candidate_alias, normalized_alias,
                    score, status, recommendation, evidence_json, created_at, updated_at
                ) VALUES(?, ?, 'en', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    domain,
                    "memory recall pipeline",
                    "hippocampus bridge",
                    server._normalize_alias_term("hippocampus bridge"),
                    0.82,
                    status,
                    "review",
                    json.dumps({"source": "unit-test"}),
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_alias_schema_tables_indexes_and_views_exist(self) -> None:
        server.load_store()
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            views = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}
        finally:
            conn.close()
        for table in ("alias_concepts", "alias_terms", "alias_proposals", "alias_proposal_events"):
            self.assertIn(table, tables)
        for index in (
            "idx_alias_terms_norm",
            "idx_alias_terms_domain_norm",
            "idx_alias_terms_concept",
            "idx_alias_concepts_domain",
            "idx_alias_proposals_status",
            "idx_alias_proposals_domain_status",
        ):
            self.assertIn(index, indexes)
        for view in ("v_alias_vocabulary", "v_alias_pending_proposals", "v_alias_concept_counts"):
            self.assertIn(view, views)

    def test_propose_aliases_dry_run_false_persists_and_links_events(self) -> None:
        self._seed_alias_proposal_evidence(with_hint=True)
        fake = MissTrackingAndAliasProposalTests._FakeSalience(signal_final=0.05)
        with mock.patch("server.load_optional_agent_salience", return_value=(fake, None)):
            result = server.memory_maintenance(
                {
                    "action": "propose_aliases",
                    "window_days": 30,
                    "domain": "agentic",
                    "include_hints": True,
                    "dry_run": False,
                }
            )
        self.assertFalse(result["isError"], result)
        structured = result["structuredContent"]
        self.assertGreaterEqual(int(structured["persisted_count"]), 1)
        self.assertFalse(bool(structured["dry_run"]))
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            conn.row_factory = sqlite3.Row
            proposal_rows = conn.execute("SELECT * FROM alias_proposals").fetchall()
            link_count = int(conn.execute("SELECT COUNT(*) FROM alias_proposal_events").fetchone()[0])
        finally:
            conn.close()
        self.assertGreaterEqual(len(proposal_rows), 1)
        self.assertGreaterEqual(link_count, 1)
        for row in proposal_rows:
            self.assertEqual(str(row["status"]), "pending")
            json.loads(str(row["evidence_json"]))

    def test_approve_alias_marks_proposal_and_prevents_duplicate_alias_term(self) -> None:
        self._insert_proposal("alias-prop-unit-approve")
        approved = server.memory_maintenance(
            {
                "action": "approve_alias",
                "proposal_id": "alias-prop-unit-approve",
                "approved_by": "unit-test",
            }
        )
        self.assertFalse(approved["isError"], approved)
        payload = approved["structuredContent"]
        self.assertEqual(payload["proposal"]["status"], "approved")
        concept_id = str(payload["concept"]["concept_id"])
        alias_id = str(payload["alias"]["alias_id"])
        self.assertTrue(concept_id)
        self.assertTrue(alias_id)
        again = server.memory_maintenance(
            {
                "action": "approve_alias",
                "proposal_id": "alias-prop-unit-approve",
                "approved_by": "unit-test",
            }
        )
        self.assertFalse(again["isError"], again)
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM alias_terms
                    WHERE concept_id = ? AND normalized_term = ?
                    """,
                    (concept_id, server._normalize_alias_term("hippocampus bridge")),
                ).fetchone()[0]
            )
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_reject_alias_proposal_marks_rejected(self) -> None:
        self._insert_proposal("alias-prop-unit-reject")
        rejected = server.memory_maintenance(
            {
                "action": "reject_alias_proposal",
                "proposal_id": "alias-prop-unit-reject",
                "reason": "generic wording",
            }
        )
        self.assertFalse(rejected["isError"], rejected)
        payload = rejected["structuredContent"]["proposal"]
        self.assertEqual(payload["status"], "rejected")
        self.assertTrue(str(payload.get("evidence_json", "")).strip())

    def test_list_alias_proposals_filters_status_and_domain(self) -> None:
        self._insert_proposal("alias-prop-pending-auth", domain="auth")
        self._insert_proposal("alias-prop-rejected-agentic", status="rejected", domain="agentic")
        pending_auth = server.memory_maintenance(
            {"action": "list_alias_proposals", "status": "pending", "domain": "auth", "limit": 20}
        )
        self.assertFalse(pending_auth["isError"], pending_auth)
        rows = pending_auth["structuredContent"]["proposals"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["proposal_id"], "alias-prop-pending-auth")
        rejected = server.memory_maintenance({"action": "list_alias_proposals", "status": "rejected", "limit": 20})
        self.assertFalse(rejected["isError"], rejected)
        self.assertTrue(any(row["proposal_id"] == "alias-prop-rejected-agentic" for row in rejected["structuredContent"]["proposals"]))

    def test_runtime_alias_search_and_disable_alias(self) -> None:
        memory = self.record("memory recall pipeline implementation details", kind="note", domain="agentic")
        approved = server.memory_maintenance(
            {
                "action": "approve_alias",
                "canonical": "memory recall pipeline",
                "candidate_alias": "hippocampus bridge",
                "domain": "agentic",
                "approved_by": "unit-test",
            }
        )
        self.assertFalse(approved["isError"], approved)
        alias_id = approved["structuredContent"]["alias"]["alias_id"]
        search_with_alias = server.search_memories({"query": "hippocampus bridge", "domain": "agentic", "limit": 5})
        self.assertFalse(search_with_alias["isError"], search_with_alias)
        structured = search_with_alias["structuredContent"]
        self.assertTrue(structured["aliases_used"])
        self.assertGreaterEqual(float(structured["alias_concept_score"]), 0.1)
        ids = [item["id"] for item in structured["matches"]]
        self.assertIn(memory["id"], ids)
        disabled = server.memory_maintenance({"action": "disable_alias", "alias_id": alias_id, "reason": "deprecated"})
        self.assertFalse(disabled["isError"], disabled)
        active_aliases = server.memory_maintenance({"action": "list_aliases", "domain": "agentic", "status": "active"})
        self.assertFalse(active_aliases["isError"], active_aliases)
        self.assertFalse(any(row["alias_id"] == alias_id for row in active_aliases["structuredContent"]["aliases"]))
        search_after_disable = server.search_memories({"query": "hippocampus bridge", "domain": "agentic", "limit": 5})
        self.assertFalse(search_after_disable["isError"], search_after_disable)
        self.assertFalse(search_after_disable["structuredContent"]["aliases_used"])

    def test_doctor_reports_alias_counts_and_no_alias_json_path(self) -> None:
        self._insert_proposal("alias-prop-pending-doctor", status="pending")
        self._insert_proposal("alias-prop-rejected-doctor", status="rejected")
        server.memory_maintenance(
            {
                "action": "approve_alias",
                "canonical": "memory recall pipeline",
                "candidate_alias": "hippocampus bridge",
                "domain": "agentic",
                "approved_by": "unit-test",
            }
        )
        doctor = server.mnemo_doctor({})
        self.assertFalse(doctor["isError"], doctor)
        structured = doctor["structuredContent"]
        aliases = structured["aliases"]
        self.assertTrue(aliases["available"])
        self.assertGreaterEqual(int(aliases["active_concept_count"]), 1)
        self.assertGreaterEqual(int(aliases["active_alias_count"]), 1)
        self.assertGreaterEqual(int(aliases["pending_proposal_count"]), 1)
        self.assertGreaterEqual(int(aliases["rejected_proposal_count"]), 1)
        text = doctor["content"][0]["text"]
        self.assertIn("Aliases: active_concepts=", text)
        self.assertNotIn("aliases.json", text)
        self.assertNotIn("aliases.example.json", text)

    def test_server_runtime_has_no_aliases_json_reference(self) -> None:
        server_text = Path(server.__file__).read_text(encoding="utf-8")
        self.assertNotIn(".agentic/vocabulary/aliases.json", server_text)
        self.assertNotIn(".agentic/vocabulary/aliases.example.json", server_text)


class SmallBenchmarkTests(MnemoTestCase):
    """In-process benchmark: 2000 memories, consolidate dry_run must finish in < 30s."""

    def setUp(self) -> None:
        super().setUp()
        self.sqlite_file = self.root / "mnemo" / "mnemo.sqlite"
        os.environ["MNEMO_STORE"] = "sqlite"
        os.environ["MNEMO_SQLITE_FILE"] = str(self.sqlite_file)
        server._SQLITE_BOOTSTRAPPED.clear()

    def _insert_batch(self, count: int) -> None:
        """Insert memories directly via SQLite for speed."""
        server.load_store()  # ensure schema
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            rows = []
            for i in range(count):
                text = f"memory entry {i} discussing topic area {i % 50} with related subtopic {i % 10}"
                sig = server._build_memory_signature(text)
                rows.append((
                    f"bench-{i}", "note", text, "bench", "[]",
                    f"2026-01-{(i % 28) + 1:02d}T00:00:00Z",
                    sig["content_hash"], sig["normalized_hash"],
                    sig["token_count"], sig["unique_token_count"],
                    sig["top_terms_json"], sig["shingle_hashes_json"],
                    sig["signature_version"], sig["normalizer_version"],
                    sig["signature_updated_at"],
                ))
            conn.executemany(
                """INSERT OR IGNORE INTO memories
                   (id, kind, text, source, tags_json, created_at,
                    content_hash, normalized_hash, token_count, unique_token_count,
                    top_terms_json, shingle_hashes_json, signature_version, normalizer_version,
                    signature_updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    def test_consolidate_2000_memories_dry_run_under_30s(self) -> None:
        self._insert_batch(2000)
        start = time.monotonic()
        result = server.memory_maintenance({"action": "consolidate", "dry_run": True})
        elapsed = time.monotonic() - start
        self.assertFalse(result["isError"], result)
        self.assertLess(elapsed, 30.0, f"consolidate dry_run took {elapsed:.1f}s on 2000 memories (limit 30s)")

    def test_backfill_2000_memories_without_signatures_under_30s(self) -> None:
        server.load_store()
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            rows = [
                (f"nobench-{i}", "note",
                 f"backfill bench entry {i} with topic area {i % 50} and subtopic {i % 10}",
                 "bench", "[]", f"2026-01-{(i % 28) + 1:02d}T00:00:00Z")
                for i in range(2000)
            ]
            conn.executemany(
                "INSERT OR IGNORE INTO memories (id, kind, text, source, tags_json, created_at) VALUES (?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()
        start = time.monotonic()
        result = server.memory_maintenance({"action": "backfill_signatures", "dry_run": False})
        elapsed = time.monotonic() - start
        self.assertFalse(result["isError"], result)
        self.assertLess(elapsed, 30.0, f"backfill took {elapsed:.1f}s on 2000 memories (limit 30s)")
        self.assertGreaterEqual(result["structuredContent"]["updated_count"], 2000)


if __name__ == "__main__":
    unittest.main()
