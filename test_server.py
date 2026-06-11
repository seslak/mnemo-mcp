from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from importlib import import_module
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import salience_loader
import server


ENV_KEYS = [
    "MNEMO_FILE",
    "MNEMO_STORE",
    "MNEMO_SQLITE_FILE",
    "MNEMO_PACK_LANDING_DIR",
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
    "MNEMO_ALIAS_MIN_IDF_STRENGTH",
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
        os.environ.pop("MNEMO_ALIAS_MIN_IDF_STRENGTH", None)
        os.environ.pop("MNEMO_MISS_TOP_SCORE_THRESHOLD", None)
        os.environ.pop("AGENT_SALIENCE_HOME", None)
        server._SYMBOL_CACHE.clear()
        if hasattr(server, "_SQLITE_SCHEMA_READY"):
            server._SQLITE_SCHEMA_READY.clear()
        if hasattr(server, "_GIT_CONTEXT_CACHE"):
            server._GIT_CONTEXT_CACHE.clear()
        if hasattr(salience_loader, "_reset_load_optional_agent_salience_cache"):
            salience_loader._reset_load_optional_agent_salience_cache()

    def tearDown(self) -> None:
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        server._SYMBOL_CACHE.clear()
        if hasattr(server, "_SQLITE_SCHEMA_READY"):
            server._SQLITE_SCHEMA_READY.clear()
        if hasattr(server, "_GIT_CONTEXT_CACHE"):
            server._GIT_CONTEXT_CACHE.clear()
        if hasattr(salience_loader, "_reset_load_optional_agent_salience_cache"):
            salience_loader._reset_load_optional_agent_salience_cache()
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

    def test_salience_loader_memoizes_unavailable_result(self) -> None:
        os.environ.pop("AGENT_SALIENCE_HOME", None)
        calls = {"count": 0}

        def side_effect(name: str, package: str | None = None):
            if name == "agent_salience":
                calls["count"] += 1
                raise ModuleNotFoundError("missing")
            return import_module(name, package=package)

        with mock.patch("salience_loader.importlib.import_module", side_effect=side_effect):
            first = salience_loader.load_optional_agent_salience()
            second = salience_loader.load_optional_agent_salience()
        self.assertEqual(calls["count"], 1)
        self.assertEqual(first, second)


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
        self.assertIn("pack_preview", payload["available_actions"])
        self.assertIn("pack_redaction_preview", payload["available_actions"])
        self.assertIn("pack_export", payload["available_actions"])
        self.assertIn("pack_inspect", payload["available_actions"])
        self.assertIn("pack_import", payload["available_actions"])
        self.assertIn("pack_landing_list", payload["available_actions"])
        self.assertIn("pack_list_imports", payload["available_actions"])
        self.assertIn("pack_review_import", payload["available_actions"])
        self.assertIn("pack_promote_preview", payload["available_actions"])
        self.assertIn("pack_promote", payload["available_actions"])
        self.assertIn("signer_add", payload["available_actions"])
        self.assertIn("signer_list", payload["available_actions"])
        self.assertIn("signer_disable", payload["available_actions"])
        self.assertIn("signer_enable", payload["available_actions"])

    def test_gateway_includes_event_history_actions(self) -> None:
        for action in ("recent_events", "search_events", "get_event", "memory_events"):
            self.assertIn(action, server.GATEWAY_ACTIONS)
        tool = server.TOOLS[0]
        enum_values = tool["inputSchema"]["properties"]["action"]["enum"]
        for action in ("recent_events", "search_events", "get_event", "memory_events"):
            self.assertIn(action, enum_values)

    def test_gateway_includes_pack_preview_action(self) -> None:
        self.assertIn("pack_preview", server.GATEWAY_ACTIONS)
        tool = server.TOOLS[0]
        enum_values = tool["inputSchema"]["properties"]["action"]["enum"]
        self.assertIn("pack_preview", enum_values)

    def test_gateway_includes_pack_redaction_preview_action(self) -> None:
        self.assertIn("pack_redaction_preview", server.GATEWAY_ACTIONS)
        tool = server.TOOLS[0]
        enum_values = tool["inputSchema"]["properties"]["action"]["enum"]
        self.assertIn("pack_redaction_preview", enum_values)

    def test_gateway_includes_pack_export_action(self) -> None:
        self.assertIn("pack_export", server.GATEWAY_ACTIONS)
        tool = server.TOOLS[0]
        enum_values = tool["inputSchema"]["properties"]["action"]["enum"]
        self.assertIn("pack_export", enum_values)

    def test_gateway_includes_pack_inspect_action(self) -> None:
        self.assertIn("pack_inspect", server.GATEWAY_ACTIONS)
        tool = server.TOOLS[0]
        enum_values = tool["inputSchema"]["properties"]["action"]["enum"]
        self.assertIn("pack_inspect", enum_values)

    def test_gateway_includes_pack_import_action(self) -> None:
        self.assertIn("pack_import", server.GATEWAY_ACTIONS)
        tool = server.TOOLS[0]
        enum_values = tool["inputSchema"]["properties"]["action"]["enum"]
        self.assertIn("pack_import", enum_values)

    def test_gateway_includes_pack_landing_list_action(self) -> None:
        self.assertIn("pack_landing_list", server.GATEWAY_ACTIONS)
        tool = server.TOOLS[0]
        enum_values = tool["inputSchema"]["properties"]["action"]["enum"]
        self.assertIn("pack_landing_list", enum_values)

    def test_gateway_includes_pack_list_imports_action(self) -> None:
        self.assertIn("pack_list_imports", server.GATEWAY_ACTIONS)
        tool = server.TOOLS[0]
        enum_values = tool["inputSchema"]["properties"]["action"]["enum"]
        self.assertIn("pack_list_imports", enum_values)

    def test_gateway_includes_pack_review_import_action(self) -> None:
        self.assertIn("pack_review_import", server.GATEWAY_ACTIONS)
        tool = server.TOOLS[0]
        enum_values = tool["inputSchema"]["properties"]["action"]["enum"]
        self.assertIn("pack_review_import", enum_values)

    def test_gateway_includes_pack_promote_preview_action(self) -> None:
        self.assertIn("pack_promote_preview", server.GATEWAY_ACTIONS)
        tool = server.TOOLS[0]
        enum_values = tool["inputSchema"]["properties"]["action"]["enum"]
        self.assertIn("pack_promote_preview", enum_values)

    def test_gateway_includes_pack_promote_action(self) -> None:
        self.assertIn("pack_promote", server.GATEWAY_ACTIONS)
        tool = server.TOOLS[0]
        enum_values = tool["inputSchema"]["properties"]["action"]["enum"]
        self.assertIn("pack_promote", enum_values)

    def test_gateway_includes_signer_actions(self) -> None:
        for action_name in ("signer_add", "signer_list", "signer_disable", "signer_enable"):
            self.assertIn(action_name, server.GATEWAY_ACTIONS)
            tool = server.TOOLS[0]
            enum_values = tool["inputSchema"]["properties"]["action"]["enum"]
            self.assertIn(action_name, enum_values)


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


class RpcErrorSanitizationTests(MnemoTestCase):
    def test_tools_call_internal_error_is_sanitized(self) -> None:
        captured: list[dict] = []
        stderr = io.StringIO()

        def capture(message: dict) -> None:
            captured.append(message)

        with mock.patch("server.send", side_effect=capture), mock.patch(
            "server.mnemo_gateway",
            side_effect=RuntimeError(str(self.workspace / "secret.txt")),
        ), mock.patch("sys.stderr", stderr):
            server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": "mnemo", "arguments": {"action": "doctor", "params": {}}},
                }
            )

        payload = captured[-1]["result"]
        self.assertTrue(payload["isError"])
        self.assertIn("internal error handling tool call", payload["content"][0]["text"])
        self.assertNotIn(str(self.workspace), payload["content"][0]["text"])
        self.assertIn(str(self.workspace), stderr.getvalue())

    def test_non_tool_dispatch_internal_error_returns_jsonrpc_error(self) -> None:
        captured: list[dict] = []
        stderr = io.StringIO()

        def capture(message: dict) -> None:
            captured.append(message)

        with mock.patch("server.send", side_effect=capture), mock.patch(
            "server.copilot_safe_tools",
            side_effect=RuntimeError("tools list blew up"),
        ), mock.patch("sys.stderr", stderr):
            server.handle_request({"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {}})

        self.assertEqual(captured[-1]["error"]["code"], -32603)
        self.assertEqual(captured[-1]["error"]["message"], "Internal error")
        self.assertIn("tools list blew up", stderr.getvalue())


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


class PythonCompatibilityTests(MnemoTestCase):
    def test_quote_token_helper_python310_safe(self) -> None:
        self.assertEqual(server._quote_sqlite_fts_token("abc"), '"abc"')
        self.assertEqual(server._quote_sqlite_fts_token('a"b'), '"a""b"')
        self.assertEqual(server._quote_sqlite_fts_token(""), '""')

    def test_mnemo_server_source_has_no_backslash_fstring_quote_pattern(self) -> None:
        source = Path(server.__file__).read_text(encoding="utf-8")
        self.assertNotIn('f"\\"{token.replace(', source)
        self.assertNotIn("f'\\\"{token.replace(", source)

    def test_mnemo_declared_python_minimum_is_compatible(self) -> None:
        pyproject = (Path(__file__).resolve().parent / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('requires-python = ">=3.10"', pyproject)
        self.assertIn('"Programming Language :: Python :: 3.10"', pyproject)
        self.assertIn('"Programming Language :: Python :: 3.11"', pyproject)


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

    def test_lookup_symbol_skips_symlink_escape(self) -> None:
        outside = self.root / "outside_secret.py"
        outside.write_text("def leaked_secret():\n    return '/tmp/secret'\n", encoding="utf-8")
        link_path = self.workspace / "linked_secret.py"
        try:
            link_path.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        server._SYMBOL_CACHE.clear()
        result = server.lookup_symbol({"name": "leaked_secret"})
        structured = result["structuredContent"]
        self.assertEqual(structured["matches"], [])
        self.assertGreaterEqual(structured["skipped_files"], 1)
        self.assertTrue(any("symlink escapes workspace root" in warning for warning in structured["warnings"]))

    def test_max_memories_invalid_env_falls_back(self) -> None:
        os.environ["MNEMO_MAX_MEMORIES"] = "garbage"
        self.assertEqual(server.max_memories(), 5000)

    def test_symbol_ttl_invalid_env_falls_back(self) -> None:
        os.environ["MNEMO_SYMBOL_TTL_SECONDS"] = "garbage"
        self.assertEqual(server.symbol_ttl_seconds(), 5.0)

    def test_record_uses_safe_consolidate_threshold_parser(self) -> None:
        os.environ["MNEMO_CONSOLIDATE_THRESHOLD"] = "abc"
        result = server.record_memory({"kind": "note", "text": "safe threshold parse"})
        self.assertFalse(result["isError"], result)


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
        server._SQLITE_SCHEMA_READY.clear()
        server._SQLITE_SCHEMA_READY.clear()
        server._SQLITE_SCHEMA_READY.clear()
        server._SQLITE_SCHEMA_READY.clear()
        server._SQLITE_SCHEMA_READY.clear()

    def _write_json_store(self, path: Path, memories: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump({"version": 1, "memories": memories}, f)

    def _git_available(self) -> bool:
        proc = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False)
        return proc.returncode == 0

    def _run_git(self, args: list[str]) -> None:
        proc = subprocess.run(["git", *args], cwd=str(self.workspace), capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise AssertionError(f"git command failed: {' '.join(args)} :: {proc.stderr.strip()}")

    def _init_git_repo(self) -> None:
        if not self._git_available():
            self.skipTest("git is not available")
        self._run_git(["init"])
        self._run_git(["config", "user.email", "mnemo-tests@example.com"])
        self._run_git(["config", "user.name", "Mnemo Tests"])

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

    def test_sqlite_search_does_not_rerun_migration_updates_after_ready(self) -> None:
        memory = self.record("search migration sentinel", kind="note")
        server.search_memories({"query": "search migration sentinel", "limit": 5})
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            before = conn.execute("SELECT updated_at FROM memories WHERE id = ?", (memory["id"],)).fetchone()[0]
        finally:
            conn.close()
        server.search_memories({"query": "search migration sentinel", "limit": 5})
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            after = conn.execute("SELECT updated_at FROM memories WHERE id = ?", (memory["id"],)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(before, after)

    def test_sqlite_record_upserts_only_new_row(self) -> None:
        for index in range(5):
            self.record(f"existing row {index}", kind="note")
        real_upsert = server._sqlite_upsert_memory
        calls: list[str] = []

        def wrapped(conn: sqlite3.Connection, memory: dict[str, Any], *args: Any, **kwargs: Any):
            calls.append(str(memory.get("id")))
            return real_upsert(conn, memory, *args, **kwargs)

        with mock.patch("server._sqlite_upsert_memory", side_effect=wrapped):
            result = server.record_memory({"kind": "note", "text": "single row write check"})
        self.assertFalse(result["isError"], result)
        self.assertEqual(len(calls), 1)

    def test_sqlite_connect_sets_busy_timeout(self) -> None:
        conn = server._sqlite_connect()
        try:
            timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(int(timeout), 5000)

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

    def test_migration_idempotent(self) -> None:
        server.load_store()
        server.load_store()
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            memory_cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        finally:
            conn.close()
        self.assertIn("git_sha", memory_cols)
        self.assertIn("git_branch", memory_cols)
        self.assertIn("git_dirty", memory_cols)
        self.assertIn("memory_files", tables)

    def test_legacy_row_neutral(self) -> None:
        server.load_store()
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            conn.execute(
                """
                INSERT INTO memories(
                    id, kind, text, source, tags_json, linked_ids_json, metadata_json,
                    pinned, deleted, created_at, token_estimate, git_sha, git_branch, git_dirty
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-neutral",
                    "note",
                    "legacy neutral marker",
                    "legacy",
                    "[]",
                    "[]",
                    "{}",
                    0,
                    0,
                    server.now_iso(),
                    1,
                    None,
                    None,
                    None,
                ),
            )
            conn.commit()
            multiplier = server.freshness_multiplier(conn, "note", "legacy-neutral", str(self.workspace))
        finally:
            conn.close()
        self.assertEqual(multiplier, 1.0)

    def test_fresh_drifted_stale(self) -> None:
        self._init_git_repo()
        a_path = self.workspace / "a.txt"
        b_path = self.workspace / "b.txt"
        a_path.write_text("alpha\n", encoding="utf-8")
        b_path.write_text("beta\n", encoding="utf-8")
        self._run_git(["add", "a.txt", "b.txt"])
        self._run_git(["commit", "-m", "seed files"])

        stored = self.record(
            "fresh drift stale marker",
            kind="note",
            touched_files=["a.txt", "b.txt"],
        )
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            conn.row_factory = sqlite3.Row
            fresh = server.freshness_multiplier(conn, "note", stored["id"], str(self.workspace))
            self.assertEqual(fresh, 1.0)
            a_path.write_text("alpha-updated\n", encoding="utf-8")
            drifted = server.freshness_multiplier(conn, "note", stored["id"], str(self.workspace))
            self.assertEqual(drifted, 0.7)
            b_path.unlink()
            stale = server.freshness_multiplier(conn, "note", stored["id"], str(self.workspace))
            self.assertEqual(stale, 0.3)
        finally:
            conn.close()

    def test_no_git_fallback(self) -> None:
        path = self.workspace / "fallback.txt"
        path.write_text("fallback bytes\n", encoding="utf-8")
        stored = self.record(
            "no git fallback marker",
            kind="note",
            touched_files=["fallback.txt"],
        )
        self.assertIsNone(stored.get("git_sha"))
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT path, file_sha FROM memory_files WHERE memory_table = ? AND memory_id = ?",
                ("note", stored["id"]),
            ).fetchall()
            self.assertGreaterEqual(len(rows), 1)
            self.assertTrue(any(str(row["file_sha"]).strip() for row in rows))
            multiplier = server.freshness_multiplier(conn, "note", stored["id"], str(self.workspace))
        finally:
            conn.close()
        self.assertEqual(multiplier, 1.0)

    def test_retrieval_reorder(self) -> None:
        self._init_git_repo()
        fresh_path = self.workspace / "fresh.txt"
        stale_path = self.workspace / "stale.txt"
        fresh_path.write_text("fresh\n", encoding="utf-8")
        stale_path.write_text("stale\n", encoding="utf-8")
        self._run_git(["add", "fresh.txt", "stale.txt"])
        self._run_git(["commit", "-m", "seed retrieval files"])

        fresh = self.record(
            "retrieval reorder fresh marker",
            kind="note",
            touched_files=["fresh.txt"],
        )
        stale = self.record(
            "retrieval reorder stale marker",
            kind="note",
            touched_files=["stale.txt"],
        )
        stale_path.unlink()
        store = self.read_store()
        by_id = {str(item.get("id")): item for item in store.get("memories", []) if isinstance(item, dict)}
        stale_memory = by_id[stale["id"]]
        fresh_memory = by_id[fresh["id"]]
        with mock.patch("server.rank_memories_for_query", return_value=[(0.8, stale_memory), (0.8, fresh_memory)]):
            matches = server.search_rank({"query": "retrieval reorder", "limit": 2})
        self.assertEqual(matches[0]["id"], fresh["id"])
        self.assertEqual(matches[1]["id"], stale["id"])

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


class MemoryPacksPhase1Tests(MnemoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sqlite_file = self.root / "mnemo" / "mnemo.sqlite"
        os.environ["MNEMO_STORE"] = "sqlite"
        os.environ["MNEMO_SQLITE_FILE"] = str(self.sqlite_file)
        server._SQLITE_BOOTSTRAPPED.clear()
        server._SQLITE_SCHEMA_READY.clear()
        server._SQLITE_SCHEMA_READY.clear()
        server._SQLITE_SCHEMA_READY.clear()
        server._SQLITE_SCHEMA_READY.clear()

    def _create_pre_phase1_schema(self, rows: list[dict[str, Any]], *, with_fts: bool = False) -> None:
        self.sqlite_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            conn.execute(
                """
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
                    git_sha TEXT,
                    git_branch TEXT,
                    git_dirty INTEGER,
                    metadata_json TEXT,
                    pinned INTEGER DEFAULT 0,
                    deleted INTEGER DEFAULT 0,
                    superseded_by TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    token_estimate INTEGER,
                    content_hash TEXT,
                    normalized_hash TEXT,
                    token_count INTEGER,
                    unique_token_count INTEGER,
                    top_terms_json TEXT,
                    shingle_hashes_json TEXT,
                    signature_version INTEGER,
                    normalizer_version INTEGER,
                    signature_updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS links (
                    source_id TEXT,
                    target_id TEXT,
                    relation TEXT,
                    created_at TEXT,
                    PRIMARY KEY (source_id, target_id, relation)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    memory_id TEXT,
                    event_type TEXT,
                    data_json TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS idf_profiles (
                    scope TEXT NOT NULL,
                    name TEXT NOT NULL,
                    profile_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    active INTEGER NOT NULL,
                    doc_count INTEGER NOT NULL,
                    unique_terms INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    min_documents INTEGER NOT NULL,
                    min_unique_terms INTEGER NOT NULL,
                    min_total_tokens INTEGER NOT NULL,
                    corpus_signature TEXT,
                    profile_json TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (scope, name, profile_version)
                )
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', '3')"
            )
            if with_fts:
                conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(id UNINDEXED, text, title, tags)")
            for row in rows:
                migrated = server.migrate_memory(dict(row))
                text = str(migrated.get("text", ""))
                signature = server._build_memory_signature(text)
                metadata_json = json.dumps(server.normalize_metadata(migrated.get("metadata")), ensure_ascii=False)
                tags_json = json.dumps(server.normalize_tags(migrated.get("tags", [])), ensure_ascii=False)
                linked_ids_json = json.dumps(
                    server.normalize_linked_ids(migrated.get("linked_ids", migrated.get("references", []))),
                    ensure_ascii=False,
                )
                conn.execute(
                    """
                    INSERT INTO memories(
                        id, kind, text, title, preview, source, tags_json, linked_ids_json,
                        agent_id, role, scope, domain, authority, retention, confidence,
                        parent_id, source_run_id, git_sha, git_branch, git_dirty,
                        metadata_json, pinned, deleted, superseded_by, created_at, updated_at,
                        token_estimate, content_hash, normalized_hash, token_count, unique_token_count,
                        top_terms_json, shingle_hashes_json, signature_version, normalizer_version, signature_updated_at
                    ) VALUES(
                        ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        str(migrated.get("id", "")),
                        str(migrated.get("kind", "note")),
                        text,
                        None,
                        text[:240],
                        str(migrated.get("source", "")),
                        tags_json,
                        linked_ids_json,
                        server.normalize_optional_string(migrated.get("agent_id")),
                        server.normalize_optional_string(migrated.get("role")),
                        server.normalize_optional_string(migrated.get("scope")),
                        server.normalize_optional_string(migrated.get("domain")),
                        server.normalize_optional_string(migrated.get("authority")),
                        server.normalize_optional_string(migrated.get("retention")),
                        server.normalize_optional_string(migrated.get("confidence")),
                        server.normalize_optional_string(migrated.get("parent_id")),
                        server.normalize_optional_string(migrated.get("source_run_id")),
                        server.normalize_optional_string(migrated.get("git_sha")),
                        server.normalize_optional_string(migrated.get("git_branch")),
                        server.normalize_git_dirty(migrated.get("git_dirty")),
                        metadata_json,
                        1 if bool(migrated.get("pinned")) else 0,
                        1 if bool(migrated.get("deleted_at")) else 0,
                        server.normalize_optional_string(migrated.get("superseded_by")),
                        str(migrated.get("created_at") or server.now_iso()),
                        server.normalize_optional_string(migrated.get("updated_at")),
                        int(server.estimate_tokens(text)),
                        signature["content_hash"],
                        signature["normalized_hash"],
                        int(signature["token_count"]),
                        int(signature["unique_token_count"]),
                        signature["top_terms_json"],
                        signature["shingle_hashes_json"],
                        int(signature["signature_version"]),
                        int(signature["normalizer_version"]),
                        signature["signature_updated_at"],
                    ),
                )
                if with_fts:
                    conn.execute(
                        "INSERT OR REPLACE INTO memories_fts(id, text, title, tags) VALUES(?, ?, ?, ?)",
                        (
                            str(migrated.get("id", "")),
                            text,
                            "",
                            " ".join(str(tag) for tag in migrated.get("tags", []) if str(tag).strip()),
                        ),
                    )
            conn.commit()
        finally:
            conn.close()
        server._SQLITE_BOOTSTRAPPED.clear()
        server._SQLITE_SCHEMA_READY.clear()
        server._SQLITE_SCHEMA_READY.clear()
        server._SQLITE_SCHEMA_READY.clear()
        server._SQLITE_SCHEMA_READY.clear()

    def _insert_imported_pack(self, *, pack_id: str, trust_level: str, namespace: str) -> None:
        server.load_store()
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO imported_packs(
                    pack_id, pack_name, source_label, trust_level, namespace,
                    imported_at, manifest_json, freshness_summary_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pack_id,
                    f"Pack {pack_id}",
                    "unit-test",
                    trust_level,
                    namespace,
                    server.now_iso(),
                    "{}",
                    None,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        server._SQLITE_BOOTSTRAPPED.clear()

    def _insert_imported_pack_unchecked(self, *, pack_id: str, trust_level: str, namespace: str) -> None:
        server.load_store()
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute(
                """
                INSERT OR REPLACE INTO imported_packs(
                    pack_id, pack_name, source_label, trust_level, namespace,
                    imported_at, manifest_json, freshness_summary_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pack_id,
                    f"Pack {pack_id}",
                    "unit-test",
                    trust_level,
                    namespace,
                    server.now_iso(),
                    "{}",
                    None,
                ),
            )
            conn.commit()
        finally:
            try:
                conn.execute("PRAGMA ignore_check_constraints = OFF")
            except Exception:
                pass
            conn.close()
        server._SQLITE_BOOTSTRAPPED.clear()

    def _pack_preview(self, **params: Any) -> dict[str, Any]:
        result = server.pack_preview(dict(params))
        self.assertFalse(result["isError"], result)
        return result

    def _memory_group_discover(self, **params: Any) -> dict[str, Any]:
        result = server.memory_group_discover(dict(params))
        self.assertFalse(result["isError"], result)
        return result

    def _memory_group_preview(self, **params: Any) -> dict[str, Any]:
        result = server.memory_group_preview(dict(params))
        self.assertFalse(result["isError"], result)
        return result

    def _pack_preview_ids(self, result: dict[str, Any]) -> list[str]:
        selection = result["structuredContent"]["selection"]
        return [str(memory_id) for memory_id in selection["row_ids"]]

    def _set_created_at(self, memory_id: str, created_at: str) -> None:
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            conn.execute("UPDATE memories SET created_at = ? WHERE id = ?", (created_at, memory_id))
            conn.commit()
        finally:
            conn.close()

    def _pack_redaction_preview(self, **params: Any) -> dict[str, Any]:
        result = server.pack_redaction_preview(dict(params))
        self.assertFalse(result["isError"], result)
        return result

    def _exported_packs_count(self) -> int:
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            return int(conn.execute("SELECT COUNT(*) FROM exported_packs").fetchone()[0])
        finally:
            conn.close()

    def _memory_count(self) -> int:
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            return int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        finally:
            conn.close()

    def _memory_text(self, memory_id: str) -> str:
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            row = conn.execute("SELECT text FROM memories WHERE id = ?", (memory_id,)).fetchone()
            return str(row[0]) if row else ""
        finally:
            conn.close()

    def _pack_export(self, **params: Any) -> dict[str, Any]:
        result = server.pack_export(dict(params))
        self.assertFalse(result["isError"], result)
        return result

    def _pack_export_error(self, **params: Any) -> dict[str, Any]:
        result = server.pack_export(dict(params))
        self.assertTrue(result["isError"], result)
        return result

    def _read_zip_members(self, path: Path) -> dict[str, bytes]:
        with zipfile.ZipFile(path, "r") as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def _recompute_pack_content_hash(self, members: dict[str, bytes], covered_members: list[str]) -> str:
        lines: list[str] = []
        for member_name in sorted(str(name) for name in covered_members):
            digest = hashlib.sha256(members[member_name]).hexdigest()
            lines.append(f"{member_name}\t{digest}\n")
        canonical = "".join(lines).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _table_count(self, table: str) -> int:
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            conn.close()

    def _table_exists(self, table: str) -> bool:
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
                (table,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def _query_digest(self, conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> str:
        digest = hashlib.sha256()
        rows = conn.execute(sql, params).fetchall()
        for row in rows:
            digest.update(json.dumps([item for item in row], ensure_ascii=False, sort_keys=False).encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()

    def _read_only_snapshot(self) -> dict[str, Any]:
        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            watched_tables = [
                "memories",
                "memory_topics",
                "memory_files",
                "imported_packs",
                "exported_packs",
                "imported_pack_rows",
                "promoted_pack_rows",
                "promotion_audit",
                "trusted_signers",
                "alias_concepts",
                "alias_terms",
                "alias_proposals",
                "alias_proposal_events",
            ]
            table_counts: dict[str, int] = {}
            for table in watched_tables:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
                    (table,),
                ).fetchone()
                if exists is not None:
                    table_counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

            fts_tables = [
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE 'memories_fts%' OR name LIKE 'events_fts%') ORDER BY name"
                ).fetchall()
            ]
            fts_counts = {name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]) for name in fts_tables}

            digests = {
                "memories": self._query_digest(
                    conn,
                    "SELECT id, kind, namespace, origin, COALESCE(import_freshness, ''), COALESCE(text, ''), COALESCE(title, '') "
                    "FROM memories ORDER BY id ASC",
                )
                if "memories" in table_counts
                else "",
                "memory_topics": self._query_digest(
                    conn,
                    "SELECT memory_id, topic, COALESCE(source, '') FROM memory_topics ORDER BY memory_id ASC, topic ASC",
                )
                if "memory_topics" in table_counts
                else "",
                "memory_files": self._query_digest(
                    conn,
                    "SELECT memory_table, memory_id, path, file_sha FROM memory_files ORDER BY memory_table ASC, memory_id ASC, path ASC",
                )
                if "memory_files" in table_counts
                else "",
                "imported_packs": self._query_digest(
                    conn,
                    "SELECT pack_id, namespace, trust_level, COALESCE(received_zip_sha256, '') FROM imported_packs ORDER BY pack_id ASC",
                )
                if "imported_packs" in table_counts
                else "",
                "exported_packs": self._query_digest(
                    conn,
                    "SELECT pack_id, pack_name, exported_at, row_count, redaction_count, signed FROM exported_packs ORDER BY pack_id ASC",
                )
                if "exported_packs" in table_counts
                else "",
                "imported_pack_rows": self._query_digest(
                    conn,
                    "SELECT pack_id, row_id_in_pack, memory_id, kind FROM imported_pack_rows ORDER BY pack_id ASC, row_id_in_pack ASC",
                )
                if "imported_pack_rows" in table_counts
                else "",
                "promoted_pack_rows": self._query_digest(
                    conn,
                    "SELECT pack_id, row_id_in_pack, imported_memory_id, promoted_memory_id, COALESCE(promotion_id, '') "
                    "FROM promoted_pack_rows ORDER BY pack_id ASC, row_id_in_pack ASC",
                )
                if "promoted_pack_rows" in table_counts
                else "",
                "promotion_audit": self._query_digest(
                    conn,
                    "SELECT promotion_id, pack_id, promoted_at, row_count, limited FROM promotion_audit ORDER BY promotion_id ASC",
                )
                if "promotion_audit" in table_counts
                else "",
                "trusted_signers": self._query_digest(
                    conn,
                    "SELECT signer_id, trust_level, signature_algorithm, COALESCE(secret_fingerprint, ''), status "
                    "FROM trusted_signers ORDER BY signer_id ASC",
                )
                if "trusted_signers" in table_counts
                else "",
            }
            return {"counts": table_counts, "fts_counts": fts_counts, "digests": digests}
        finally:
            conn.close()

    def _reset_sqlite_file(self) -> None:
        if self.sqlite_file.exists():
            self.sqlite_file.unlink()
        self.sqlite_file.parent.mkdir(parents=True, exist_ok=True)
        server._SQLITE_BOOTSTRAPPED.clear()
        if hasattr(server, "_SQLITE_SCHEMA_READY"):
            server._SQLITE_SCHEMA_READY.clear()

    def _pack_inspect(self, **params: Any) -> dict[str, Any]:
        result = server.pack_inspect(dict(params))
        self.assertFalse(result["isError"], result)
        return result

    def _pack_import(self, **params: Any) -> dict[str, Any]:
        result = server.pack_import(dict(params))
        self.assertFalse(result["isError"], result)
        return result

    def _pack_import_error(self, **params: Any) -> dict[str, Any]:
        result = server.pack_import(dict(params))
        self.assertTrue(result["isError"], result)
        return result

    def _pack_landing_list(self, **params: Any) -> dict[str, Any]:
        result = server.pack_landing_list(dict(params))
        self.assertFalse(result["isError"], result)
        return result

    def _signer_add(self, **params: Any) -> dict[str, Any]:
        result = server.signer_add(dict(params))
        self.assertFalse(result["isError"], result)
        return result

    def _signer_add_error(self, **params: Any) -> dict[str, Any]:
        result = server.signer_add(dict(params))
        self.assertTrue(result["isError"], result)
        return result

    def _signer_list(self, **params: Any) -> dict[str, Any]:
        result = server.signer_list(dict(params))
        self.assertFalse(result["isError"], result)
        return result

    def _signer_disable(self, **params: Any) -> dict[str, Any]:
        result = server.signer_disable(dict(params))
        self.assertFalse(result["isError"], result)
        return result

    def _signer_disable_error(self, **params: Any) -> dict[str, Any]:
        result = server.signer_disable(dict(params))
        self.assertTrue(result["isError"], result)
        return result

    def _signer_enable(self, **params: Any) -> dict[str, Any]:
        result = server.signer_enable(dict(params))
        self.assertFalse(result["isError"], result)
        return result

    def _signer_enable_error(self, **params: Any) -> dict[str, Any]:
        result = server.signer_enable(dict(params))
        self.assertTrue(result["isError"], result)
        return result

    def test_pack_landing_list_returns_mem_files_and_ignores_non_mem_by_default(self) -> None:
        landing_dir = self.root / "landing"
        landing_dir.mkdir(parents=True, exist_ok=True)
        newest = landing_dir / "recent.mem"
        older = landing_dir / "older.mem"
        ignored = landing_dir / "notes.txt"
        legacy = landing_dir / "legacy.zip"
        older.write_bytes(b"older")
        newest.write_bytes(b"newer")
        ignored.write_text("ignore", encoding="utf-8")
        legacy.write_bytes(b"legacy")
        os.environ["MNEMO_PACK_LANDING_DIR"] = str(landing_dir)
        now = time.time()
        os.utime(older, (now - 10, now - 10))
        os.utime(newest, (now, now))
        os.utime(legacy, (now - 5, now - 5))

        result = self._pack_landing_list(limit=10)
        payload = result["structuredContent"]
        self.assertEqual(payload["action"], "pack_landing_list")
        self.assertEqual(payload["landing_dir"], str(landing_dir.resolve()))
        self.assertTrue(payload["landing_dir_exists"])
        self.assertEqual(payload["total"], 2)
        self.assertEqual([item["filename"] for item in payload["packs"]], ["recent.mem", "older.mem"])
        self.assertEqual([item["suffix"] for item in payload["packs"]], [".mem", ".mem"])

    def test_pack_landing_list_optional_legacy_zip_inclusion_works(self) -> None:
        landing_dir = self.root / "landing"
        landing_dir.mkdir(parents=True, exist_ok=True)
        (landing_dir / "trusted.mem").write_bytes(b"mem")
        (landing_dir / "legacy.zip").write_bytes(b"zip")
        os.environ["MNEMO_PACK_LANDING_DIR"] = str(landing_dir)

        result = self._pack_landing_list(include_legacy_zip=True, limit=10)
        payload = result["structuredContent"]
        filenames = [item["filename"] for item in payload["packs"]]
        self.assertEqual(payload["total"], 2)
        self.assertIn("trusted.mem", filenames)
        self.assertIn("legacy.zip", filenames)
        legacy_row = next(item for item in payload["packs"] if item["filename"] == "legacy.zip")
        self.assertTrue(legacy_row["legacy_zip"])

    def _pack_list_imports(self, **params: Any) -> dict[str, Any]:
        result = server.pack_list_imports(dict(params))
        self.assertFalse(result["isError"], result)
        return result

    def _pack_review_import(self, **params: Any) -> dict[str, Any]:
        result = server.pack_review_import(dict(params))
        self.assertFalse(result["isError"], result)
        return result

    def _pack_review_import_error(self, **params: Any) -> dict[str, Any]:
        result = server.pack_review_import(dict(params))
        self.assertTrue(result["isError"], result)
        return result

    def _pack_promote_preview(self, **params: Any) -> dict[str, Any]:
        result = server.pack_promote_preview(dict(params))
        self.assertFalse(result["isError"], result)
        return result

    def _pack_promote_preview_error(self, **params: Any) -> dict[str, Any]:
        result = server.pack_promote_preview(dict(params))
        self.assertTrue(result["isError"], result)
        return result

    def _pack_promote(self, **params: Any) -> dict[str, Any]:
        result = server.pack_promote(dict(params))
        self.assertFalse(result["isError"], result)
        return result

    def _pack_promote_error(self, **params: Any) -> dict[str, Any]:
        result = server.pack_promote(dict(params))
        self.assertTrue(result["isError"], result)
        return result

    def _create_exported_pack(self, *, pack_name: str, output_dir: Path, **params: Any) -> Path:
        export_params = {
            "pack_name": pack_name,
            "output_dir": str(output_dir),
            "allow_unsigned": True,
        }
        export_params.update(params)
        result = self._pack_export(**export_params)
        return Path(result["structuredContent"]["output_path"])

    def _create_signed_exported_pack(
        self,
        *,
        pack_name: str,
        output_dir: Path,
        signer_id: str,
        signing_secret: str,
        **params: Any,
    ) -> tuple[Path, dict[str, Any]]:
        export_params = {
            "pack_name": pack_name,
            "output_dir": str(output_dir),
            "sign_pack": True,
            "signer_id": signer_id,
            "signing_secret": signing_secret,
        }
        export_params.update(params)
        result = self._pack_export(**export_params)
        return Path(result["structuredContent"]["output_path"]), result["structuredContent"]

    def _create_trusted_import_fixture(
        self,
        *,
        marker: str,
        kind: str = "context_block",
        touched_files: list[str] | None = None,
    ) -> dict[str, Any]:
        signer_id = f"{marker}.trusted.signer"
        secret = f"{marker}-trusted-secret-012345678901234567890123"
        self._signer_add(signer_id=signer_id, secret=secret, trust_level="trusted")
        source = self.record(
            f"{marker} trusted import source",
            kind=kind,
            title=f"{marker} trusted title",
            touched_files=list(touched_files or []),
        )
        topic = f"{marker}-trusted-topic"
        add_topic = server.topic_add({"memory_id": str(source["id"]), "topic": topic, "source": "operator"})
        self.assertFalse(add_topic["isError"], add_topic)
        pack_path, _ = self._create_signed_exported_pack(
            pack_name=f"{marker}_trusted_pack",
            output_dir=self.root / f"{marker}_trusted_pack",
            signer_id=signer_id,
            signing_secret=secret,
            topics=[topic],
            kinds=[kind],
        )
        inspected = self._pack_inspect(pack_path=str(pack_path), verification_secret=secret)
        imported = self._pack_import(
            pack_path=str(pack_path),
            allow_trusted_import=True,
            verification_secret=secret,
        )
        return {
            "signer_id": signer_id,
            "secret": secret,
            "topic": topic,
            "source_memory_id": str(source["id"]),
            "pack_path": str(pack_path),
            "inspect": inspected,
            "imported": imported,
        }

    def _create_signed_pack_fixture(
        self,
        *,
        marker: str,
        trust_level: str = "trusted",
        signer_id: str | None = None,
        secret: str | None = None,
    ) -> dict[str, Any]:
        signer = signer_id or f"{marker}.signer"
        signing_secret = secret or f"{marker}-secret-012345678901234567890123"
        self._signer_add(signer_id=signer, secret=signing_secret, trust_level=trust_level)
        source = self.record(f"{marker} signed source", kind="context_block", title=f"{marker} signed title")
        topic = f"{marker}-topic"
        add_topic = server.topic_add({"memory_id": str(source["id"]), "topic": topic, "source": "operator"})
        self.assertFalse(add_topic["isError"], add_topic)
        pack_path, export_sc = self._create_signed_exported_pack(
            pack_name=f"{marker}_signed_pack",
            output_dir=self.root / f"{marker}_signed_pack",
            signer_id=signer,
            signing_secret=signing_secret,
            topics=[topic],
            kinds=["context_block"],
        )
        return {
            "signer_id": signer,
            "secret": signing_secret,
            "topic": topic,
            "source_memory_id": str(source["id"]),
            "pack_path": str(pack_path),
            "export": export_sc,
        }

    def _pack_error_code(self, result: dict[str, Any]) -> str:
        structured = result.get("structuredContent", {}) if isinstance(result, dict) else {}
        error = structured.get("error", {}) if isinstance(structured, dict) else {}
        return str(error.get("code", ""))

    def _pack_warning_codes(self, result: dict[str, Any]) -> set[str]:
        structured = result.get("structuredContent", {}) if isinstance(result, dict) else {}
        warnings = structured.get("warnings", []) if isinstance(structured, dict) else []
        if not isinstance(warnings, list):
            return set()
        out: set[str] = set()
        for item in warnings:
            if isinstance(item, dict) and item.get("code") is not None:
                out.add(str(item.get("code")))
        return out

    def _create_phase4b_imported_pack(
        self,
        *,
        marker: str,
        rows: int = 3,
        kinds: list[str] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        kinds_list = list(kinds or ["context_block", "hippocampus_entry"])
        touch_a = self.workspace / "src" / "phase4b" / "auth.py"
        touch_b = self.workspace / "src" / "phase4b" / "billing.py"
        touch_a.parent.mkdir(parents=True, exist_ok=True)
        touch_b.parent.mkdir(parents=True, exist_ok=True)
        touch_a.write_text("AUTH='phase4b'\n", encoding="utf-8")
        touch_b.write_text("BILLING='phase4b'\n", encoding="utf-8")

        created_ids: list[str] = []
        for idx in range(rows):
            kind_name = kinds_list[idx % len(kinds_list)]
            touched = ["src/phase4b/auth.py"] if idx % 2 == 0 else ["src/phase4b/billing.py"]
            recorded = self.record(
                f"{marker} row {idx}",
                kind=kind_name,
                title=f"{marker} title {idx}",
                touched_files=touched,
            )
            created_ids.append(str(recorded["id"]))
            add_topic = server.topic_add(
                {"memory_id": str(recorded["id"]), "topic": f"{marker}-topic-{idx:02d}", "source": "operator"}
            )
            self.assertFalse(add_topic["isError"], add_topic)

        pack_path = self._create_exported_pack(
            pack_name=marker,
            output_dir=self.root / marker,
            kinds=sorted(set(kinds_list)),
            limit=500,
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        imported_sc = imported["structuredContent"]
        return str(imported_sc["pack_id"]), imported_sc

    def _pack_rows(self, pack_id: str) -> list[tuple[str, str, str]]:
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            rows = conn.execute(
                """
                SELECT row_id_in_pack, memory_id, kind
                FROM imported_pack_rows
                WHERE pack_id = ?
                ORDER BY row_id_in_pack ASC
                """,
                (pack_id,),
            ).fetchall()
            return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]
        finally:
            conn.close()

    def _rewrite_zip(
        self,
        source_path: Path,
        dest_path: Path,
        *,
        remove_members: set[str] | None = None,
        replace_members: dict[str, bytes] | None = None,
        extra_members: dict[str, bytes] | None = None,
        duplicate_member: tuple[str, bytes] | None = None,
    ) -> None:
        remove_members = remove_members or set()
        replace_members = replace_members or {}
        extra_members = extra_members or {}
        with zipfile.ZipFile(source_path, "r") as src:
            original_members = {name: src.read(name) for name in src.namelist()}
        with zipfile.ZipFile(dest_path, "w", compression=zipfile.ZIP_DEFLATED) as dst:
            for name in sorted(original_members):
                if name in remove_members:
                    continue
                if name in replace_members:
                    dst.writestr(name, replace_members[name])
                else:
                    dst.writestr(name, original_members[name])
            for name, data in extra_members.items():
                dst.writestr(name, data)
            if duplicate_member is not None:
                dst.writestr(duplicate_member[0], duplicate_member[1])

    def test_memory_packs_phase1_migration_idempotent(self) -> None:
        server.load_store()
        server.load_store()
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            memory_cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
            schema_version = int(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
        finally:
            conn.close()
        self.assertTrue({"namespace", "origin", "import_freshness"}.issubset(memory_cols))
        self.assertIn("memory_topics", tables)
        self.assertIn("imported_packs", tables)
        self.assertIn("imported_pack_rows", tables)
        self.assertIn("exported_packs", tables)
        self.assertIn("promoted_pack_rows", tables)
        self.assertIn("promotion_audit", tables)
        self.assertIn("idx_memories_namespace", indexes)
        self.assertIn("idx_memories_origin", indexes)
        self.assertIn("idx_memories_namespace_kind", indexes)
        self.assertIn("idx_memory_topics_topic", indexes)
        self.assertIn("idx_memory_topics_memory_id", indexes)
        self.assertIn("idx_promoted_pack_rows_pack_id", indexes)
        self.assertIn("idx_promoted_pack_rows_promoted_memory_id", indexes)
        self.assertIn("idx_promotion_audit_pack_id", indexes)
        self.assertGreaterEqual(schema_version, 7)

    def test_memory_packs_phase1_existing_rows_default_local(self) -> None:
        legacy = server.new_memory("legacy-v3", "note", "legacy body text", "", [])
        legacy.pop("namespace", None)
        legacy.pop("origin", None)
        legacy.pop("import_freshness", None)
        self._create_pre_phase1_schema([legacy])
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            before_count = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            before_text = str(conn.execute("SELECT text FROM memories WHERE id = 'legacy-v3'").fetchone()[0])
        finally:
            conn.close()
        server.load_store()
        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM memories WHERE id = 'legacy-v3'").fetchone()
            after_count = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        finally:
            conn.close()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(before_count, after_count)
        self.assertEqual(before_text, str(row["text"]))
        self.assertEqual(str(row["namespace"]), "local")
        self.assertEqual(str(row["origin"]), "local")
        self.assertIsNone(row["import_freshness"])

    def test_memory_packs_phase1_default_retrieval_unchanged(self) -> None:
        rows = [
            server.new_memory("legacy-a", "note", "alpha beta gamma", "", []),
            server.new_memory("legacy-b", "note", "alpha beta", "", []),
            server.new_memory("legacy-c", "note", "alpha", "", []),
        ]
        for row in rows:
            row.pop("namespace", None)
            row.pop("origin", None)
            row.pop("import_freshness", None)
        baseline_memories = [server.migrate_memory(dict(row)) for row in rows]
        baseline_ranked = server.rank_memories_for_query(
            baseline_memories,
            server.tokenize("alpha beta"),
            phase=None,
            query_text="alpha beta",
        )
        baseline_ids = [str(memory.get("id")) for score, memory in baseline_ranked[:3] if float(score) > 0.0]
        self._create_pre_phase1_schema(rows)
        result = server.search_memories({"query": "alpha beta", "limit": 3})
        self.assertFalse(result["isError"], result)
        ids = [str(item["id"]) for item in result["structuredContent"]["matches"]]
        self.assertEqual(ids, baseline_ids)

    def test_topic_add_remove_list(self) -> None:
        memory = self.record("topic add remove marker", kind="note")
        added = server.topic_add({"memory_id": memory["id"], "topic": "auth", "source": "operator"})
        self.assertFalse(added["isError"], added)
        self.assertTrue(added["structuredContent"]["inserted"])
        dup = server.topic_add({"memory_id": memory["id"], "topic": "auth"})
        self.assertFalse(dup["isError"], dup)
        self.assertFalse(dup["structuredContent"]["inserted"])
        other = server.topic_add({"memory_id": memory["id"], "topic": "release", "source": "maintenance"})
        self.assertFalse(other["isError"], other)
        all_topics = server.topic_list({})
        self.assertFalse(all_topics["isError"], all_topics)
        by_topic = {row["topic"]: int(row["count"]) for row in all_topics["structuredContent"]["topics"]}
        self.assertEqual(by_topic.get("auth"), 1)
        self.assertEqual(by_topic.get("release"), 1)
        removed = server.topic_remove({"memory_id": memory["id"], "topic": "auth"})
        self.assertFalse(removed["isError"], removed)
        self.assertEqual(int(removed["structuredContent"]["removed"]), 1)
        all_after = server.topic_list({})
        by_topic_after = {row["topic"]: int(row["count"]) for row in all_after["structuredContent"]["topics"]}
        self.assertNotIn("auth", by_topic_after)

    def test_topic_list_scope_memory(self) -> None:
        left = self.record("topic scope left", kind="note")
        right = self.record("topic scope right", kind="note")
        server.topic_add({"memory_id": left["id"], "topic": "auth"})
        server.topic_add({"memory_id": left["id"], "topic": "gateway"})
        server.topic_add({"memory_id": right["id"], "topic": "billing"})
        scoped = server.topic_list({"scope": "memory", "memory_id": left["id"]})
        self.assertFalse(scoped["isError"], scoped)
        topics = {row["topic"] for row in scoped["structuredContent"]["topics"]}
        self.assertEqual(topics, {"auth", "gateway"})

    def test_retrieval_excludes_imported_by_default(self) -> None:
        self._insert_imported_pack(pack_id="pack-test", trust_level="trusted", namespace="pack:test")
        imported = self.record(
            "imported trusted namespace marker",
            kind="note",
            namespace="pack:test",
            origin="imported",
        )
        default = server.search_memories({"query": "imported trusted namespace marker", "limit": 5})
        self.assertFalse(default["isError"], default)
        self.assertEqual(default["structuredContent"]["matches"], [])
        included = server.search_memories(
            {"query": "imported trusted namespace marker", "limit": 5, "include_imported": True}
        )
        self.assertFalse(included["isError"], included)
        ids = [row["id"] for row in included["structuredContent"]["matches"]]
        self.assertIn(imported["id"], ids)

    def test_retrieval_excludes_quarantine_unless_opted_in(self) -> None:
        self._insert_imported_pack(
            pack_id="pack-quarantine-test",
            trust_level="quarantine",
            namespace="pack:quarantine:test",
        )
        quarantined = self.record(
            "quarantine namespace marker",
            kind="note",
            namespace="pack:quarantine:test",
            origin="imported",
        )
        default = server.search_memories({"query": "quarantine namespace marker", "limit": 5})
        self.assertFalse(default["isError"], default)
        self.assertEqual(default["structuredContent"]["matches"], [])
        imported_only = server.search_memories(
            {"query": "quarantine namespace marker", "limit": 5, "include_imported": True}
        )
        self.assertFalse(imported_only["isError"], imported_only)
        self.assertEqual(imported_only["structuredContent"]["matches"], [])
        quarantine = server.search_memories(
            {"query": "quarantine namespace marker", "limit": 5, "include_quarantine": True}
        )
        self.assertFalse(quarantine["isError"], quarantine)
        ids = [row["id"] for row in quarantine["structuredContent"]["matches"]]
        self.assertIn(quarantined["id"], ids)

    def test_namespace_and_namespaces_conflict_errors(self) -> None:
        result = server.search_memories(
            {
                "query": "namespace conflict marker",
                "namespace": "local",
                "namespaces": ["local"],
            }
        )
        self.assertTrue(result["isError"])
        self.assertIn("namespace and namespaces cannot both be supplied", result["content"][0]["text"])

    def test_origin_filter_only_when_explicit(self) -> None:
        local_row = self.record(
            "origin filter marker shared",
            kind="note",
            namespace="local",
            origin="local",
        )
        promoted_row = self.record(
            "origin filter marker shared promoted",
            kind="note",
            namespace="local",
            origin="promoted",
        )
        default = server.search_memories({"query": "origin filter marker shared", "limit": 10})
        self.assertFalse(default["isError"], default)
        default_ids = {row["id"] for row in default["structuredContent"]["matches"]}
        self.assertIn(local_row["id"], default_ids)
        self.assertIn(promoted_row["id"], default_ids)

        local_only = server.search_memories(
            {"query": "origin filter marker shared", "limit": 10, "origin": "local"}
        )
        self.assertFalse(local_only["isError"], local_only)
        local_ids = [row["id"] for row in local_only["structuredContent"]["matches"]]
        self.assertTrue(local_ids)
        self.assertEqual(set(local_ids), {local_row["id"]})

        promoted_only = server.search_memories(
            {"query": "origin filter marker shared", "limit": 10, "origins": ["promoted"]}
        )
        self.assertFalse(promoted_only["isError"], promoted_only)
        promoted_ids = [row["id"] for row in promoted_only["structuredContent"]["matches"]]
        self.assertTrue(promoted_ids)
        self.assertEqual(set(promoted_ids), {promoted_row["id"]})

    def test_fts_schema_unchanged(self) -> None:
        legacy = server.new_memory("legacy-fts", "note", "legacy fts schema marker", "", [])
        legacy.pop("namespace", None)
        legacy.pop("origin", None)
        legacy.pop("import_freshness", None)
        self._create_pre_phase1_schema([legacy], with_fts=True)
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            before = conn.execute("PRAGMA table_info(memories_fts)").fetchall()
        finally:
            conn.close()
        server.load_store()
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            after = conn.execute("PRAGMA table_info(memories_fts)").fetchall()
        finally:
            conn.close()
        self.assertEqual(before, after)

    def test_doctor_reports_topics_and_namespaces(self) -> None:
        self._insert_imported_pack(pack_id="pack-topic-doctor", trust_level="trusted", namespace="pack:test")
        local = self.record("doctor topic local", kind="note", namespace="local", origin="local")
        promoted = self.record("doctor topic promoted", kind="note", namespace="local", origin="promoted")
        imported = self.record("doctor topic imported", kind="note", namespace="pack:test", origin="imported")
        server.topic_add({"memory_id": local["id"], "topic": "auth", "source": "operator"})
        server.topic_add({"memory_id": imported["id"], "topic": "auth", "source": "pack_import"})
        server.topic_add({"memory_id": imported["id"], "topic": "release", "source": "pack_import"})
        doctor = server.mnemo_doctor({})
        self.assertFalse(doctor["isError"], doctor)
        payload = doctor["structuredContent"]["memory_packs"]
        self.assertGreaterEqual(int(payload["count_by_namespace"].get("local", 0)), 2)
        self.assertGreaterEqual(int(payload["count_by_namespace"].get("pack:test", 0)), 1)
        self.assertGreaterEqual(int(payload["count_by_origin"].get("local", 0)), 1)
        self.assertGreaterEqual(int(payload["count_by_origin"].get("promoted", 0)), 1)
        self.assertGreaterEqual(int(payload["count_by_origin"].get("imported", 0)), 1)
        self.assertGreaterEqual(int(payload["total_topic_count"]), 3)
        self.assertGreaterEqual(int(payload["untagged_memory_count"]), 1)
        self.assertEqual(int(payload["import_freshness_non_null_count"]), 0)
        self.assertEqual(int(payload["imported_packs_count"]), 1)
        self.assertEqual(int(payload["exported_packs_count"]), 0)
        top_topics = {row["topic"]: int(row["count"]) for row in payload["top_topics"]}
        self.assertEqual(top_topics.get("auth"), 2)

    def test_topic_selection_uses_join_not_body_text(self) -> None:
        body_only = self.record("auth keyword appears in body only", kind="note")
        tagged = self.record("no matching keyword here", kind="note")
        server.topic_add({"memory_id": tagged["id"], "topic": "auth", "source": "operator"})
        listed = server.topic_list({})
        self.assertFalse(listed["isError"], listed)
        by_topic = {row["topic"]: int(row["count"]) for row in listed["structuredContent"]["topics"]}
        self.assertEqual(by_topic.get("auth"), 1)
        body_scope = server.topic_list({"scope": "memory", "memory_id": body_only["id"]})
        self.assertFalse(body_scope["isError"], body_scope)
        self.assertEqual(body_scope["structuredContent"]["topics"], [])

    def test_pack_preview_default_local_only(self) -> None:
        local = self.record("phase2a local preview marker", kind="context_block", namespace="local", origin="local")
        self._insert_imported_pack(pack_id="phase2a-trusted", trust_level="trusted", namespace="pack:phase2a-trusted")
        self._insert_imported_pack(
            pack_id="phase2a-quarantine",
            trust_level="quarantine",
            namespace="pack:quarantine:phase2a-quarantine",
        )
        trusted = self.record(
            "phase2a trusted preview marker",
            kind="context_block",
            namespace="pack:phase2a-trusted",
            origin="imported",
        )
        quarantined = self.record(
            "phase2a quarantine preview marker",
            kind="context_block",
            namespace="pack:quarantine:phase2a-quarantine",
            origin="imported",
        )
        result = self._pack_preview()
        ids = set(self._pack_preview_ids(result))
        self.assertIn(local["id"], ids)
        self.assertNotIn(trusted["id"], ids)
        self.assertNotIn(quarantined["id"], ids)

    def test_pack_preview_topic_filter_uses_memory_topics(self) -> None:
        body_only = self.record("auth appears in body text only", kind="context_block")
        tagged = self.record("no keyword in this memory", kind="context_block")
        topic_add = server.topic_add({"memory_id": tagged["id"], "topic": "auth", "source": "operator"})
        self.assertFalse(topic_add["isError"], topic_add)
        result = self._pack_preview(topics=["auth"])
        ids = self._pack_preview_ids(result)
        self.assertIn(tagged["id"], ids)
        self.assertNotIn(body_only["id"], ids)

    def test_pack_preview_kind_filter(self) -> None:
        context = self.record("phase2a kind context", kind="context_block")
        hippocampus = self.record("phase2a kind hippocampus", kind="hippocampus_entry")
        result = self._pack_preview(kinds=["context_block"])
        ids = set(self._pack_preview_ids(result))
        self.assertIn(context["id"], ids)
        self.assertNotIn(hippocampus["id"], ids)
        self.assertEqual(set(result["structuredContent"]["counts"]["by_kind"].keys()), {"context_block"})

    def test_pack_preview_memory_ids_exact_selector(self) -> None:
        first = self.record("phase213 exact preview first", kind="context_block")
        second = self.record("phase213 exact preview second", kind="hippocampus_entry")
        ignored = self.record("phase213 exact preview ignored", kind="context_block")
        result = self._pack_preview(memory_ids=[str(second["id"]), str(first["id"]), str(second["id"])], limit=50)
        ids = set(self._pack_preview_ids(result))
        self.assertEqual(ids, {str(first["id"]), str(second["id"])})
        self.assertNotIn(str(ignored["id"]), ids)

    def test_pack_preview_group_selector_matches_memory_group_preview(self) -> None:
        core = self.record("group selector preview core", kind="context_block")
        peer = self.record("group selector preview peer", kind="hippocampus_entry")
        related = self.record("group selector preview related", kind="context_block")
        for memory_id in (str(core["id"]), str(peer["id"])):
            added = server.topic_add({"memory_id": memory_id, "topic": "phase216-group-preview", "source": "operator"})
            self.assertFalse(added["isError"], added)
        linked = server.memory_link({"source_id": str(core["id"]), "target_id": str(related["id"]), "relation": "related"})
        self.assertFalse(linked["isError"], linked)
        group = self._memory_group_preview(group_id="topic:phase216-group-preview", scope="core_plus_related", limit=50)
        expected_ids = group["structuredContent"]["selection"]["memory_ids"]

        result = self._pack_preview(group_id="topic:phase216-group-preview", scope="core_plus_related", limit=50)
        structured = result["structuredContent"]
        self.assertEqual(structured["filters"]["group_id"], "topic:phase216-group-preview")
        self.assertEqual(structured["filters"]["scope"], "core_plus_related")
        self.assertEqual(structured["selection"]["row_ids"], expected_ids)

    def test_pack_preview_group_selector_rejects_mixed_selectors(self) -> None:
        result = server.pack_preview({"group_id": "topic:phase216-mixed", "topics": ["phase216-mixed"]})
        self.assertTrue(result["isError"], result)
        self.assertEqual(result["structuredContent"]["error"]["code"], "ambiguous_selector")

    def test_pack_preview_group_selector_unknown_group(self) -> None:
        result = server.pack_preview({"group_id": "topic:missing-phase216-group"})
        self.assertTrue(result["isError"], result)
        self.assertEqual(result["structuredContent"]["error"]["code"], "memory_group_not_found")

    def test_pack_preview_include_imported(self) -> None:
        self._insert_imported_pack(pack_id="phase2a-inc-trusted", trust_level="trusted", namespace="pack:phase2a-inc-trusted")
        self._insert_imported_pack(
            pack_id="phase2a-inc-quarantine",
            trust_level="quarantine",
            namespace="pack:quarantine:phase2a-inc-quarantine",
        )
        self.record("phase2a local include_imported", kind="context_block", namespace="local", origin="local")
        trusted = self.record(
            "phase2a trusted include_imported",
            kind="context_block",
            namespace="pack:phase2a-inc-trusted",
            origin="imported",
        )
        quarantined = self.record(
            "phase2a quarantine include_imported",
            kind="context_block",
            namespace="pack:quarantine:phase2a-inc-quarantine",
            origin="imported",
        )
        result = self._pack_preview(include_imported=True)
        ids = set(self._pack_preview_ids(result))
        self.assertIn(trusted["id"], ids)
        self.assertNotIn(quarantined["id"], ids)

    def test_pack_preview_include_quarantine(self) -> None:
        self._insert_imported_pack(
            pack_id="phase2a-quarantine-only",
            trust_level="quarantine",
            namespace="pack:quarantine:phase2a-quarantine-only",
        )
        quarantined = self.record(
            "phase2a quarantine include_quarantine",
            kind="context_block",
            namespace="pack:quarantine:phase2a-quarantine-only",
            origin="imported",
        )
        result = self._pack_preview(include_quarantine=True)
        ids = set(self._pack_preview_ids(result))
        self.assertIn(quarantined["id"], ids)

    def test_pack_preview_namespace_conflict(self) -> None:
        result = server.pack_preview({"namespace": "local", "namespaces": ["local"]})
        self.assertTrue(result["isError"])
        self.assertIn("namespace and namespaces cannot both be supplied", result["content"][0]["text"])

    def test_pack_preview_origin_conflict(self) -> None:
        result = server.pack_preview({"origin": "local", "origins": ["local"]})
        self.assertTrue(result["isError"])
        self.assertIn("origin and origins cannot both be supplied", result["content"][0]["text"])

    def test_pack_preview_touched_paths_filter(self) -> None:
        auth_file = self.workspace / "src" / "auth" / "session.py"
        auth_file.parent.mkdir(parents=True, exist_ok=True)
        auth_file.write_text("TOKEN = 'a'\n", encoding="utf-8")
        pay_file = self.workspace / "src" / "payments" / "ledger.py"
        pay_file.parent.mkdir(parents=True, exist_ok=True)
        pay_file.write_text("TOKEN = 'b'\n", encoding="utf-8")

        auth_memory = self.record(
            "phase2a touched path auth",
            kind="context_block",
            touched_files=["src/auth/session.py"],
        )
        other_memory = self.record(
            "phase2a touched path other",
            kind="context_block",
            touched_files=["src/payments/ledger.py"],
        )
        result = self._pack_preview(touched_paths=["src/auth/session.py"])
        ids = set(self._pack_preview_ids(result))
        self.assertIn(auth_memory["id"], ids)
        self.assertNotIn(other_memory["id"], ids)

    def test_pack_preview_counts_and_samples(self) -> None:
        tracked = self.workspace / "src" / "auth" / "session.py"
        tracked.parent.mkdir(parents=True, exist_ok=True)
        tracked.write_text("phase2a tracked\n", encoding="utf-8")
        context = self.record(
            "phase2a counts context",
            kind="context_block",
            touched_files=["src/auth/session.py"],
        )
        hippocampus = self.record("phase2a counts hippocampus", kind="hippocampus_entry")
        server.topic_add({"memory_id": context["id"], "topic": "phase2a-auth", "source": "operator"})
        server.topic_add({"memory_id": hippocampus["id"], "topic": "phase2a-auth", "source": "operator"})
        result = self._pack_preview(topics=["phase2a-auth"], sample_per_kind=2)
        payload = result["structuredContent"]
        self.assertIn("total_rows", payload["selection"])
        self.assertIn("by_kind", payload["counts"])
        self.assertIn("by_namespace", payload["counts"])
        self.assertIn("by_origin", payload["counts"])
        self.assertIn("by_topic", payload["counts"])
        self.assertIn("samples", payload)
        self.assertIn("top_referenced_files", payload["files"])
        self.assertGreaterEqual(int(payload["selection"]["total_rows"]), 2)
        self.assertEqual(int(payload["counts"]["by_topic"].get("phase2a-auth", 0)), 2)
        self.assertTrue(payload["samples"])
        for kind_rows in payload["samples"].values():
            self.assertLessEqual(len(kind_rows), 2)

    def test_pack_preview_is_read_only(self) -> None:
        self.record("phase2a read only baseline", kind="context_block")
        server.load_store()
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            before_count = int(conn.execute("SELECT COUNT(*) FROM exported_packs").fetchone()[0])
        finally:
            conn.close()
        exports_dir = self.sqlite_file.parent / "exports"
        before_files = sorted(str(path.relative_to(exports_dir)) for path in exports_dir.rglob("*")) if exports_dir.exists() else []

        result = self._pack_preview()
        self.assertFalse(result["isError"], result)

        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            after_count = int(conn.execute("SELECT COUNT(*) FROM exported_packs").fetchone()[0])
        finally:
            conn.close()
        after_files = sorted(str(path.relative_to(exports_dir)) for path in exports_dir.rglob("*")) if exports_dir.exists() else []
        self.assertEqual(before_count, after_count)
        self.assertEqual(before_files, after_files)

    def test_pack_preview_interaction_log_warning(self) -> None:
        interaction = self.record("phase2a interaction preview", kind="interaction_log")
        result = self._pack_preview(kinds=["interaction_log"])
        ids = set(self._pack_preview_ids(result))
        self.assertIn(interaction["id"], ids)
        warnings = result["structuredContent"]["warnings"]
        self.assertTrue(any(row.get("code") == "kind_preview_only" for row in warnings))

    def test_pack_preview_limit(self) -> None:
        for idx in range(6):
            self.record(f"phase2a limit context {idx}", kind="context_block")
        result = self._pack_preview(kinds=["context_block"], limit=2)
        selection = result["structuredContent"]["selection"]
        self.assertTrue(selection["limited"])
        self.assertLessEqual(len(selection["row_ids"]), 2)
        self.assertGreater(int(selection["total_rows"]), 2)

    def test_pack_preview_date_filters(self) -> None:
        older = self.record("phase2a date older", kind="context_block")
        newer = self.record("phase2a date newer", kind="context_block")
        self._set_created_at(older["id"], "2025-01-01T00:00:00Z")
        self._set_created_at(newer["id"], "2027-01-01T00:00:00Z")

        after = self._pack_preview(kinds=["context_block"], created_after="2026-01-01T00:00:00Z")
        after_ids = set(self._pack_preview_ids(after))
        self.assertIn(newer["id"], after_ids)
        self.assertNotIn(older["id"], after_ids)

        before = self._pack_preview(kinds=["context_block"], created_before="2026-01-01T00:00:00Z")
        before_ids = set(self._pack_preview_ids(before))
        self.assertIn(older["id"], before_ids)
        self.assertNotIn(newer["id"], before_ids)

    def test_pack_preview_combined_filters(self) -> None:
        self._insert_imported_pack(pack_id="phase2a-combo", trust_level="trusted", namespace="pack:phase2a-combo")
        local_match = self.record(
            "phase2a combo local target",
            kind="context_block",
            namespace="local",
            origin="local",
        )
        local_other_kind = self.record(
            "phase2a combo local other kind",
            kind="hippocampus_entry",
            namespace="local",
            origin="local",
        )
        imported = self.record(
            "phase2a combo imported",
            kind="context_block",
            namespace="pack:phase2a-combo",
            origin="imported",
        )
        server.topic_add({"memory_id": local_match["id"], "topic": "phase2a-combo", "source": "operator"})
        server.topic_add({"memory_id": local_other_kind["id"], "topic": "phase2a-combo", "source": "operator"})
        server.topic_add({"memory_id": imported["id"], "topic": "phase2a-combo", "source": "operator"})

        result = self._pack_preview(topics=["phase2a-combo"], kinds=["context_block"], namespace="local")
        ids = set(self._pack_preview_ids(result))
        self.assertEqual(ids, {local_match["id"]})

    def test_pack_preview_empty_selection(self) -> None:
        result = self._pack_preview(topics=["phase2a-topic-not-present"])
        payload = result["structuredContent"]
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(int(payload["selection"]["total_rows"]), 0)
        self.assertEqual(payload["selection"]["row_ids"], [])
        self.assertEqual(payload["samples"], {})

    def test_pack_preview_sample_per_kind(self) -> None:
        for idx in range(5):
            self.record(f"phase2a sample context {idx}", kind="context_block")
            self.record(f"phase2a sample hippocampus {idx}", kind="hippocampus_entry")
        result = self._pack_preview(sample_per_kind=2)
        samples = result["structuredContent"]["samples"]
        self.assertIn("context_block", samples)
        self.assertIn("hippocampus_entry", samples)
        self.assertLessEqual(len(samples["context_block"]), 2)
        self.assertLessEqual(len(samples["hippocampus_entry"]), 2)

    def test_pack_preview_include_samples_false(self) -> None:
        self.record("phase2a no samples row", kind="context_block")
        result = self._pack_preview(include_samples=False)
        self.assertEqual(result["structuredContent"]["samples"], {})

    def test_pack_preview_backward_compat_retrieval_unchanged(self) -> None:
        self.record("phase2a retrieval baseline alpha beta", kind="note")
        self.record("phase2a retrieval baseline alpha", kind="note")
        before = server.search_memories({"query": "alpha beta", "limit": 5})
        self.assertFalse(before["isError"], before)
        before_ids = [row["id"] for row in before["structuredContent"]["matches"]]
        preview = self._pack_preview()
        self.assertFalse(preview["isError"], preview)
        after = server.search_memories({"query": "alpha beta", "limit": 5})
        self.assertFalse(after["isError"], after)
        after_ids = [row["id"] for row in after["structuredContent"]["matches"]]
        self.assertEqual(before_ids, after_ids)

    def test_memory_group_discover_topic_groups(self) -> None:
        first = self.record("group topic alpha body", kind="context_block", title="Alpha memory")
        second = self.record("group topic beta body", kind="hippocampus_entry", title="Beta memory")
        added_a = server.topic_add({"memory_id": str(first["id"]), "topic": "mnemo-memory-packs", "source": "operator"})
        added_b = server.topic_add({"memory_id": str(second["id"]), "topic": "mnemo-memory-packs", "source": "operator"})
        self.assertFalse(added_a["isError"], added_a)
        self.assertFalse(added_b["isError"], added_b)

        result = self._memory_group_discover(limit_groups=10)
        groups = result["structuredContent"]["groups"]
        group = next(item for item in groups if item["group_id"] == "topic:mnemo-memory-packs")
        self.assertEqual(group["group_type"], "topic")
        self.assertGreaterEqual(group["core_memory_count"], 2)
        self.assertIn(str(first["id"]), group["sample_memory_ids"])

    def test_memory_group_discover_excludes_mechanical_topics_by_default(self) -> None:
        row = self.record("mechanical topic row", kind="context_block")
        added = server.topic_add({"memory_id": str(row["id"]), "topic": "synthetic:run:demo", "source": "operator"})
        self.assertFalse(added["isError"], added)

        result = self._memory_group_discover(limit_groups=20)
        group_ids = {str(item["group_id"]) for item in result["structuredContent"]["groups"]}
        self.assertNotIn("topic:synthetic:run:demo", group_ids)

    def test_memory_group_discover_catalog_mode_returns_compact_options(self) -> None:
        alpha = self.record("banking risk domain alpha", kind="context_block", title="Banking Risk Alpha", domain="banking_risk")
        beta = self.record("banking risk domain beta", kind="hippocampus_entry", title="Banking Risk Beta", domain="banking_risk")
        result = self._memory_group_discover(
            output_mode="catalog",
            catalog_for="export",
            limit_groups=10,
            include_raw_groups=False,
        )
        payload = result["structuredContent"]
        self.assertEqual(payload["output_mode"], "catalog")
        self.assertIn("catalog", payload)
        options = payload["catalog"]["options"]
        self.assertTrue(options)
        option = next(item for item in options if item["group_id"] == "domain:banking_risk")
        for key in ("label", "value", "description", "group_id", "group_type"):
            self.assertIn(key, option)
        self.assertEqual(option["value"], option["group_id"])
        self.assertEqual(option["group_type"], "domain")
        self.assertGreaterEqual(int(option["core_memory_count"]), 2)
        self.assertGreaterEqual(int(option["core_exportable_count"]), 2)

    def test_memory_group_discover_catalog_mode_omits_verbose_fields(self) -> None:
        one = self.record("catalog compact alpha", kind="context_block", title="Compact Alpha")
        two = self.record("catalog compact beta", kind="hippocampus_entry", title="Compact Beta")
        for memory_id in (str(one["id"]), str(two["id"])):
            added = server.topic_add({"memory_id": memory_id, "topic": "catalog-compact-topic", "source": "operator"})
            self.assertFalse(added["isError"], added)
        result = self._memory_group_discover(
            output_mode="catalog",
            catalog_for="export",
            limit_groups=10,
            include_raw_groups=False,
        )
        payload = result["structuredContent"]
        self.assertNotIn("groups", payload)
        option = next(item for item in payload["catalog"]["options"] if item["group_id"] == "topic:catalog-compact-topic")
        for forbidden in ("core_topics", "related_topics", "domains", "touched_paths", "sample_memory_ids", "sample_titles", "reasons"):
            self.assertNotIn(forbidden, option)

    def test_memory_group_discover_catalog_mode_core_counts(self) -> None:
        self.record("banking counts one", kind="context_block", domain="banking_risk")
        self.record("banking counts two", kind="hippocampus_entry", domain="banking_risk")
        self.record("banking counts nonexportable", kind="interaction_log", summary="banking log", domain="banking_risk")
        result = self._memory_group_discover(
            output_mode="catalog",
            catalog_for="export",
            limit_groups=10,
            include_raw_groups=False,
        )
        option = next(item for item in result["structuredContent"]["catalog"]["options"] if item["group_id"] == "domain:banking_risk")
        self.assertEqual(int(option["core_memory_count"]), 3)
        self.assertEqual(int(option["core_exportable_count"]), 2)

    def test_memory_group_discover_catalog_mode_sorts_domain_before_synthetic_path(self) -> None:
        self.record("domain group row one", kind="context_block", domain="banking_risk")
        self.record("domain group row two", kind="hippocampus_entry", domain="banking_risk")
        synthetic_path = "state/mnemo/synthetic_files/uxlab/demo/component.md"
        synthetic_file = self.workspace / synthetic_path
        synthetic_file.parent.mkdir(parents=True, exist_ok=True)
        synthetic_file.write_text("synthetic demo component", encoding="utf-8")
        self.record(
            "synthetic path alpha",
            kind="context_block",
            title="Synthetic Path Alpha",
            touched_files=[synthetic_path],
        )
        self.record(
            "synthetic path beta",
            kind="hippocampus_entry",
            title="Synthetic Path Beta",
            touched_files=[synthetic_path],
        )
        result = self._memory_group_discover(
            output_mode="catalog",
            catalog_for="export",
            limit_groups=10,
            include_raw_groups=False,
        )
        options = result["structuredContent"]["catalog"]["options"]
        self.assertTrue(options)
        self.assertEqual(str(options[0]["group_id"]), "domain:banking_risk")
        synthetic_option = next(
            item for item in options if bool(item["synthetic"]) and "state/mnemo/synthetic_files/" in str(item["value"])
        )
        self.assertEqual(str(synthetic_option["group_type"]), "path")

    def test_memory_group_discover_catalog_mode_no_groups(self) -> None:
        result = self._memory_group_discover(
            output_mode="catalog",
            catalog_for="export",
            limit_groups=10,
            include_raw_groups=False,
        )
        payload = result["structuredContent"]
        self.assertEqual(payload["output_mode"], "catalog")
        self.assertEqual(payload["catalog"]["options"], [])
        self.assertNotIn("groups", payload)

    def test_memory_group_preview_core_scope(self) -> None:
        first = self.record("core preview first", kind="context_block", title="Core first")
        second = self.record("core preview second", kind="hippocampus_entry", title="Core second")
        for memory_id in (str(first["id"]), str(second["id"])):
            added = server.topic_add({"memory_id": memory_id, "topic": "phase213-group-core", "source": "operator"})
            self.assertFalse(added["isError"], added)

        result = self._memory_group_preview(group_id="topic:phase213-group-core", scope="core", limit=50)
        selection = result["structuredContent"]["selection"]
        self.assertEqual(set(selection["memory_ids"]), {str(first["id"]), str(second["id"])})
        readiness = result["structuredContent"]["pack_readiness"]
        self.assertTrue(readiness["can_export_default"])

    def test_memory_group_preview_core_plus_related_scope(self) -> None:
        core = self.record("related preview core", kind="context_block", title="Related core")
        peer = self.record("related preview peer", kind="hippocampus_entry", title="Related peer")
        related = self.record("related preview extra", kind="context_block", title="Related extra")
        for memory_id in (str(core["id"]), str(peer["id"])):
            added = server.topic_add({"memory_id": memory_id, "topic": "phase213-group-related", "source": "operator"})
            self.assertFalse(added["isError"], added)
        linked = server.memory_link({"source_id": str(core["id"]), "target_id": str(related["id"]), "relation": "related"})
        self.assertFalse(linked["isError"], linked)

        result = self._memory_group_preview(group_id="topic:phase213-group-related", scope="core_plus_related", limit=50)
        ids = set(result["structuredContent"]["selection"]["memory_ids"])
        self.assertIn(str(core["id"]), ids)
        self.assertIn(str(peer["id"]), ids)
        self.assertIn(str(related["id"]), ids)
        reasons = result["structuredContent"]["membership_reasons"][str(related["id"])]
        self.assertTrue(any("explicit link" in str(item) for item in reasons))

    def test_memory_group_preview_unknown_group(self) -> None:
        result = server.memory_group_preview({"group_id": "topic:missing-group"})
        self.assertTrue(result["isError"], result)
        self.assertEqual(result["structuredContent"]["error"]["code"], "memory_group_not_found")

    def test_memory_group_discover_alias_groups(self) -> None:
        alpha = self.record("hippocampus bridge operator notes", kind="context_block", domain="agentic")
        beta = self.record("bridge for recall workflows", kind="hippocampus_entry", domain="agentic")
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
        concept_id = str(approved["structuredContent"]["concept"]["concept_id"])

        result = self._memory_group_discover(limit_groups=20)
        groups = result["structuredContent"]["groups"]
        group = next(item for item in groups if item["group_id"] == f"alias:{concept_id}")
        self.assertEqual(group["group_type"], "alias")
        self.assertEqual(str(group["label"]), "memory recall pipeline")
        self.assertGreaterEqual(int(group["core_memory_count"]), 2)
        self.assertIn(str(alpha["id"]), set(group["sample_memory_ids"]))
        self.assertIn("alias concept:memory recall pipeline", list(group["reasons"]))

    def test_memory_group_preview_alias_core_scope(self) -> None:
        alpha = self.record("hippocampus bridge sync", kind="context_block", domain="agentic")
        beta = self.record("memory recall pipeline design", kind="hippocampus_entry", domain="agentic")
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
        concept_id = str(approved["structuredContent"]["concept"]["concept_id"])

        result = self._memory_group_preview(group_id=f"alias:{concept_id}", scope="core", limit=50)
        payload = result["structuredContent"]
        self.assertEqual(set(payload["selection"]["memory_ids"]), {str(alpha["id"]), str(beta["id"])})
        reasons = payload["membership_reasons"][str(alpha["id"])]
        self.assertTrue(any("matched alias concept memory recall pipeline" in str(item) for item in reasons))

    def test_memory_group_preview_alias_core_plus_related_scope(self) -> None:
        core = self.record("hippocampus bridge sync", kind="context_block", domain="agentic")
        peer = self.record("memory recall pipeline design", kind="hippocampus_entry", domain="agentic")
        related = self.record("supporting related memory", kind="context_block", domain="ops")
        topic_added = server.topic_add({"memory_id": str(core["id"]), "topic": "alias-related-support", "source": "operator"})
        self.assertFalse(topic_added["isError"], topic_added)
        topic_added = server.topic_add({"memory_id": str(related["id"]), "topic": "alias-related-support", "source": "operator"})
        self.assertFalse(topic_added["isError"], topic_added)
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
        concept_id = str(approved["structuredContent"]["concept"]["concept_id"])

        result = self._memory_group_preview(group_id=f"alias:{concept_id}", scope="core_plus_related", limit=50)
        payload = result["structuredContent"]
        ids = set(payload["selection"]["memory_ids"])
        self.assertIn(str(core["id"]), ids)
        self.assertIn(str(peer["id"]), ids)
        self.assertIn(str(related["id"]), ids)
        self.assertTrue(any("shared topic:alias-related-support" in str(item) for item in payload["membership_reasons"][str(related["id"])]))

    def test_memory_group_discover_ignores_inactive_aliases(self) -> None:
        self.record("hippocampus bridge sync", kind="context_block", domain="agentic")
        self.record("bridge for recall workflows", kind="hippocampus_entry", domain="agentic")
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
        alias_id = str(approved["structuredContent"]["alias"]["alias_id"])
        concept_id = str(approved["structuredContent"]["concept"]["concept_id"])
        disabled = server.memory_maintenance({"action": "disable_alias", "alias_id": alias_id, "reason": "inactive"})
        self.assertFalse(disabled["isError"], disabled)

        result = self._memory_group_discover(limit_groups=20)
        group_ids = {str(item["group_id"]) for item in result["structuredContent"]["groups"]}
        self.assertNotIn(f"alias:{concept_id}", group_ids)

    def test_memory_group_discover_ignores_pending_alias_proposals(self) -> None:
        self.record("hippocampus bridge sync", kind="context_block", domain="agentic")
        self.record("bridge for recall workflows", kind="hippocampus_entry", domain="agentic")
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            conn.execute(
                """
                INSERT INTO alias_proposals(
                    proposal_id, canonical, candidate_alias, domain, language, status,
                    normalized_alias, score, evidence_json, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "alias-group-pending-only",
                    "memory recall pipeline",
                    "hippocampus bridge",
                    "agentic",
                    "en",
                    "pending",
                    server._normalize_alias_term("hippocampus bridge"),
                    0.75,
                    json.dumps([], ensure_ascii=False),
                    server.now_iso(),
                    server.now_iso(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        result = self._memory_group_discover(limit_groups=20)
        self.assertFalse(
            any(str(item["group_id"]).startswith("alias:") for item in result["structuredContent"]["groups"])
        )

    def test_memory_group_discover_alias_respects_visibility(self) -> None:
        trusted_pack_id = "alias-trusted-pack"
        quarantine_pack_id = "alias-quarantine-pack"
        trusted_namespace = f"pack:trusted:{trusted_pack_id}"
        quarantine_namespace = f"pack:quarantine:{quarantine_pack_id}"
        self._insert_imported_pack(pack_id=trusted_pack_id, trust_level="trusted", namespace=trusted_namespace)
        self._insert_imported_pack(pack_id=quarantine_pack_id, trust_level="quarantine", namespace=quarantine_namespace)
        trusted = self.record(
            "hippocampus bridge trusted visibility",
            kind="context_block",
            domain="agentic",
            namespace=trusted_namespace,
            origin="imported",
        )
        quarantine = self.record(
            "hippocampus bridge quarantine visibility",
            kind="context_block",
            domain="agentic",
            namespace=quarantine_namespace,
            origin="imported",
        )
        local = self.record("hippocampus bridge local visibility", kind="context_block", domain="agentic")
        local_peer = self.record("memory recall pipeline local peer", kind="hippocampus_entry", domain="agentic")
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
        concept_id = str(approved["structuredContent"]["concept"]["concept_id"])

        default_group = next(
            item
            for item in self._memory_group_discover(limit_groups=20)["structuredContent"]["groups"]
            if item["group_id"] == f"alias:{concept_id}"
        )
        self.assertEqual(int(default_group["core_memory_count"]), 2)

        trusted_group = next(
            item
            for item in self._memory_group_discover(include_imported=True, limit_groups=20)["structuredContent"]["groups"]
            if item["group_id"] == f"alias:{concept_id}"
        )
        self.assertEqual(int(trusted_group["core_memory_count"]), 3)

        quarantine_group = next(
            item
            for item in self._memory_group_discover(include_quarantine=True, limit_groups=20)["structuredContent"]["groups"]
            if item["group_id"] == f"alias:{concept_id}"
        )
        self.assertEqual(int(quarantine_group["core_memory_count"]), 3)

        all_group = next(
            item
            for item in self._memory_group_discover(include_imported=True, include_quarantine=True, limit_groups=20)["structuredContent"]["groups"]
            if item["group_id"] == f"alias:{concept_id}"
        )
        self.assertEqual(int(all_group["core_memory_count"]), 4)

        trusted_preview = self._memory_group_preview(
            group_id=f"alias:{concept_id}",
            scope="core",
            include_imported=True,
            limit=50,
        )
        self.assertIn(str(trusted["id"]), set(trusted_preview["structuredContent"]["selection"]["memory_ids"]))
        self.assertNotIn(str(quarantine["id"]), set(trusted_preview["structuredContent"]["selection"]["memory_ids"]))

    def test_memory_group_preview_alias_unknown_group(self) -> None:
        result = server.memory_group_preview({"group_id": "alias:missing-concept"})
        self.assertTrue(result["isError"], result)
        self.assertEqual(result["structuredContent"]["error"]["code"], "memory_group_not_found")

    def test_no_schema_bump_for_alias_groups(self) -> None:
        self.record("hippocampus bridge sync", kind="context_block", domain="agentic")
        self.record("bridge for recall workflows", kind="hippocampus_entry", domain="agentic")
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
        discover = self._memory_group_discover(limit_groups=20)
        self.assertFalse(discover["isError"], discover)
        server.load_store()
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            schema_version = int(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(schema_version, 7)
        self.assertEqual(int(server.SQLITE_SCHEMA_VERSION), 7)

    def test_memory_group_actions_read_only(self) -> None:
        first = self.record("group read only row a", kind="context_block", title="Read only row a")
        second = self.record("group read only row b", kind="hippocampus_entry", title="Read only row b")
        for memory_id in (str(first["id"]), str(second["id"])):
            added = server.topic_add({"memory_id": memory_id, "topic": "phase213-read-only", "source": "operator"})
            self.assertFalse(added["isError"], added)
        before = self._read_only_snapshot()
        discover = self._memory_group_discover(limit_groups=10)
        self.assertFalse(discover["isError"], discover)
        preview = self._memory_group_preview(group_id="topic:phase213-read-only", scope="core", limit=50)
        self.assertFalse(preview["isError"], preview)
        after = self._read_only_snapshot()
        self.assertEqual(before, after)

    def test_no_schema_bump(self) -> None:
        server.load_store()
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            schema_version = int(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(schema_version, 7)
        self.assertEqual(int(server.SQLITE_SCHEMA_VERSION), 7)

    def test_pack_preview_strict_read_only_empty_db(self) -> None:
        result = server.pack_preview({})
        self.assertFalse(result["isError"], result)
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            memories_count = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            exported_count = int(conn.execute("SELECT COUNT(*) FROM exported_packs").fetchone()[0])
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        finally:
            conn.close()
        self.assertIn("memories", tables)
        self.assertIn("memory_topics", tables)
        self.assertIn("imported_packs", tables)
        self.assertIn("exported_packs", tables)
        self.assertEqual(memories_count, 0)
        self.assertEqual(exported_count, 0)
        self.assertEqual(result["structuredContent"]["selection"]["row_ids"], [])

    def test_pack_redaction_preview_email(self) -> None:
        literal = "test.user@example.test"
        self.record(f"phase2b email literal {literal}", kind="context_block")
        result = self._pack_redaction_preview()
        redaction = result["structuredContent"]["redaction"]
        self.assertGreaterEqual(int(redaction["by_category"].get("email", 0)), 1)
        samples = result["structuredContent"]["samples"]
        self.assertTrue(samples)
        self.assertIn("[REDACTED:email]", samples[0]["redacted_preview"])
        self.assertNotIn(literal, json.dumps(result["structuredContent"], ensure_ascii=True))

    def test_pack_redaction_preview_memory_ids_exact_selector(self) -> None:
        first_literal = "exact.one@example.test"
        second_literal = "exact.two@example.test"
        first = self.record(f"phase213 redaction exact {first_literal}", kind="context_block")
        self.record(f"phase213 redaction ignored {second_literal}", kind="context_block")
        result = self._pack_redaction_preview(memory_ids=[str(first["id"])], limit=50)
        redaction = result["structuredContent"]["redaction"]
        self.assertEqual(int(redaction["affected_rows"]), 1)
        self.assertGreaterEqual(int(redaction["by_category"].get("email", 0)), 1)
        payload_text = json.dumps(result["structuredContent"], ensure_ascii=True)
        self.assertIn("exact.one@example.test".replace("exact.one@example.test", "[REDACTED:email]"), payload_text)
        self.assertNotIn(second_literal, payload_text)

    def test_pack_redaction_preview_group_selector_matches_memory_group_preview(self) -> None:
        first = self.record("group redaction first alpha@example.test", kind="context_block")
        second = self.record("group redaction second beta@example.test", kind="hippocampus_entry")
        ignored = self.record("group redaction ignored gamma@example.test", kind="context_block")
        for memory_id in (str(first["id"]), str(second["id"])):
            added = server.topic_add({"memory_id": memory_id, "topic": "phase216-group-redaction", "source": "operator"})
            self.assertFalse(added["isError"], added)

        group = self._memory_group_preview(group_id="topic:phase216-group-redaction", scope="core", limit=50)
        expected_ids = group["structuredContent"]["selection"]["memory_ids"]
        result = self._pack_redaction_preview(group_id="topic:phase216-group-redaction", scope="core", limit=50)
        structured = result["structuredContent"]
        self.assertEqual(structured["filters"]["group_id"], "topic:phase216-group-redaction")
        self.assertEqual(structured["filters"]["scope"], "core")
        self.assertEqual(structured["selection"]["row_ids"], expected_ids)
        self.assertNotIn(str(ignored["id"]), structured["selection"]["row_ids"])

    def test_pack_redaction_preview_group_selector_rejects_mixed_selectors(self) -> None:
        result = server.pack_redaction_preview({"group_id": "topic:phase216-redact-mixed", "memory_ids": ["mem_1"]})
        self.assertTrue(result["isError"], result)
        self.assertEqual(result["structuredContent"]["error"]["code"], "ambiguous_selector")

    def test_pack_redaction_preview_secret_patterns(self) -> None:
        jwt_literal = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1MSJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        aws_literal = "AKIA1234567890ABCDEF"
        private_header = "-----BEGIN RSA PRIVATE KEY-----"
        self.record(f"phase2b jwt literal {jwt_literal}", kind="context_block")
        self.record(f"phase2b aws literal {aws_literal}", kind="context_block")
        self.record(f"phase2b key header {private_header}", kind="context_block")
        result = self._pack_redaction_preview(kinds=["context_block"])
        redaction = result["structuredContent"]["redaction"]
        self.assertGreaterEqual(int(redaction["by_category"].get("jwt", 0)), 1)
        self.assertGreaterEqual(int(redaction["by_category"].get("aws_access_key", 0)), 1)
        self.assertGreaterEqual(int(redaction["by_category"].get("private_key_header", 0)), 1)
        payload_text = json.dumps(result["structuredContent"], ensure_ascii=True)
        self.assertNotIn(jwt_literal, payload_text)
        self.assertNotIn(aws_literal, payload_text)
        self.assertNotIn(private_header, payload_text)

    def test_pack_redaction_preview_ip_and_user_path(self) -> None:
        self.record(
            "phase2b host 10.23.45.67 and path C:\\Users\\fakeuser\\secret.txt",
            kind="context_block",
        )
        result = self._pack_redaction_preview(kinds=["context_block"])
        redaction = result["structuredContent"]["redaction"]
        self.assertGreaterEqual(int(redaction["by_category"].get("ipv4", 0)), 1)
        self.assertGreaterEqual(int(redaction["by_category"].get("user_path", 0)), 1)

    def test_pack_redaction_preview_uses_pack_preview_selection(self) -> None:
        self._insert_imported_pack(pack_id="phase2b-trusted", trust_level="trusted", namespace="pack:phase2b-trusted")
        self._insert_imported_pack(
            pack_id="phase2b-quarantine",
            trust_level="quarantine",
            namespace="pack:quarantine:phase2b-quarantine",
        )
        local = self.record("phase2b local marker test.user@example.test", kind="context_block", namespace="local", origin="local")
        trusted = self.record(
            "phase2b trusted marker test.user@example.test",
            kind="context_block",
            namespace="pack:phase2b-trusted",
            origin="imported",
        )
        quarantined = self.record(
            "phase2b quarantined marker test.user@example.test",
            kind="context_block",
            namespace="pack:quarantine:phase2b-quarantine",
            origin="imported",
        )
        default_preview = self._pack_preview(kinds=["context_block"])
        default_redaction = self._pack_redaction_preview(kinds=["context_block"])
        self.assertEqual(default_preview["structuredContent"]["selection"]["row_ids"], default_redaction["structuredContent"]["selection"]["row_ids"])
        self.assertIn(local["id"], default_redaction["structuredContent"]["selection"]["row_ids"])
        self.assertNotIn(trusted["id"], default_redaction["structuredContent"]["selection"]["row_ids"])
        self.assertNotIn(quarantined["id"], default_redaction["structuredContent"]["selection"]["row_ids"])

        trusted_preview = self._pack_preview(kinds=["context_block"], include_imported=True)
        trusted_redaction = self._pack_redaction_preview(kinds=["context_block"], include_imported=True)
        self.assertEqual(trusted_preview["structuredContent"]["selection"]["row_ids"], trusted_redaction["structuredContent"]["selection"]["row_ids"])
        self.assertIn(trusted["id"], trusted_redaction["structuredContent"]["selection"]["row_ids"])
        self.assertNotIn(quarantined["id"], trusted_redaction["structuredContent"]["selection"]["row_ids"])

        quarantine_preview = self._pack_preview(kinds=["context_block"], include_quarantine=True)
        quarantine_redaction = self._pack_redaction_preview(kinds=["context_block"], include_quarantine=True)
        self.assertEqual(
            quarantine_preview["structuredContent"]["selection"]["row_ids"],
            quarantine_redaction["structuredContent"]["selection"]["row_ids"],
        )
        self.assertIn(quarantined["id"], quarantine_redaction["structuredContent"]["selection"]["row_ids"])

    def test_pack_redaction_preview_topic_filter(self) -> None:
        body_only = self.record("phase2b auth text only test.user@example.test", kind="context_block")
        tagged = self.record("phase2b tagged row with redaction test.user@example.test", kind="context_block")
        topic_add = server.topic_add({"memory_id": tagged["id"], "topic": "phase2b-auth", "source": "operator"})
        self.assertFalse(topic_add["isError"], topic_add)
        result = self._pack_redaction_preview(topics=["phase2b-auth"], kinds=["context_block"])
        ids = result["structuredContent"]["selection"]["row_ids"]
        self.assertIn(tagged["id"], ids)
        self.assertNotIn(body_only["id"], ids)

    def test_pack_redaction_preview_touched_paths_filter(self) -> None:
        auth_file = self.workspace / "src" / "auth" / "session.py"
        auth_file.parent.mkdir(parents=True, exist_ok=True)
        auth_file.write_text("phase2b\n", encoding="utf-8")
        pay_file = self.workspace / "src" / "billing" / "ledger.py"
        pay_file.parent.mkdir(parents=True, exist_ok=True)
        pay_file.write_text("phase2b\n", encoding="utf-8")
        auth = self.record(
            "phase2b touched auth test.user@example.test",
            kind="context_block",
            touched_files=["src/auth/session.py"],
        )
        other = self.record(
            "phase2b touched other test.user@example.test",
            kind="context_block",
            touched_files=["src/billing/ledger.py"],
        )
        result = self._pack_redaction_preview(touched_paths=["src/auth/session.py"], kinds=["context_block"])
        ids = result["structuredContent"]["selection"]["row_ids"]
        self.assertIn(auth["id"], ids)
        self.assertNotIn(other["id"], ids)

    def test_pack_redaction_preview_empty_selection(self) -> None:
        result = self._pack_redaction_preview(topics=["phase2b-missing-topic"])
        payload = result["structuredContent"]
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(int(payload["selection"]["total_rows"]), 0)
        self.assertEqual(payload["selection"]["row_ids"], [])
        self.assertEqual(int(payload["redaction"]["total_matches"]), 0)
        self.assertEqual(payload["samples"], [])

    def test_pack_redaction_preview_samples_bounded(self) -> None:
        row_a = self.record(
            "phase2b sample one test.user@example.test",
            kind="context_block",
        )
        row_b = self.record(
            "phase2b sample two test.user@example.test 10.20.30.40",
            kind="context_block",
        )
        row_c = self.record(
            "phase2b sample three test.user@example.test 10.20.30.41 AKIA1234567890ABCDEF",
            kind="context_block",
        )
        self._set_created_at(row_a["id"], "2025-01-01T00:00:00Z")
        self._set_created_at(row_b["id"], "2026-01-01T00:00:00Z")
        self._set_created_at(row_c["id"], "2027-01-01T00:00:00Z")
        result = self._pack_redaction_preview(kinds=["context_block"], max_redacted_samples=2)
        samples = result["structuredContent"]["samples"]
        self.assertLessEqual(len(samples), 2)
        self.assertEqual([int(item["match_count"]) for item in samples], sorted([int(item["match_count"]) for item in samples], reverse=True))

    def test_pack_redaction_preview_include_samples_false(self) -> None:
        self.record("phase2b no samples test.user@example.test", kind="context_block")
        result = self._pack_redaction_preview(include_redacted_samples=False)
        self.assertEqual(result["structuredContent"]["samples"], [])

    def test_pack_redaction_preview_read_only(self) -> None:
        memory = self.record("phase2b read only test.user@example.test", kind="context_block")
        before_exported = self._exported_packs_count()
        before_memories = self._memory_count()
        before_text = self._memory_text(memory["id"])
        exports_dir = self.sqlite_file.parent / "exports"
        before_files = sorted(str(path.relative_to(exports_dir)) for path in exports_dir.rglob("*")) if exports_dir.exists() else []
        result = self._pack_redaction_preview()
        self.assertFalse(result["isError"], result)
        after_exported = self._exported_packs_count()
        after_memories = self._memory_count()
        after_text = self._memory_text(memory["id"])
        after_files = sorted(str(path.relative_to(exports_dir)) for path in exports_dir.rglob("*")) if exports_dir.exists() else []
        self.assertEqual(before_exported, after_exported)
        self.assertEqual(before_memories, after_memories)
        self.assertEqual(before_text, after_text)
        self.assertEqual(before_files, after_files)

    def test_pack_redaction_preview_interaction_log_warning(self) -> None:
        memory = self.record("phase2b interaction test.user@example.test", kind="interaction_log")
        result = self._pack_redaction_preview(kinds=["interaction_log"])
        warnings = result["structuredContent"]["warnings"]
        self.assertTrue(any(item.get("code") == "kind_preview_only" for item in warnings))
        ids = result["structuredContent"]["selection"]["row_ids"]
        self.assertIn(memory["id"], ids)
        self.assertGreaterEqual(int(result["structuredContent"]["redaction"]["by_category"].get("email", 0)), 1)

    def test_pack_redaction_preview_no_original_secret_leak(self) -> None:
        jwt_literal = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1MSJ9.SflKxwRJSMeKKF2QT4fwpMeJJVVVVVVVVVVVV"
        aws_literal = "AKIA1234567890ABCDEF"
        email_literal = "phase2b.user@example.test"
        ip_literal = "172.16.23.45"
        path_literal = "C:\\Users\\phase2b\\secret.txt"
        key_header = "-----BEGIN RSA PRIVATE KEY-----"
        self.record(
            f"phase2b leak check {email_literal} {aws_literal} {jwt_literal} {ip_literal} {path_literal} {key_header}",
            kind="context_block",
        )
        result = self._pack_redaction_preview(kinds=["context_block"])
        structured_text = json.dumps(result.get("structuredContent", {}), ensure_ascii=True)
        content_text = json.dumps(result.get("content", []), ensure_ascii=True)
        for literal in (jwt_literal, aws_literal, email_literal, ip_literal, path_literal, key_header):
            self.assertNotIn(literal, structured_text)
            self.assertNotIn(literal, content_text)

    def test_pack_redaction_preview_limit(self) -> None:
        for idx in range(6):
            self.record(f"phase2b limit {idx} test.user@example.test", kind="context_block")
        result = self._pack_redaction_preview(kinds=["context_block"], limit=2)
        payload = result["structuredContent"]
        self.assertTrue(payload["selection"]["limited"])
        self.assertLessEqual(len(payload["selection"]["row_ids"]), 2)
        self.assertLessEqual(int(payload["redaction"]["affected_rows"]), 2)
        warnings = payload["warnings"]
        self.assertTrue(any(item.get("code") == "redaction_counts_limited" for item in warnings))

    def test_pack_preview_no_bootstrap_regression(self) -> None:
        result = server.mnemo_gateway({"action": "pack_preview", "params": {}})
        self.assertFalse(result["isError"], result)
        self.assertEqual(self._memory_count(), 0)

    def test_pack_redaction_preview_no_ipv6_category_in_phase2b(self) -> None:
        self.record("phase2b ipv6 only 2001:0db8:85a3:0000:0000:8a2e:0370:7334", kind="context_block")
        result = self._pack_redaction_preview(kinds=["context_block"])
        redaction = result["structuredContent"]["redaction"]
        self.assertNotIn("ipv6", redaction["rules_applied"])
        self.assertNotIn("ipv6", redaction["by_category"])
        warnings = result["structuredContent"]["warnings"]
        self.assertTrue(any(item.get("code") == "redaction_ruleset_baseline_only" for item in warnings))

    def test_pack_redaction_preview_caps_max_samples(self) -> None:
        for idx in range(55):
            self.record(f"phase2b cap {idx} test.user@example.test", kind="context_block")
        result = self._pack_redaction_preview(kinds=["context_block"], max_redacted_samples=100)
        self.assertLessEqual(len(result["structuredContent"]["samples"]), 50)
        warnings = result["structuredContent"]["warnings"]
        self.assertTrue(any(item.get("code") == "max_redacted_samples_capped" for item in warnings))

    def test_pack_redaction_preview_action_log_no_sensitive_leak(self) -> None:
        literal = "phase2b.log@example.test"
        self.record(f"phase2b log safety {literal}", kind="context_block")
        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            before_max_rowid = int(conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM events").fetchone()[0])
        finally:
            conn.close()

        result = self._pack_redaction_preview(kinds=["context_block"])
        self.assertFalse(result["isError"], result)

        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            available = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(events)").fetchall()
            }
            wanted = [name for name in ("event_type", "action", "query_text", "summary", "data_json") if name in available]
            if not wanted:
                self.assertTrue(True)
                return
            select_sql = "SELECT " + ", ".join(wanted) + " FROM events WHERE rowid > ?"
            new_rows = conn.execute(select_sql, (before_max_rowid,)).fetchall()
        finally:
            conn.close()
        if not new_rows:
            self.assertTrue(True)
            return
        rendered = json.dumps([dict(row) for row in new_rows], ensure_ascii=True)
        self.assertNotIn(literal, rendered)
        self.assertNotIn("[REDACTED:email]", rendered)

    def test_pack_redaction_preview_consume_on_match(self) -> None:
        # user_path runs before ipv4; ipv4 inside this path should not be double-counted.
        self.record("phase2b consume /home/10.23.45.67/secret.txt", kind="context_block")
        result = self._pack_redaction_preview(kinds=["context_block"])
        by_category = result["structuredContent"]["redaction"]["by_category"]
        self.assertEqual(int(by_category.get("user_path", 0)), 1)
        self.assertEqual(int(by_category.get("ipv4", 0)), 0)

    def test_pack_redaction_preview_scans_all_relevant_text_fields(self) -> None:
        self.record(
            "phase2b safe body text only",
            kind="context_block",
            title="phase2b.title@example.test",
        )
        result = self._pack_redaction_preview(kinds=["context_block"])
        redaction = result["structuredContent"]["redaction"]
        self.assertGreaterEqual(int(redaction["by_category"].get("email", 0)), 1)

    def test_pack_export_requires_allow_unsigned(self) -> None:
        self.record("phase2c requires unsigned test.user@example.test", kind="context_block")
        output_dir = self.root / "phase2c_unsigned_required"
        before_audit = self._exported_packs_count()
        result = self._pack_export_error(
            pack_name="phase2c_requires_unsigned",
            output_dir=str(output_dir),
        )
        self.assertIn("allow_unsigned=true", result["content"][0]["text"])
        self.assertEqual(self._exported_packs_count(), before_audit)
        if output_dir.exists():
            self.assertEqual(list(output_dir.glob("*.mem")), [])

    def test_pack_export_uses_mem_extension(self) -> None:
        self.record("phase2c mem extension", kind="context_block")
        output_dir = self.root / "phase2c_mem_extension"
        result = self._pack_export(
            pack_name="phase2c_mem_extension",
            output_dir=str(output_dir),
            allow_unsigned=True,
        )
        output_path = Path(result["structuredContent"]["output_path"])
        self.assertEqual(output_path.suffix.lower(), ".mem")
        self.assertTrue(output_path.exists())

    def test_pack_export_mem_opens_as_zip(self) -> None:
        self.record("phase2c mem opens as zip", kind="context_block")
        output_dir = self.root / "phase2c_mem_zip"
        result = self._pack_export(
            pack_name="phase2c_mem_zip",
            output_dir=str(output_dir),
            allow_unsigned=True,
        )
        output_path = Path(result["structuredContent"]["output_path"])
        with zipfile.ZipFile(output_path, "r") as archive:
            self.assertIn("manifest.json", archive.namelist())

    def test_pack_export_creates_zip_with_required_members(self) -> None:
        self.record("phase2c zip member email test.user@example.test", kind="context_block")
        self.record("phase2c zip member aws AKIA1234567890ABCDEF", kind="hippocampus_entry")
        output_dir = self.root / "phase2c_members"
        result = self._pack_export(
            pack_name="phase2c_members",
            output_dir=str(output_dir),
            allow_unsigned=True,
        )
        output_path = Path(result["structuredContent"]["output_path"])
        self.assertTrue(output_path.exists())
        with zipfile.ZipFile(output_path, "r") as archive:
            names = set(archive.namelist())
        for member in server.PACK_REQUIRED_MEMBERS:
            self.assertIn(member, names)

    def test_pack_export_manifest_and_jsonl_parse(self) -> None:
        self.record("phase2c manifest parse one", kind="context_block")
        self.record("phase2c manifest parse two", kind="hippocampus_entry")
        output_dir = self.root / "phase2c_manifest_parse"
        result = self._pack_export(
            pack_name="phase2c_manifest_parse",
            output_dir=str(output_dir),
            allow_unsigned=True,
        )
        output_path = Path(result["structuredContent"]["output_path"])
        with zipfile.ZipFile(output_path, "r") as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            rows_blob = archive.read("content/memories.jsonl").decode("utf-8")
        rows = [json.loads(line) for line in rows_blob.splitlines() if line.strip()]
        self.assertEqual(len(rows), int(manifest["selection"]["exported_rows"]))

    def test_pack_export_content_hash_verifies(self) -> None:
        self.record("phase2c hash email test.user@example.test", kind="context_block")
        output_dir = self.root / "phase2c_hash"
        result = self._pack_export(
            pack_name="phase2c_hash",
            output_dir=str(output_dir),
            allow_unsigned=True,
        )
        output_path = Path(result["structuredContent"]["output_path"])
        members = self._read_zip_members(output_path)
        manifest = json.loads(members["manifest.json"].decode("utf-8"))
        covered = list(manifest["content_hash"]["covered_members"])
        recomputed = self._recompute_pack_content_hash(members, covered)
        self.assertEqual(recomputed, str(manifest["content_hash"]["value"]))
        self.assertEqual(recomputed, str(result["structuredContent"]["content_hash"]["value"]))

    def test_pack_export_redacts_sensitive_literals(self) -> None:
        fake_email = "test.user@example.test"
        fake_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwaDJjIn0.c2lnbmF0dXJl"
        fake_aws = "AKIA1234567890ABCDEF"
        fake_path = "C:\\Users\\fakeuser\\secret.txt"
        fake_ipv4 = "10.44.55.66"
        fake_key = "-----BEGIN RSA PRIVATE KEY-----"
        self.record(
            f"phase2c redact {fake_email} {fake_jwt} {fake_aws} {fake_path} {fake_ipv4} {fake_key}",
            kind="context_block",
        )
        output_dir = self.root / "phase2c_redaction"
        result = self._pack_export(
            pack_name="phase2c_redaction",
            output_dir=str(output_dir),
            allow_unsigned=True,
        )
        output_path = Path(result["structuredContent"]["output_path"])
        members = self._read_zip_members(output_path)
        bundle_text = "\n".join(blob.decode("utf-8", errors="ignore") for blob in members.values())
        for literal in (fake_email, fake_jwt, fake_aws, fake_path, fake_ipv4, fake_key):
            self.assertNotIn(literal, bundle_text)
        for replacement in (
            "[REDACTED:email]",
            "[REDACTED:jwt]",
            "[REDACTED:aws_access_key]",
            "[REDACTED:user_path]",
            "[REDACTED:ipv4]",
            "[REDACTED:private_key_header]",
        ):
            self.assertIn(replacement, bundle_text)

    def test_pack_export_no_source_db_ids_in_zip(self) -> None:
        first = self.record("phase2c no id leak first", kind="context_block")
        second = self.record("phase2c no id leak second", kind="hippocampus_entry")
        output_dir = self.root / "phase2c_no_source_ids"
        result = self._pack_export(
            pack_name="phase2c_no_source_ids",
            output_dir=str(output_dir),
            allow_unsigned=True,
        )
        output_path = Path(result["structuredContent"]["output_path"])
        members = self._read_zip_members(output_path)
        bundle_text = "\n".join(blob.decode("utf-8", errors="ignore") for blob in members.values())
        self.assertNotIn(first["id"], bundle_text)
        self.assertNotIn(second["id"], bundle_text)

    def test_pack_export_memory_ids_exact_selector_does_not_leak_source_ids_in_zip(self) -> None:
        first = self.record("phase2c exact id no leak first", kind="context_block")
        second = self.record("phase2c exact id no leak second", kind="hippocampus_entry")
        result = self._pack_export(
            pack_name="phase2c_exact_id_no_leak",
            output_dir=str(self.root / "phase2c_exact_id_no_leak"),
            allow_unsigned=True,
            memory_ids=[first["id"], second["id"]],
        )
        output_path = Path(result["structuredContent"]["output_path"])
        members = self._read_zip_members(output_path)
        bundle_text = "\n".join(blob.decode("utf-8", errors="ignore") for blob in members.values())
        self.assertNotIn(first["id"], bundle_text)
        self.assertNotIn(second["id"], bundle_text)

    def test_pack_export_writes_exported_packs_audit_row(self) -> None:
        self.record("phase2c audit row one", kind="context_block")
        before = self._exported_packs_count()
        output_dir = self.root / "phase2c_audit_row"
        result = self._pack_export(
            pack_name="phase2c_audit_row",
            output_dir=str(output_dir),
            allow_unsigned=True,
        )
        after = self._exported_packs_count()
        self.assertEqual(after, before + 1)
        pack_id = str(result["structuredContent"]["pack_id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM exported_packs WHERE pack_id = ?", (pack_id,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        assert row is not None
        manifest = json.loads(str(row["manifest_json"]))
        self.assertEqual(str(row["pack_id"]), pack_id)
        self.assertEqual(int(row["row_count"]), int(result["structuredContent"]["selection"]["exported_rows"]))
        self.assertEqual(int(row["signed"]), 0)
        self.assertEqual(
            str(manifest["content_hash"]["value"]),
            str(result["structuredContent"]["content_hash"]["value"]),
        )

    def test_pack_export_failure_no_audit_row(self) -> None:
        self.record("phase2c failure no audit", kind="context_block")
        before = self._exported_packs_count()
        result = self._pack_export_error(pack_name="phase2c_failure_no_audit")
        self.assertIn("allow_unsigned=true", result["content"][0]["text"])
        self.assertEqual(self._exported_packs_count(), before)

    def test_pack_export_rejects_interaction_log_and_agent_feedback(self) -> None:
        self.record("phase2c interaction log", kind="interaction_log")
        self.record("phase2c feedback", kind="agent_feedback", role="specialist")
        before = self._exported_packs_count()
        output_dir = self.root / "phase2c_reject_kind"
        bad_log = self._pack_export_error(
            pack_name="phase2c_reject_interaction",
            output_dir=str(output_dir),
            allow_unsigned=True,
            kinds=["interaction_log"],
        )
        self.assertIn("kind 'interaction_log' is previewable but not exportable", bad_log["content"][0]["text"])
        bad_feedback = self._pack_export_error(
            pack_name="phase2c_reject_feedback",
            output_dir=str(output_dir),
            allow_unsigned=True,
            kinds=["agent_feedback"],
        )
        self.assertIn("kind 'agent_feedback' is previewable but not exportable", bad_feedback["content"][0]["text"])
        self.assertEqual(self._exported_packs_count(), before)
        if output_dir.exists():
            self.assertEqual(list(output_dir.glob("*.mem")), [])

    def test_pack_export_empty_selection_fails(self) -> None:
        self.record("phase2c empty selection marker", kind="context_block")
        before = self._exported_packs_count()
        output_dir = self.root / "phase2c_empty_selection"
        result = self._pack_export_error(
            pack_name="phase2c_empty_selection",
            output_dir=str(output_dir),
            allow_unsigned=True,
            topics=["missing-phase2c-topic"],
        )
        self.assertIn("selection returned zero rows", result["content"][0]["text"])
        self.assertEqual(self._exported_packs_count(), before)
        if output_dir.exists():
            self.assertEqual(list(output_dir.glob("*.mem")), [])

    def test_pack_export_limited_selection_requires_override(self) -> None:
        for idx in range(6):
            self.record(f"phase2c limited selection {idx}", kind="context_block")
        output_dir = self.root / "phase2c_limited"
        failed = self._pack_export_error(
            pack_name="phase2c_limited_fail",
            output_dir=str(output_dir),
            allow_unsigned=True,
            limit=2,
        )
        self.assertEqual(failed["structuredContent"]["error"]["code"], "limited_export_requires_confirmation")
        self.assertIn("selection is limited", failed["content"][0]["text"])
        success = self._pack_export(
            pack_name="phase2c_limited_success",
            output_dir=str(output_dir),
            allow_unsigned=True,
            limit=2,
            allow_limited_export=True,
        )
        warnings = success["structuredContent"]["warnings"]
        self.assertTrue(any(item.get("code") == "limited_export" for item in warnings))
        output_path = Path(success["structuredContent"]["output_path"])
        with zipfile.ZipFile(output_path, "r") as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        selection = manifest["selection"]
        self.assertGreater(int(selection["total_rows"]), int(selection["exported_rows"]))
        self.assertEqual(int(selection["exported_rows"]), 2)

    def test_pack_export_group_selector_matches_memory_group_preview_export(self) -> None:
        first = self.record("group export first", kind="context_block")
        second = self.record("group export second", kind="hippocampus_entry")
        extra = self.record("group export extra", kind="context_block")
        for memory_id in (str(first["id"]), str(second["id"])):
            added = server.topic_add({"memory_id": memory_id, "topic": "phase216-group-export", "source": "operator"})
            self.assertFalse(added["isError"], added)
        linked = server.memory_link({"source_id": str(first["id"]), "target_id": str(extra["id"]), "relation": "related"})
        self.assertFalse(linked["isError"], linked)

        group = self._memory_group_preview(group_id="topic:phase216-group-export", scope="core_plus_related", limit=50)
        expected_ids = group["structuredContent"]["selection"]["memory_ids"]
        result = self._pack_export(
            pack_name="phase216_group_selector",
            output_dir=str(self.root / "phase216_group_selector"),
            group_id="topic:phase216-group-export",
            scope="core_plus_related",
            allow_unsigned=True,
        )
        members = self._read_zip_members(Path(result["structuredContent"]["output_path"]))
        manifest = json.loads(members["manifest.json"].decode("utf-8"))
        rows = [json.loads(line) for line in members["content/memories.jsonl"].decode("utf-8").splitlines() if line.strip()]
        self.assertEqual(manifest["selection"]["filters"]["group_id"], "topic:phase216-group-export")
        self.assertEqual(manifest["selection"]["filters"]["scope"], "core_plus_related")
        self.assertEqual(len(rows), len(expected_ids))

    def test_pack_export_group_selector_rejects_mixed_selectors(self) -> None:
        result = self._pack_export_error(
            pack_name="phase216_group_selector_mixed",
            output_dir=str(self.root / "phase216_group_selector_mixed"),
            group_id="topic:phase216-group-mixed",
            memory_ids=["mem_1"],
            allow_unsigned=True,
        )
        self.assertEqual(result["structuredContent"]["error"]["code"], "ambiguous_selector")

    def test_pack_export_group_selector_limited_requires_confirmation(self) -> None:
        first = self.record("group limited 1", kind="context_block")
        second = self.record("group limited 2", kind="context_block")
        third = self.record("group limited 3", kind="context_block")
        for memory_id in (str(first["id"]), str(second["id"]), str(third["id"])):
            added = server.topic_add({"memory_id": memory_id, "topic": "phase216-group-limited", "source": "operator"})
            self.assertFalse(added["isError"], added)

        failed = self._pack_export_error(
            pack_name="phase216_group_limited_fail",
            output_dir=str(self.root / "phase216_group_limited"),
            group_id="topic:phase216-group-limited",
            scope="core",
            limit=2,
            allow_unsigned=True,
        )
        self.assertEqual(failed["structuredContent"]["error"]["code"], "limited_export_requires_confirmation")
        success = self._pack_export(
            pack_name="phase216_group_limited_ok",
            output_dir=str(self.root / "phase216_group_limited"),
            group_id="topic:phase216-group-limited",
            scope="core",
            limit=2,
            allow_unsigned=True,
            allow_limited_export=True,
        )
        warnings = success["structuredContent"]["warnings"]
        self.assertTrue(any(item.get("code") == "limited_export" for item in warnings))

    def test_pack_export_topic_filter_uses_memory_topics(self) -> None:
        body_only = self.record("phase2c topic auth body-only marker", kind="context_block")
        tagged = self.record("phase2c tagged memory", kind="context_block")
        topic_add = server.topic_add({"memory_id": tagged["id"], "topic": "phase2c-auth", "source": "operator"})
        self.assertFalse(topic_add["isError"], topic_add)
        output_dir = self.root / "phase2c_topic_filter"
        result = self._pack_export(
            pack_name="phase2c_topic_filter",
            output_dir=str(output_dir),
            allow_unsigned=True,
            topics=["phase2c-auth"],
            kinds=["context_block"],
        )
        output_path = Path(result["structuredContent"]["output_path"])
        with zipfile.ZipFile(output_path, "r") as archive:
            rows = [
                json.loads(line)
                for line in archive.read("content/memories.jsonl").decode("utf-8").splitlines()
                if line.strip()
            ]
        self.assertEqual(len(rows), 1)
        text_value = rows[0]["text_fields"]["text"]
        self.assertIn("phase2c tagged memory", text_value)
        self.assertNotIn(body_only["id"], json.dumps(rows, ensure_ascii=True))

    def test_pack_export_memory_ids_exact_selector(self) -> None:
        first = self.record("phase213 exact export first", kind="context_block")
        second = self.record("phase213 exact export second", kind="hippocampus_entry")
        self.record("phase213 exact export ignored", kind="context_block")
        result = self._pack_export(
            pack_name="phase213_exact_export",
            output_dir=str(self.root / "phase213_exact_export"),
            allow_unsigned=True,
            memory_ids=[str(first["id"]), str(second["id"])],
            limit=50,
        )
        members = self._read_zip_members(Path(result["structuredContent"]["output_path"]))
        rows = [
            json.loads(line)
            for line in members["content/memories.jsonl"].decode("utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(str(row["row_id_in_pack"]) for row in rows), ["ctx_001", "hip_001"])

    def test_pack_export_memory_ids_unknown_ids_safe(self) -> None:
        result = self._pack_export_error(
            pack_name="phase213_unknown_ids_safe",
            output_dir=str(self.root / "phase213_unknown_ids_safe"),
            allow_unsigned=True,
            memory_ids=["mem_missing_a", "mem_missing_b"],
            limit=50,
        )
        self.assertIn("nothing to export", result["content"][0]["text"].lower())

    def test_pack_export_touched_paths_filter(self) -> None:
        auth_file = self.workspace / "src" / "auth" / "session.py"
        auth_file.parent.mkdir(parents=True, exist_ok=True)
        auth_file.write_text("phase2c\n", encoding="utf-8")
        other_file = self.workspace / "src" / "billing" / "ledger.py"
        other_file.parent.mkdir(parents=True, exist_ok=True)
        other_file.write_text("phase2c\n", encoding="utf-8")
        self.record(
            "phase2c touched auth",
            kind="context_block",
            touched_files=["src/auth/session.py"],
        )
        self.record(
            "phase2c touched billing",
            kind="context_block",
            touched_files=["src/billing/ledger.py"],
        )
        output_dir = self.root / "phase2c_touched_paths"
        result = self._pack_export(
            pack_name="phase2c_touched_paths",
            output_dir=str(output_dir),
            allow_unsigned=True,
            touched_paths=["src/auth/session.py"],
            kinds=["context_block"],
        )
        output_path = Path(result["structuredContent"]["output_path"])
        with zipfile.ZipFile(output_path, "r") as archive:
            rows = [
                json.loads(line)
                for line in archive.read("content/memories.jsonl").decode("utf-8").splitlines()
                if line.strip()
            ]
        self.assertEqual(len(rows), 1)
        touched_files = rows[0]["touched_files"]
        self.assertEqual(len(touched_files), 1)
        self.assertEqual(str(touched_files[0]["path"]), "src/auth/session.py")

    def test_pack_export_read_only_except_audit_and_zip(self) -> None:
        self.record("phase2c read-only export row", kind="context_block")
        before = {
            "memories": self._table_count("memories"),
            "memory_topics": self._table_count("memory_topics"),
            "memory_files": self._table_count("memory_files"),
            "imported_packs": self._table_count("imported_packs"),
            "alias_concepts": self._table_count("alias_concepts"),
            "alias_terms": self._table_count("alias_terms"),
            "alias_proposals": self._table_count("alias_proposals"),
            "alias_proposal_events": self._table_count("alias_proposal_events"),
            "exported_packs": self._table_count("exported_packs"),
        }
        output_dir = self.root / "phase2c_read_only"
        result = self._pack_export(
            pack_name="phase2c_read_only",
            output_dir=str(output_dir),
            allow_unsigned=True,
        )
        after = {
            "memories": self._table_count("memories"),
            "memory_topics": self._table_count("memory_topics"),
            "memory_files": self._table_count("memory_files"),
            "imported_packs": self._table_count("imported_packs"),
            "alias_concepts": self._table_count("alias_concepts"),
            "alias_terms": self._table_count("alias_terms"),
            "alias_proposals": self._table_count("alias_proposals"),
            "alias_proposal_events": self._table_count("alias_proposal_events"),
            "exported_packs": self._table_count("exported_packs"),
        }
        self.assertEqual(before["memories"], after["memories"])
        self.assertEqual(before["memory_topics"], after["memory_topics"])
        self.assertEqual(before["memory_files"], after["memory_files"])
        self.assertEqual(before["imported_packs"], after["imported_packs"])
        self.assertEqual(before["alias_concepts"], after["alias_concepts"])
        self.assertEqual(before["alias_terms"], after["alias_terms"])
        self.assertEqual(before["alias_proposals"], after["alias_proposals"])
        self.assertEqual(before["alias_proposal_events"], after["alias_proposal_events"])
        self.assertEqual(before["exported_packs"] + 1, after["exported_packs"])
        self.assertTrue(Path(result["structuredContent"]["output_path"]).exists())

    def test_pack_export_pack_local_row_ids(self) -> None:
        self.record("phase2c row ids context a", kind="context_block")
        self.record("phase2c row ids hip a", kind="hippocampus_entry")
        self.record("phase2c row ids context b", kind="context_block")
        self.record("phase2c row ids hip b", kind="hippocampus_entry")
        output_dir = self.root / "phase2c_row_ids"
        result = self._pack_export(
            pack_name="phase2c_row_ids",
            output_dir=str(output_dir),
            allow_unsigned=True,
            kinds=["context_block", "hippocampus_entry"],
        )
        output_path = Path(result["structuredContent"]["output_path"])
        with zipfile.ZipFile(output_path, "r") as archive:
            rows = [
                json.loads(line)
                for line in archive.read("content/memories.jsonl").decode("utf-8").splitlines()
                if line.strip()
            ]
        ctx_ids = [str(row["row_id_in_pack"]) for row in rows if str(row["kind"]) == "context_block"]
        hip_ids = [str(row["row_id_in_pack"]) for row in rows if str(row["kind"]) == "hippocampus_entry"]
        self.assertEqual(ctx_ids, [f"ctx_{idx:03d}" for idx in range(1, len(ctx_ids) + 1)])
        self.assertEqual(hip_ids, [f"hip_{idx:03d}" for idx in range(1, len(hip_ids) + 1)])

    def test_pack_export_zip_validation_failure_no_success(self) -> None:
        self.record("phase2c forced validation failure", kind="context_block")
        output_dir = self.root / "phase2c_validation_fail"
        before = self._exported_packs_count()
        with mock.patch("server._pack_validate_zip", side_effect=ValueError("forced invalid zip")):
            result = self._pack_export_error(
                pack_name="phase2c_validation_fail",
                output_dir=str(output_dir),
                allow_unsigned=True,
            )
        self.assertIn("forced invalid zip", result["content"][0]["text"])
        self.assertEqual(self._exported_packs_count(), before)
        if output_dir.exists():
            leftovers = [
                path
                for path in output_dir.iterdir()
                if path.suffix in {".mem", ".tmp"} or path.name.endswith(".mem.tmp")
            ]
            self.assertEqual(leftovers, [])

    def test_pack_export_rejects_unsafe_pack_name(self) -> None:
        self.record("phase2c unsafe names", kind="context_block")
        before = self._exported_packs_count()
        empty_name = self._pack_export_error(pack_name="   ", allow_unsigned=True)
        self.assertIn("pack_name", empty_name["content"][0]["text"])
        leading_dot = self._pack_export_error(pack_name=".hidden_pack", allow_unsigned=True)
        self.assertIn("cannot start with", leading_dot["content"][0]["text"])
        reserved = self._pack_export_error(pack_name="CON", allow_unsigned=True)
        self.assertIn("reserved", reserved["content"][0]["text"])
        self.assertEqual(self._exported_packs_count(), before)

    def test_pack_export_filename_sanitization(self) -> None:
        self.record("phase2c filename sanitize row", kind="context_block")
        output_dir = self.root / "phase2c_safe_output"
        result = self._pack_export(
            pack_name="my pack ../unsafe::name!!",
            output_dir=str(output_dir),
            allow_unsigned=True,
        )
        output_path = Path(result["structuredContent"]["output_path"]).resolve()
        self.assertEqual(output_path.parent, output_dir.resolve())
        self.assertNotIn(" ", output_path.name)
        self.assertNotIn("/", output_path.name)
        self.assertNotIn("\\", output_path.name)

    def test_pack_export_preview_redaction_parity(self) -> None:
        first = self.record(
            "phase2c parity one test.user@example.test AKIA1234567890ABCDEF",
            kind="context_block",
        )
        second = self.record(
            "phase2c parity two test.user@example.test 10.22.33.44",
            kind="hippocampus_entry",
        )
        server.topic_add({"memory_id": first["id"], "topic": "phase2c-parity", "source": "operator"})
        server.topic_add({"memory_id": second["id"], "topic": "phase2c-parity", "source": "operator"})
        preview = self._pack_redaction_preview(
            topics=["phase2c-parity"],
            kinds=["context_block", "hippocampus_entry"],
            limit=100,
        )
        export = self._pack_export(
            pack_name="phase2c_parity",
            output_dir=str(self.root / "phase2c_parity"),
            allow_unsigned=True,
            topics=["phase2c-parity"],
            kinds=["context_block", "hippocampus_entry"],
            limit=100,
        )
        preview_redaction = preview["structuredContent"]["redaction"]
        export_redaction = export["structuredContent"]["redaction"]
        self.assertEqual(int(preview_redaction["total_matches"]), int(export_redaction["total_matches"]))
        self.assertEqual(
            {str(k): int(v) for k, v in preview_redaction["by_category"].items()},
            {str(k): int(v) for k, v in export_redaction["by_category"].items()},
        )

    def test_pack_export_all_rows_redacted_edge_case(self) -> None:
        self.record("phase2c all redacted one test.user@example.test", kind="context_block")
        self.record("phase2c all redacted two test.user@example.test", kind="hippocampus_entry")
        result = self._pack_export(
            pack_name="phase2c_all_redacted",
            output_dir=str(self.root / "phase2c_all_redacted"),
            allow_unsigned=True,
            kinds=["context_block", "hippocampus_entry"],
        )
        payload = result["structuredContent"]
        self.assertEqual(int(payload["redaction"]["affected_rows"]), int(payload["selection"]["exported_rows"]))

    def test_pack_export_ruleset_version_constant(self) -> None:
        self.record("phase2c ruleset constant test.user@example.test", kind="context_block")
        result = self._pack_export(
            pack_name="phase2c_ruleset",
            output_dir=str(self.root / "phase2c_ruleset"),
            allow_unsigned=True,
        )
        output_path = Path(result["structuredContent"]["output_path"])
        with zipfile.ZipFile(output_path, "r") as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            redactions = json.loads(archive.read("provenance/redactions.json").decode("utf-8"))
        self.assertEqual(str(manifest["redaction_ruleset_version"]), "baseline-v1")
        self.assertEqual(str(redactions["ruleset_version"]), "baseline-v1")
        self.assertEqual(str(result["structuredContent"]["redaction"]["ruleset_version"]), "baseline-v1")

    def test_pack_inspect_valid_exported_pack(self) -> None:
        self.record("phase3a valid inspect row", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3a_valid_pack",
            output_dir=self.root / "phase3a_valid_pack",
        )
        result = self._pack_inspect(pack_path=str(pack_path))
        payload = result["structuredContent"]
        self.assertEqual(payload["status"], "valid")
        self.assertEqual(payload["import_recommendation"], "quarantine_only")
        warnings = payload["warnings"]
        self.assertTrue(any(item.get("code") == "unsigned_pack" for item in warnings))

    def test_pack_inspect_mem_no_non_zip_warning(self) -> None:
        self.record("phase3a mem inspect row", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3a_mem_pack",
            output_dir=self.root / "phase3a_mem_pack",
        )
        self.assertEqual(pack_path.suffix.lower(), ".mem")
        result = self._pack_inspect(pack_path=str(pack_path))
        warnings = result["structuredContent"]["warnings"]
        codes = {str(item.get("code", "")) for item in warnings if isinstance(item, dict)}
        self.assertNotIn("non_zip_suffix", codes)
        self.assertNotIn("nonstandard_pack_suffix", codes)
        self.assertNotIn("legacy_zip_suffix", codes)

    def test_pack_inspect_zip_legacy_warning(self) -> None:
        self.record("phase3a zip legacy warning row", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3a_zip_legacy",
            output_dir=self.root / "phase3a_zip_legacy",
        )
        legacy_path = pack_path.with_suffix(".zip")
        shutil.copyfile(pack_path, legacy_path)
        result = self._pack_inspect(pack_path=str(legacy_path))
        warnings = result["structuredContent"]["warnings"]
        self.assertTrue(any(str(item.get("code", "")) == "legacy_zip_suffix" for item in warnings))

    def test_pack_inspect_content_hash_verifies(self) -> None:
        self.record("phase3a hash verify", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3a_hash_verify",
            output_dir=self.root / "phase3a_hash_verify",
        )
        result = self._pack_inspect(pack_path=str(pack_path))
        content_hash = result["structuredContent"]["content_hash"]
        self.assertTrue(bool(content_hash["valid"]))
        self.assertEqual(str(content_hash["manifest_value"]), str(content_hash["recomputed_value"]))

    def test_pack_inspect_tampered_content_rejected(self) -> None:
        self.record("phase3a tamper base", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3a_tamper_base",
            output_dir=self.root / "phase3a_tamper_base",
        )
        tampered = self.root / "phase3a_tamper_base" / "tampered.zip"
        with zipfile.ZipFile(pack_path, "r") as archive:
            rows = archive.read("content/memories.jsonl").decode("utf-8")
        tampered_rows = rows + json.dumps({"row_id_in_pack": "ctx_999", "kind": "context_block"}) + "\n"
        self._rewrite_zip(
            pack_path,
            tampered,
            replace_members={"content/memories.jsonl": tampered_rows.encode("utf-8")},
        )
        result = self._pack_inspect(pack_path=str(tampered))
        payload = result["structuredContent"]
        self.assertEqual(payload["status"], "invalid")
        self.assertFalse(bool(payload["content_hash"]["valid"]))
        self.assertEqual(payload["import_recommendation"], "reject")

    def test_pack_inspect_covered_members_tampering_rejected(self) -> None:
        self.record("phase3a covered members", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3a_covered_members",
            output_dir=self.root / "phase3a_covered_members",
        )
        tampered = self.root / "phase3a_covered_members" / "tampered_covered.zip"
        with zipfile.ZipFile(pack_path, "r") as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            members = {name: archive.read(name) for name in archive.namelist()}
        covered_members = [
            "content/memories.jsonl",
            "content/topics.json",
            "provenance/origin.json",
            "provenance/redactions.json",
        ]
        lines: list[str] = []
        for name in sorted(covered_members):
            lines.append(f"{name}\t{hashlib.sha256(members[name]).hexdigest()}\n")
        manifest["content_hash"]["covered_members"] = covered_members
        manifest["content_hash"]["value"] = hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()
        self._rewrite_zip(
            pack_path,
            tampered,
            replace_members={"manifest.json": (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")},
        )
        result = self._pack_inspect(pack_path=str(tampered))
        payload = result["structuredContent"]
        self.assertEqual(payload["status"], "invalid")
        self.assertEqual(payload["import_recommendation"], "reject")
        self.assertTrue(any(item.get("code") == "covered_members_mismatch" for item in payload["errors"]))

    def test_pack_inspect_missing_required_member_rejected(self) -> None:
        self.record("phase3a missing member", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3a_missing_member",
            output_dir=self.root / "phase3a_missing_member",
        )
        tampered = self.root / "phase3a_missing_member" / "missing_member.zip"
        self._rewrite_zip(pack_path, tampered, remove_members={"provenance/redactions.json"})
        result = self._pack_inspect(pack_path=str(tampered))
        self.assertEqual(result["structuredContent"]["status"], "invalid")

    def test_pack_inspect_malformed_manifest_rejected(self) -> None:
        self.record("phase3a malformed manifest", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3a_malformed_manifest",
            output_dir=self.root / "phase3a_malformed_manifest",
        )
        tampered = self.root / "phase3a_malformed_manifest" / "malformed_manifest.zip"
        self._rewrite_zip(pack_path, tampered, replace_members={"manifest.json": b"{invalid json"})
        result = self._pack_inspect(pack_path=str(tampered))
        self.assertEqual(result["structuredContent"]["status"], "invalid")

    def test_pack_inspect_unsupported_schema_rejected(self) -> None:
        self.record("phase3a unsupported schema", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3a_unsupported_schema",
            output_dir=self.root / "phase3a_unsupported_schema",
        )
        tampered = self.root / "phase3a_unsupported_schema" / "unsupported_schema.zip"
        with zipfile.ZipFile(pack_path, "r") as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        manifest["pack_schema_version"] = 999
        self._rewrite_zip(
            pack_path,
            tampered,
            replace_members={"manifest.json": (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")},
        )
        result = self._pack_inspect(pack_path=str(tampered))
        payload = result["structuredContent"]
        self.assertEqual(payload["status"], "unsupported")
        self.assertEqual(payload["import_recommendation"], "reject")

    def test_pack_inspect_source_memory_id_leak_rejected(self) -> None:
        self.record("phase3a source id leak", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3a_source_id_leak",
            output_dir=self.root / "phase3a_source_id_leak",
        )
        tampered = self.root / "phase3a_source_id_leak" / "source_id_leak.zip"
        with zipfile.ZipFile(pack_path, "r") as archive:
            rows_blob = archive.read("content/memories.jsonl").decode("utf-8")
        leak_rows = rows_blob + json.dumps({"leak": "mem_fake_leak_id"}) + "\n"
        self._rewrite_zip(
            pack_path,
            tampered,
            replace_members={"content/memories.jsonl": leak_rows.encode("utf-8")},
        )
        result = self._pack_inspect(pack_path=str(tampered), include_samples=True)
        payload = result["structuredContent"]
        self.assertEqual(payload["status"], "invalid")
        self.assertTrue(any(item.get("code") == "source_memory_id_leak" for item in payload["errors"]))
        self.assertEqual(payload["samples"], [])

    def test_pack_inspect_rejects_interaction_log_rows(self) -> None:
        self.record("phase3a interaction row", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3a_interaction_row",
            output_dir=self.root / "phase3a_interaction_row",
        )
        tampered = self.root / "phase3a_interaction_row" / "interaction_kind.zip"
        with zipfile.ZipFile(pack_path, "r") as archive:
            rows = [json.loads(line) for line in archive.read("content/memories.jsonl").decode("utf-8").splitlines() if line.strip()]
        rows[0]["kind"] = "interaction_log"
        rows_blob = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in rows)
        self._rewrite_zip(
            pack_path,
            tampered,
            replace_members={"content/memories.jsonl": rows_blob.encode("utf-8")},
        )
        result = self._pack_inspect(pack_path=str(tampered))
        self.assertEqual(result["structuredContent"]["status"], "invalid")

    def test_pack_inspect_redaction_metadata_validation(self) -> None:
        self.record("phase3a redaction metadata", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3a_redaction_metadata",
            output_dir=self.root / "phase3a_redaction_metadata",
        )
        tampered = self.root / "phase3a_redaction_metadata" / "redaction_ruleset_mismatch.zip"
        with zipfile.ZipFile(pack_path, "r") as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            redactions = json.loads(archive.read("provenance/redactions.json").decode("utf-8"))
        redactions["ruleset_version"] = "baseline-v2"
        self._rewrite_zip(
            pack_path,
            tampered,
            replace_members={"provenance/redactions.json": (json.dumps(redactions, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")},
        )
        result = self._pack_inspect(pack_path=str(tampered))
        self.assertIn(result["structuredContent"]["status"], {"invalid", "unsupported"})

    def test_pack_inspect_redaction_counts_exact_match(self) -> None:
        self.record("phase3a redaction counts test.user@example.test", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3a_redaction_counts",
            output_dir=self.root / "phase3a_redaction_counts",
        )
        tampered = self.root / "phase3a_redaction_counts" / "redaction_counts_mismatch.zip"
        with zipfile.ZipFile(pack_path, "r") as archive:
            redactions = json.loads(archive.read("provenance/redactions.json").decode("utf-8"))
        redactions["total_matches"] = int(redactions.get("total_matches", 0)) + 1
        self._rewrite_zip(
            pack_path,
            tampered,
            replace_members={"provenance/redactions.json": (json.dumps(redactions, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")},
        )
        result = self._pack_inspect(pack_path=str(tampered))
        payload = result["structuredContent"]
        self.assertEqual(payload["status"], "invalid")
        self.assertFalse(bool(payload["validation"]["redaction_metadata_valid"]))

    def test_pack_inspect_redaction_applied_required_true(self) -> None:
        self.record("phase3a redaction applied row", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3a_redaction_applied",
            output_dir=self.root / "phase3a_redaction_applied",
        )
        tampered = self.root / "phase3a_redaction_applied" / "redaction_applied_false.zip"
        with zipfile.ZipFile(pack_path, "r") as archive:
            rows = [json.loads(line) for line in archive.read("content/memories.jsonl").decode("utf-8").splitlines() if line.strip()]
        rows[0]["redaction_applied"] = False
        rows_blob = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in rows)
        self._rewrite_zip(
            pack_path,
            tampered,
            replace_members={"content/memories.jsonl": rows_blob.encode("utf-8")},
        )
        result = self._pack_inspect(pack_path=str(tampered))
        payload = result["structuredContent"]
        self.assertEqual(payload["status"], "invalid")
        self.assertFalse(bool(payload["validation"]["redaction_metadata_valid"]))

    def test_pack_inspect_read_only(self) -> None:
        self.record("phase3a read only inspect", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3a_read_only_inspect",
            output_dir=self.root / "phase3a_read_only_inspect",
        )
        before = {
            "memories": self._table_count("memories"),
            "imported_packs": self._table_count("imported_packs"),
            "exported_packs": self._table_count("exported_packs"),
            "memory_topics": self._table_count("memory_topics"),
            "memory_files": self._table_count("memory_files"),
        }
        result = self._pack_inspect(pack_path=str(pack_path))
        self.assertEqual(result["structuredContent"]["status"], "valid")
        after = {
            "memories": self._table_count("memories"),
            "imported_packs": self._table_count("imported_packs"),
            "exported_packs": self._table_count("exported_packs"),
            "memory_topics": self._table_count("memory_topics"),
            "memory_files": self._table_count("memory_files"),
        }
        self.assertEqual(before, after)

    def test_pack_inspect_samples_bounded(self) -> None:
        for idx in range(4):
            self.record(f"phase3a sample bounded {idx}", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3a_samples_bounded",
            output_dir=self.root / "phase3a_samples_bounded",
        )
        valid = self._pack_inspect(pack_path=str(pack_path), include_samples=True, sample_limit=2)
        self.assertLessEqual(len(valid["structuredContent"]["samples"]), 2)
        tampered = self.root / "phase3a_samples_bounded" / "invalid_for_samples.zip"
        self._rewrite_zip(pack_path, tampered, remove_members={"provenance/redactions.json"})
        invalid = self._pack_inspect(pack_path=str(tampered), include_samples=True, sample_limit=2)
        self.assertEqual(invalid["structuredContent"]["samples"], [])

    def test_pack_inspect_zip_safety_path_traversal(self) -> None:
        zip_path = self.root / "phase3a_traversal.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("../evil.json", "{}")
        marker = self.root / "evil.json"
        if marker.exists():
            marker.unlink()
        result = self._pack_inspect(pack_path=str(zip_path))
        self.assertEqual(result["structuredContent"]["status"], "invalid")
        self.assertFalse(marker.exists())

    def test_pack_inspect_zip_safety_backslash_drive_control_chars(self) -> None:
        zip_path = self.root / "phase3a_bad_names.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("C:evil.json", "{}")
            archive.writestr("bad\x01name.json", "{}")
        result = self._pack_inspect(pack_path=str(zip_path))
        self.assertEqual(result["structuredContent"]["status"], "invalid")

    def test_pack_inspect_zip_safety_duplicate_members(self) -> None:
        zip_path = self.root / "phase3a_duplicate_members.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", "{}")
            archive.writestr("manifest.json", "{}")
        result = self._pack_inspect(pack_path=str(zip_path))
        self.assertEqual(result["structuredContent"]["status"], "invalid")

    def test_pack_inspect_non_zip_rejected(self) -> None:
        file_path = self.root / "phase3a_not_zip.txt"
        file_path.write_text("not a zip", encoding="utf-8")
        result = self._pack_inspect(pack_path=str(file_path))
        payload = result["structuredContent"]
        self.assertEqual(payload["status"], "invalid")
        self.assertEqual(payload["import_recommendation"], "reject")

    def test_pack_inspect_no_extraction(self) -> None:
        zip_path = self.root / "phase3a_no_extract.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("../outside.txt", "oops")
        outside = self.root / "outside.txt"
        if outside.exists():
            outside.unlink()
        result = self._pack_inspect(pack_path=str(zip_path))
        self.assertEqual(result["structuredContent"]["status"], "invalid")
        self.assertFalse(outside.exists())

    def test_pack_inspect_timestamp_validation(self) -> None:
        self.record("phase3a timestamp validation", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3a_timestamp_validation",
            output_dir=self.root / "phase3a_timestamp_validation",
        )
        tampered = self.root / "phase3a_timestamp_validation" / "bad_timestamp.zip"
        with zipfile.ZipFile(pack_path, "r") as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        manifest["created_at"] = "2026/05/25 00:00:00"
        self._rewrite_zip(
            pack_path,
            tampered,
            replace_members={"manifest.json": (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")},
        )
        result = self._pack_inspect(pack_path=str(tampered))
        self.assertEqual(result["structuredContent"]["status"], "invalid")

    def test_pack_inspect_extra_member_warning(self) -> None:
        self.record("phase3a extra member warning", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3a_extra_member_warning",
            output_dir=self.root / "phase3a_extra_member_warning",
        )
        tampered = self.root / "phase3a_extra_member_warning" / "extra_member.zip"
        self._rewrite_zip(pack_path, tampered, extra_members={"future/unknown.json": b"{}"})
        result = self._pack_inspect(pack_path=str(tampered))
        payload = result["structuredContent"]
        self.assertEqual(payload["status"], "valid")
        self.assertTrue(any(item.get("code") == "unknown_extra_member" for item in payload["warnings"]))

    def test_pack_inspect_text_field_warning(self) -> None:
        self.record("phase3a text field warning", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3a_text_field_warning",
            output_dir=self.root / "phase3a_text_field_warning",
        )
        tampered = self.root / "phase3a_text_field_warning" / "unexpected_text_field.zip"
        with zipfile.ZipFile(pack_path, "r") as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            rows = [json.loads(line) for line in archive.read("content/memories.jsonl").decode("utf-8").splitlines() if line.strip()]
            members = {name: archive.read(name) for name in archive.namelist()}
        rows[0]["text_fields"]["body"] = "extra field"
        rows_blob = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in rows)
        members["content/memories.jsonl"] = rows_blob.encode("utf-8")
        covered_members = list(manifest.get("content_hash", {}).get("covered_members", server.PACK_CONTENT_HASH_COVERED_MEMBERS))
        manifest["content_hash"]["value"] = self._recompute_pack_content_hash(members, covered_members)
        self._rewrite_zip(
            pack_path,
            tampered,
            replace_members={
                "content/memories.jsonl": rows_blob.encode("utf-8"),
                "manifest.json": (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
            },
        )
        result = self._pack_inspect(pack_path=str(tampered))
        payload = result["structuredContent"]
        self.assertEqual(payload["status"], "valid")
        self.assertTrue(any(item.get("code") == "unexpected_text_field" for item in payload["warnings"]))

    def test_pack_import_requires_allow_unsigned_quarantine(self) -> None:
        self.record("phase3b import requires allow flag", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_requires_allow",
            output_dir=self.root / "phase3b_requires_allow",
        )
        before_imported = self._table_count("imported_packs")
        before_memories = self._table_count("memories")
        failed = self._pack_import_error(pack_path=str(pack_path))
        self.assertEqual(self._pack_error_code(failed), "import_target_not_allowed")
        self.assertEqual(before_imported, self._table_count("imported_packs"))
        self.assertEqual(before_memories, self._table_count("memories"))

    def test_pack_import_valid_unsigned_pack_to_quarantine(self) -> None:
        self.record("phase3b valid quarantine import", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_valid_quarantine",
            output_dir=self.root / "phase3b_valid_quarantine",
        )
        result = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        payload = result["structuredContent"]
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["trust_level"], "quarantine")
        self.assertTrue(str(payload["namespace"]).startswith("pack:quarantine:"))
        self.assertIn(str(payload["pack_id"]), str(payload["namespace"]))

    def test_pack_import_mem_supported(self) -> None:
        self.record("phase3b mem import supported", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_mem_supported",
            output_dir=self.root / "phase3b_mem_supported",
        )
        self.assertEqual(pack_path.suffix.lower(), ".mem")
        result = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        payload = result["structuredContent"]
        self.assertEqual(payload["status"], "ok")
        warnings = payload.get("warnings", [])
        codes = {str(item.get("code", "")) for item in warnings if isinstance(item, dict)}
        self.assertNotIn("legacy_zip_suffix", codes)

    def test_pack_import_zip_legacy_warning_supported(self) -> None:
        self.record("phase3b legacy zip import supported", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_legacy_zip_supported",
            output_dir=self.root / "phase3b_legacy_zip_supported",
        )
        legacy_path = pack_path.with_suffix(".zip")
        shutil.copyfile(pack_path, legacy_path)
        result = self._pack_import(pack_path=str(legacy_path), allow_unsigned_quarantine=True)
        warnings = result["structuredContent"].get("warnings", [])
        self.assertTrue(any(str(item.get("code", "")) == "legacy_zip_suffix" for item in warnings))

    def test_pack_import_inserts_imported_packs_row(self) -> None:
        self.record("phase3b imported pack row", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_imported_pack_row",
            output_dir=self.root / "phase3b_imported_pack_row",
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        pack_id = str(imported["structuredContent"]["pack_id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM imported_packs WHERE pack_id = ?", (pack_id,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(str(row["trust_level"]), "quarantine")
        self.assertTrue(str(row["received_zip_sha256"]))
        self.assertIsInstance(json.loads(str(row["manifest_json"])), dict)
        self.assertIsInstance(json.loads(str(row["freshness_summary_json"])), dict)

    def test_pack_import_creates_new_memory_ids(self) -> None:
        source = self.record("phase3b source memory id check", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_new_mem_ids",
            output_dir=self.root / "phase3b_new_mem_ids",
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        imported_rows = imported["structuredContent"]["imported_rows"]
        self.assertTrue(imported_rows)
        source_id = str(source["id"])
        for row in imported_rows:
            memory_id = str(row["memory_id"])
            row_id_in_pack = str(row["row_id_in_pack"])
            self.assertTrue(memory_id.startswith("mem_"))
            self.assertNotEqual(memory_id, row_id_in_pack)
            self.assertNotEqual(memory_id, source_id)

    def test_pack_import_sets_namespace_origin_freshness(self) -> None:
        self.record("phase3b namespace origin freshness", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_namespace_origin_freshness",
            output_dir=self.root / "phase3b_namespace_origin_freshness",
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        payload = imported["structuredContent"]
        imported_ids = [str(item["memory_id"]) for item in payload["imported_rows"]]
        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            placeholders = ",".join("?" for _ in imported_ids)
            rows = conn.execute(
                f"SELECT id, namespace, origin, import_freshness FROM memories WHERE id IN ({placeholders})",
                tuple(imported_ids),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), len(imported_ids))
        for row in rows:
            self.assertEqual(str(row["namespace"]), str(payload["namespace"]))
            self.assertEqual(str(row["origin"]), "imported")
            self.assertIn(str(row["import_freshness"]), {"verified", "stale", "missing", "unknown"})

    def test_pack_import_preserves_git_provenance(self) -> None:
        self.record("phase3b git provenance base", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_git_provenance",
            output_dir=self.root / "phase3b_git_provenance",
        )
        tampered = self.root / "phase3b_git_provenance" / "git_provenance.zip"
        with zipfile.ZipFile(pack_path, "r") as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            rows = [json.loads(line) for line in archive.read("content/memories.jsonl").decode("utf-8").splitlines() if line.strip()]
            members = {name: archive.read(name) for name in archive.namelist()}
        rows[0]["git_sha_at_write"] = "gitsha_phase3b"
        rows[0]["git_branch_at_write"] = "phase3b-branch"
        rows[0]["git_dirty_at_write"] = 1
        rows_blob = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in rows)
        members["content/memories.jsonl"] = rows_blob.encode("utf-8")
        covered = [str(name) for name in manifest["content_hash"]["covered_members"]]
        manifest["content_hash"]["value"] = self._recompute_pack_content_hash(members, covered)
        self._rewrite_zip(
            pack_path,
            tampered,
            replace_members={
                "content/memories.jsonl": rows_blob.encode("utf-8"),
                "manifest.json": (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
            },
        )
        imported = self._pack_import(pack_path=str(tampered), allow_unsigned_quarantine=True)
        memory_id = str(imported["structuredContent"]["imported_rows"][0]["memory_id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT git_sha, git_branch, git_dirty FROM memories WHERE id = ?", (memory_id,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(str(row["git_sha"]), "gitsha_phase3b")
        self.assertEqual(str(row["git_branch"]), "phase3b-branch")
        self.assertEqual(int(row["git_dirty"]), 1)

    def test_pack_import_imports_topics(self) -> None:
        memory = self.record("phase3b topic import", kind="context_block")
        add = server.topic_add({"memory_id": memory["id"], "topic": "phase3b-topic", "source": "operator"})
        self.assertFalse(add["isError"], add)
        pack_path = self._create_exported_pack(
            pack_name="phase3b_topics",
            output_dir=self.root / "phase3b_topics",
            topics=["phase3b-topic"],
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        memory_id = str(imported["structuredContent"]["imported_rows"][0]["memory_id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT topic, source FROM memory_topics WHERE memory_id = ? ORDER BY topic ASC",
                (memory_id,),
            ).fetchall()
        finally:
            conn.close()
        self.assertTrue(rows)
        self.assertIn(("phase3b-topic", "pack_import"), [(str(row["topic"]), str(row["source"])) for row in rows])

    def test_pack_import_imports_memory_files(self) -> None:
        tracked = self.workspace / "src" / "auth" / "session.py"
        tracked.parent.mkdir(parents=True, exist_ok=True)
        tracked.write_text("SESSION='phase3b-import-files'\n", encoding="utf-8")
        self.record(
            "phase3b file import marker",
            kind="context_block",
            touched_files=["src/auth/session.py"],
        )
        pack_path = self._create_exported_pack(
            pack_name="phase3b_memory_files",
            output_dir=self.root / "phase3b_memory_files",
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        imported_row = imported["structuredContent"]["imported_rows"][0]
        memory_id = str(imported_row["memory_id"])
        kind_name = str(imported_row["kind"])
        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT memory_table, memory_id, path, file_sha FROM memory_files WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(str(row["memory_table"]), kind_name)
        self.assertEqual(str(row["memory_id"]), memory_id)
        self.assertEqual(str(row["path"]), "src/auth/session.py")
        self.assertTrue(str(row["file_sha"]))

    def test_pack_import_imported_pack_rows_mapping(self) -> None:
        self.record("phase3b mapping row one", kind="context_block")
        self.record("phase3b mapping row two", kind="hippocampus_entry")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_mapping_rows",
            output_dir=self.root / "phase3b_mapping_rows",
            kinds=["context_block", "hippocampus_entry"],
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        pack_id = str(imported["structuredContent"]["pack_id"])
        imported_rows = imported["structuredContent"]["imported_rows"]
        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT row_id_in_pack, memory_id FROM imported_pack_rows WHERE pack_id = ? ORDER BY imported_at ASC, row_id_in_pack ASC",
                (pack_id,),
            ).fetchall()
        finally:
            conn.close()
        mapped_ids = {str(row["row_id_in_pack"]): str(row["memory_id"]) for row in rows}
        self.assertTrue(mapped_ids)
        for item in imported_rows:
            self.assertEqual(mapped_ids.get(str(item["row_id_in_pack"])), str(item["memory_id"]))

    def test_pack_import_rejects_reimport_same_pack(self) -> None:
        self.record("phase3b reimport same bytes", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_reimport_same",
            output_dir=self.root / "phase3b_reimport_same",
        )
        self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        before = {
            "memories": self._table_count("memories"),
            "topics": self._table_count("memory_topics"),
            "files": self._table_count("memory_files"),
            "maps": self._table_count("imported_pack_rows"),
        }
        second = self._pack_import_error(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        self.assertIn("pack_already_imported", second["content"][0]["text"])
        after = {
            "memories": self._table_count("memories"),
            "topics": self._table_count("memory_topics"),
            "files": self._table_count("memory_files"),
            "maps": self._table_count("imported_pack_rows"),
        }
        self.assertEqual(before, after)

    def test_pack_import_rejects_same_pack_id_distinct_content(self) -> None:
        self.record("phase3b pack id collision", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_collision_source",
            output_dir=self.root / "phase3b_collision_source",
        )
        self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        variant = self.root / "phase3b_collision_source" / "variant.zip"
        self._rewrite_zip(pack_path, variant, extra_members={"extra/collision.txt": b"phase3b"})
        failed = self._pack_import_error(pack_path=str(variant), allow_unsigned_quarantine=True)
        self.assertIn("pack_id_collision_distinct_content", failed["content"][0]["text"])

    def test_pack_import_rejects_invalid_pack(self) -> None:
        self.record("phase3b invalid pack base", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_invalid_pack",
            output_dir=self.root / "phase3b_invalid_pack",
        )
        tampered = self.root / "phase3b_invalid_pack" / "tampered_invalid.zip"
        self._rewrite_zip(pack_path, tampered, replace_members={"content/memories.jsonl": b"{not-json}\n"})
        before = self._table_count("imported_packs")
        failed = self._pack_import_error(pack_path=str(tampered), allow_unsigned_quarantine=True)
        self.assertIn("pack_validation_failed", failed["content"][0]["text"])
        self.assertEqual(before, self._table_count("imported_packs"))

    def test_pack_import_transaction_rollback_on_failure(self) -> None:
        self.record("phase3b rollback base", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_rollback",
            output_dir=self.root / "phase3b_rollback",
        )
        before = {
            "imported_packs": self._table_count("imported_packs"),
            "memories": self._table_count("memories"),
            "topics": self._table_count("memory_topics"),
            "files": self._table_count("memory_files"),
            "maps": self._table_count("imported_pack_rows"),
        }
        with mock.patch("server._sqlite_upsert_memory", side_effect=RuntimeError("forced insert failure")):
            failed = self._pack_import_error(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        self.assertIn("pack_import_failed", failed["content"][0]["text"])
        after = {
            "imported_packs": self._table_count("imported_packs"),
            "memories": self._table_count("memories"),
            "topics": self._table_count("memory_topics"),
            "files": self._table_count("memory_files"),
            "maps": self._table_count("imported_pack_rows"),
        }
        self.assertEqual(before, after)

    def test_pack_import_retrieval_quarantine_visibility(self) -> None:
        unique_text = "phase3b quarantine retrieval visibility marker"
        self.record(unique_text, kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_visibility",
            output_dir=self.root / "phase3b_visibility",
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        namespace = str(imported["structuredContent"]["namespace"])
        default = server.search_memories({"query": unique_text, "limit": 20})
        include_imported = server.search_memories({"query": unique_text, "limit": 20, "include_imported": True})
        include_quarantine = server.search_memories({"query": unique_text, "limit": 20, "include_quarantine": True})
        explicit_namespace = server.search_memories({"query": unique_text, "limit": 20, "namespace": namespace})
        imported_ids = {str(item["memory_id"]) for item in imported["structuredContent"]["imported_rows"]}
        default_ids = {str(item["id"]) for item in default["structuredContent"]["matches"]}
        include_imported_ids = {str(item["id"]) for item in include_imported["structuredContent"]["matches"]}
        include_quarantine_ids = {str(item["id"]) for item in include_quarantine["structuredContent"]["matches"]}
        explicit_ids = {str(item["id"]) for item in explicit_namespace["structuredContent"]["matches"]}
        self.assertFalse(imported_ids & default_ids)
        self.assertFalse(imported_ids & include_imported_ids)
        self.assertTrue(imported_ids & include_quarantine_ids)
        self.assertTrue(imported_ids & explicit_ids)

    def test_pack_import_retrieval_result_provenance_fields(self) -> None:
        marker = "phase3b retrieval provenance fields marker"
        self.record(marker, kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_retrieval_provenance",
            output_dir=self.root / "phase3b_retrieval_provenance",
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        imported_ids = {str(item["memory_id"]) for item in imported["structuredContent"]["imported_rows"]}
        result = server.search_memories({"query": marker, "limit": 20, "include_quarantine": True})
        matches = [item for item in result["structuredContent"]["matches"] if str(item.get("id")) in imported_ids]
        self.assertTrue(matches)
        match = matches[0]
        self.assertIn("namespace", match)
        self.assertIn("origin", match)
        self.assertIn("pack_id", match)
        self.assertIn("import_freshness", match)
        self.assertEqual(str(match["origin"]), "imported")

    def test_pack_import_freshness_verified_stale_missing_unknown(self) -> None:
        self.record("phase3b freshness verified", kind="context_block")
        self.record("phase3b freshness stale", kind="context_block")
        self.record("phase3b freshness missing", kind="context_block")
        self.record("phase3b freshness unknown", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_freshness_labels",
            output_dir=self.root / "phase3b_freshness_labels",
            kinds=["context_block"],
            limit=100,
        )
        tampered = self.root / "phase3b_freshness_labels" / "freshness.zip"
        with zipfile.ZipFile(pack_path, "r") as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            rows = [json.loads(line) for line in archive.read("content/memories.jsonl").decode("utf-8").splitlines() if line.strip()]
            members = {name: archive.read(name) for name in archive.namelist()}
        rows = rows[:4]
        rows[0]["touched_files"] = [{"path": "verified.txt", "file_sha": "sha_verified"}]
        rows[1]["touched_files"] = [{"path": "stale.txt", "file_sha": "sha_old"}]
        rows[2]["touched_files"] = [{"path": "missing.txt", "file_sha": "sha_missing"}]
        rows[3]["touched_files"] = [{"path": "unknown.txt", "file_sha": "sha_unknown"}]
        rows_blob = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in rows)
        members["content/memories.jsonl"] = rows_blob.encode("utf-8")
        manifest["selection"]["total_rows"] = 4
        manifest["selection"]["exported_rows"] = 4
        manifest["selection"]["limited"] = False
        manifest["counts"]["by_kind"] = {"context_block": 4}
        covered = [str(name) for name in manifest["content_hash"]["covered_members"]]
        manifest["content_hash"]["value"] = self._recompute_pack_content_hash(members, covered)
        self._rewrite_zip(
            pack_path,
            tampered,
            replace_members={
                "content/memories.jsonl": rows_blob.encode("utf-8"),
                "manifest.json": (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
            },
        )
        (self.workspace / "verified.txt").write_text("v\n", encoding="utf-8")
        (self.workspace / "stale.txt").write_text("s\n", encoding="utf-8")
        (self.workspace / "unknown.txt").write_text("u\n", encoding="utf-8")

        def fake_current_file_sha(_repo_root: str, rel_path: str) -> str | None:
            if rel_path == "verified.txt":
                return "sha_verified"
            if rel_path == "stale.txt":
                return "sha_current"
            if rel_path == "missing.txt":
                return None
            if rel_path == "unknown.txt":
                return None
            return None

        with mock.patch("server.current_file_sha", side_effect=fake_current_file_sha):
            imported = self._pack_import(pack_path=str(tampered), allow_unsigned_quarantine=True)
        freshness = imported["structuredContent"]["freshness"]
        self.assertEqual(int(freshness["by_file"]["verified"]), 1)
        self.assertEqual(int(freshness["by_file"]["stale"]), 1)
        self.assertEqual(int(freshness["by_file"]["missing"]), 1)
        self.assertEqual(int(freshness["by_file"]["unknown"]), 1)
        labels = {str(item["import_freshness"]) for item in imported["structuredContent"]["imported_rows"]}
        self.assertIn("verified", labels)
        self.assertIn("stale", labels)
        self.assertIn("missing", labels)
        self.assertIn("unknown", labels)

    def test_pack_import_does_not_mutate_exported_packs_or_aliases(self) -> None:
        self.record("phase3b mutation isolation", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_mutation_isolation",
            output_dir=self.root / "phase3b_mutation_isolation",
        )
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            alias_tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'alias_%' ORDER BY name"
                ).fetchall()
            ]
            before_alias = {
                name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]) for name in alias_tables
            }
            before_exported = int(conn.execute("SELECT COUNT(*) FROM exported_packs").fetchone()[0])
        finally:
            conn.close()
        self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            after_alias = {
                name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]) for name in before_alias
            }
            after_exported = int(conn.execute("SELECT COUNT(*) FROM exported_packs").fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(before_exported, after_exported)
        self.assertEqual(before_alias, after_alias)

    def test_pack_import_unknown_text_field_skipped_warning(self) -> None:
        self.record("phase3b unknown text field", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_unknown_text_field",
            output_dir=self.root / "phase3b_unknown_text_field",
        )
        tampered = self.root / "phase3b_unknown_text_field" / "unknown_field.zip"
        with zipfile.ZipFile(pack_path, "r") as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            rows = [json.loads(line) for line in archive.read("content/memories.jsonl").decode("utf-8").splitlines() if line.strip()]
            members = {name: archive.read(name) for name in archive.namelist()}
        rows[0]["text_fields"]["body"] = "ignored-body-field"
        rows_blob = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in rows)
        members["content/memories.jsonl"] = rows_blob.encode("utf-8")
        covered = [str(name) for name in manifest["content_hash"]["covered_members"]]
        manifest["content_hash"]["value"] = self._recompute_pack_content_hash(members, covered)
        self._rewrite_zip(
            pack_path,
            tampered,
            replace_members={
                "content/memories.jsonl": rows_blob.encode("utf-8"),
                "manifest.json": (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
            },
        )
        imported = self._pack_import(pack_path=str(tampered), allow_unsigned_quarantine=True)
        warning_codes = {
            str(item.get("code"))
            for item in imported["structuredContent"]["warnings"]
            if isinstance(item, dict) and item.get("code") is not None
        }
        self.assertIn("unknown_text_field_skipped", warning_codes)

    def test_pack_import_output_imported_rows_capped(self) -> None:
        for idx in range(105):
            self.record(f"phase3b capped output row {idx}", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_output_cap",
            output_dir=self.root / "phase3b_output_cap",
            kinds=["context_block"],
            limit=200,
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        output_rows = imported["structuredContent"]["imported_rows"]
        self.assertEqual(len(output_rows), 100)
        warning_codes = {
            str(item.get("code"))
            for item in imported["structuredContent"]["warnings"]
            if isinstance(item, dict) and item.get("code") is not None
        }
        self.assertIn("imported_rows_truncated", warning_codes)

    def test_pack_import_shared_validator_same_snapshot(self) -> None:
        marker = "phase3b snapshot same-bytes marker"
        self.record(marker, kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_snapshot_invariant",
            output_dir=self.root / "phase3b_snapshot_invariant",
        )
        snapshot = server._load_pack_snapshot(pack_path)
        original_sha = str(snapshot["received_zip_sha256"])
        pack_path.write_text("not a zip anymore", encoding="utf-8")
        with mock.patch("server._load_pack_snapshot", return_value=snapshot):
            imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        payload = imported["structuredContent"]
        self.assertEqual(str(payload["received_zip_sha256"]), original_sha)
        imported_id = str(payload["imported_rows"][0]["memory_id"])
        fetched = server.memory_get({"id": imported_id, "full": True})
        self.assertFalse(fetched["isError"], fetched)
        self.assertIn(marker, str(fetched["structuredContent"]["memory"]["text"]))

    def test_pack_import_namespace_trust_invariant(self) -> None:
        self.record("phase3b namespace trust invariant", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase3b_namespace_trust",
            output_dir=self.root / "phase3b_namespace_trust",
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        pack_id = str(imported["structuredContent"]["pack_id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT namespace, trust_level FROM imported_packs WHERE pack_id = ?", (pack_id,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(str(row["trust_level"]), "quarantine")
        self.assertTrue(str(row["namespace"]).startswith("pack:quarantine:"))

    def test_pack_list_imports_lists_imported_pack(self) -> None:
        self.record("phase4a list imports baseline", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase4a_list_imports",
            output_dir=self.root / "phase4a_list_imports",
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        pack_id = str(imported["structuredContent"]["pack_id"])
        result = self._pack_list_imports()
        payload = result["structuredContent"]
        self.assertEqual(payload["action"], "pack_list_imports")
        packs = [row for row in payload["packs"] if str(row.get("pack_id")) == pack_id]
        self.assertTrue(packs)
        row = packs[0]
        self.assertEqual(str(row["namespace"]), str(imported["structuredContent"]["namespace"]))
        self.assertEqual(str(row["trust_level"]), "quarantine")
        self.assertTrue(str(row["imported_at"]))

    def test_pack_list_imports_total_limit_semantics(self) -> None:
        for idx in range(2):
            self.record(f"phase4a list limit row {idx}", kind="context_block")
            pack_path = self._create_exported_pack(
                pack_name=f"phase4a_list_limit_{idx}",
                output_dir=self.root / f"phase4a_list_limit_{idx}",
            )
            self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        listed = self._pack_list_imports(limit=1)
        payload = listed["structuredContent"]
        self.assertGreaterEqual(int(payload["total"]), 2)
        self.assertLessEqual(len(payload["packs"]), 1)
        self.assertEqual(bool(payload["limited"]), bool(int(payload["total"]) > 1))

    def test_pack_list_imports_counts_and_freshness(self) -> None:
        tracked = self.workspace / "src" / "phase4a" / "list_counts.py"
        tracked.parent.mkdir(parents=True, exist_ok=True)
        tracked.write_text("print('phase4a')\n", encoding="utf-8")
        self.record(
            "phase4a list counts freshness",
            kind="context_block",
            touched_files=["src/phase4a/list_counts.py"],
        )
        pack_path = self._create_exported_pack(
            pack_name="phase4a_list_counts",
            output_dir=self.root / "phase4a_list_counts",
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        pack_id = str(imported["structuredContent"]["pack_id"])
        listed = self._pack_list_imports(pack_id=pack_id)
        payload = listed["structuredContent"]
        self.assertEqual(payload["total"], 1)
        row = payload["packs"][0]
        self.assertIn("memory_count", row)
        self.assertIn("topic_count", row)
        self.assertIn("memory_file_count", row)
        self.assertIn("freshness", row)
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            expected_files = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_files WHERE memory_id IN (SELECT memory_id FROM imported_pack_rows WHERE pack_id = ?)",
                    (pack_id,),
                ).fetchone()[0]
            )
        finally:
            conn.close()
        self.assertEqual(int(row["memory_file_count"]), expected_files)

    def test_pack_list_imports_top_topics_bound_and_order(self) -> None:
        memory_ids: list[str] = []
        for idx in range(12):
            row = self.record(f"phase4a topic bound row {idx}", kind="context_block")
            memory_ids.append(str(row["id"]))
        for idx, memory_id in enumerate(memory_ids):
            added = server.topic_add({"memory_id": memory_id, "topic": f"topic_{idx:02d}", "source": "operator"})
            self.assertFalse(added["isError"], added)
        pack_path = self._create_exported_pack(
            pack_name="phase4a_topics_bound",
            output_dir=self.root / "phase4a_topics_bound",
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        pack_id = str(imported["structuredContent"]["pack_id"])
        listed = self._pack_list_imports(pack_id=pack_id, include_topics=True)
        topics = listed["structuredContent"]["packs"][0]["top_topics"]
        self.assertLessEqual(len(topics), 10)
        ordered = sorted(topics, key=lambda item: (-int(item["row_count"]), str(item["topic"])))
        self.assertEqual(topics, ordered)

    def test_pack_list_imports_source_label_basename(self) -> None:
        self.record("phase4a source label basename", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase4a_source_label",
            output_dir=self.root / "phase4a_source_label",
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        pack_id = str(imported["structuredContent"]["pack_id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            conn.execute(
                "UPDATE imported_packs SET source_label = ? WHERE pack_id = ?",
                ("C:\\temp\\phase4a\\nested\\pack.zip", pack_id),
            )
            conn.commit()
        finally:
            conn.close()
        listed = self._pack_list_imports(pack_id=pack_id)
        row = listed["structuredContent"]["packs"][0]
        self.assertEqual(str(row["source_label"]), "pack.zip")

    def test_pack_review_import_basic(self) -> None:
        self.record("phase4a review basic one", kind="context_block")
        self.record("phase4a review basic two", kind="hippocampus_entry")
        pack_path = self._create_exported_pack(
            pack_name="phase4a_review_basic",
            output_dir=self.root / "phase4a_review_basic",
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        pack_id = str(imported["structuredContent"]["pack_id"])
        reviewed = self._pack_review_import(pack_id=pack_id)
        payload = reviewed["structuredContent"]
        self.assertEqual(payload["action"], "pack_review_import")
        self.assertEqual(str(payload["pack"]["pack_id"]), pack_id)
        self.assertGreater(int(payload["selection"]["total_pack_rows"]), 0)
        self.assertGreater(int(payload["selection"]["selected_rows"]), 0)
        self.assertIn("by_kind", payload["counts"])
        self.assertIn("by_import_freshness", payload["counts"])
        self.assertIn("top_referenced_files", payload["files"])
        self.assertIn("samples", payload)

    def test_pack_review_import_grouped_summary(self) -> None:
        first = self.record("phase213 grouped review alpha", kind="context_block", title="Grouped alpha")
        second = self.record("phase213 grouped review beta", kind="hippocampus_entry", title="Grouped beta")
        for memory_id in (str(first["id"]), str(second["id"])):
            added = server.topic_add({"memory_id": memory_id, "topic": "phase213-grouped-review", "source": "operator"})
            self.assertFalse(added["isError"], added)
        pack_path = self._create_exported_pack(
            pack_name="phase213_grouped_review",
            output_dir=self.root / "phase213_grouped_review",
            kinds=["context_block", "hippocampus_entry"],
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        reviewed = self._pack_review_import(
            pack_id=str(imported["structuredContent"]["pack_id"]),
            include_grouped_summary=True,
        )
        grouped = reviewed["structuredContent"]["grouped_summary"]
        self.assertIn("top_topic_groups", grouped)
        self.assertIn("freshness_counts", grouped)
        self.assertTrue(grouped["top_topic_groups"])

    def test_pack_review_import_unknown_pack(self) -> None:
        self.record("phase4a review unknown seed", kind="context_block")
        before = {
            "memories": self._table_count("memories"),
            "packs": self._table_count("imported_packs"),
            "maps": self._table_count("imported_pack_rows"),
            "topics": self._table_count("memory_topics"),
            "files": self._table_count("memory_files"),
            "exports": self._table_count("exported_packs"),
        }
        failed = self._pack_review_import_error(pack_id="missing-pack-phase4a")
        self.assertIn("pack_not_found", failed["content"][0]["text"])
        after = {
            "memories": self._table_count("memories"),
            "packs": self._table_count("imported_packs"),
            "maps": self._table_count("imported_pack_rows"),
            "topics": self._table_count("memory_topics"),
            "files": self._table_count("memory_files"),
            "exports": self._table_count("exported_packs"),
        }
        self.assertEqual(before, after)

    def test_pack_review_import_topic_filter_uses_memory_topics(self) -> None:
        body_only = self.record("phase4a topic auth in body only", kind="context_block")
        tagged = self.record("phase4a tagged row without keyword", kind="context_block")
        added = server.topic_add({"memory_id": tagged["id"], "topic": "auth", "source": "operator"})
        self.assertFalse(added["isError"], added)
        pack_path = self._create_exported_pack(
            pack_name="phase4a_review_topic_filter",
            output_dir=self.root / "phase4a_review_topic_filter",
            kinds=["context_block"],
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        reviewed = self._pack_review_import(
            pack_id=str(imported["structuredContent"]["pack_id"]),
            topics=["auth"],
            sample_limit=20,
        )
        payload = reviewed["structuredContent"]
        self.assertEqual(int(payload["selection"]["selected_rows"]), 1)
        self.assertEqual(len(payload["samples"]), 1)
        self.assertIn("auth", payload["samples"][0]["topics"])
        self.assertNotEqual(str(body_only["id"]), str(payload["samples"][0]["memory_id"]))

    def test_pack_review_import_kind_filter(self) -> None:
        self.record("phase4a kind filter context", kind="context_block")
        self.record("phase4a kind filter hip", kind="hippocampus_entry")
        pack_path = self._create_exported_pack(
            pack_name="phase4a_review_kind_filter",
            output_dir=self.root / "phase4a_review_kind_filter",
            kinds=["context_block", "hippocampus_entry"],
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        reviewed = self._pack_review_import(
            pack_id=str(imported["structuredContent"]["pack_id"]),
            kinds=["context_block"],
            sample_limit=50,
        )
        payload = reviewed["structuredContent"]
        self.assertGreater(int(payload["selection"]["selected_rows"]), 0)
        self.assertTrue(all(str(sample["kind"]) == "context_block" for sample in payload["samples"]))

    def test_pack_review_import_freshness_filter(self) -> None:
        self.record("phase4a freshness review a", kind="context_block")
        self.record("phase4a freshness review b", kind="context_block")
        self.record("phase4a freshness review c", kind="context_block")
        self.record("phase4a freshness review d", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase4a_review_freshness",
            output_dir=self.root / "phase4a_review_freshness",
            kinds=["context_block"],
        )
        tampered = self.root / "phase4a_review_freshness" / "freshness_pack.zip"
        with zipfile.ZipFile(pack_path, "r") as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            rows = [json.loads(line) for line in archive.read("content/memories.jsonl").decode("utf-8").splitlines() if line.strip()]
            members = {name: archive.read(name) for name in archive.namelist()}
        rows = rows[:4]
        rows[0]["touched_files"] = [{"path": "verified.txt", "file_sha": "sha_verified"}]
        rows[1]["touched_files"] = [{"path": "stale.txt", "file_sha": "sha_old"}]
        rows[2]["touched_files"] = [{"path": "missing.txt", "file_sha": "sha_missing"}]
        rows[3]["touched_files"] = [{"path": "unknown.txt", "file_sha": "sha_unknown"}]
        rows_blob = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in rows)
        members["content/memories.jsonl"] = rows_blob.encode("utf-8")
        manifest["selection"]["total_rows"] = 4
        manifest["selection"]["exported_rows"] = 4
        manifest["selection"]["limited"] = False
        manifest["counts"]["by_kind"] = {"context_block": 4}
        covered = [str(name) for name in manifest["content_hash"]["covered_members"]]
        manifest["content_hash"]["value"] = self._recompute_pack_content_hash(members, covered)
        self._rewrite_zip(
            pack_path,
            tampered,
            replace_members={
                "content/memories.jsonl": rows_blob.encode("utf-8"),
                "manifest.json": (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
            },
        )
        (self.workspace / "verified.txt").write_text("v\n", encoding="utf-8")
        (self.workspace / "stale.txt").write_text("s\n", encoding="utf-8")
        (self.workspace / "unknown.txt").write_text("u\n", encoding="utf-8")

        def fake_current_file_sha(_repo_root: str, rel_path: str) -> str | None:
            if rel_path == "verified.txt":
                return "sha_verified"
            if rel_path == "stale.txt":
                return "sha_current"
            if rel_path == "missing.txt":
                return None
            if rel_path == "unknown.txt":
                return None
            return None

        with mock.patch("server.current_file_sha", side_effect=fake_current_file_sha):
            imported = self._pack_import(pack_path=str(tampered), allow_unsigned_quarantine=True)
        reviewed = self._pack_review_import(
            pack_id=str(imported["structuredContent"]["pack_id"]),
            import_freshness=["stale"],
        )
        payload = reviewed["structuredContent"]
        self.assertEqual(int(payload["selection"]["selected_rows"]), 1)
        self.assertEqual(int(payload["counts"]["by_import_freshness"]["stale"]), 1)
        self.assertEqual(str(payload["samples"][0]["import_freshness"]), "stale")

    def test_pack_review_import_touched_paths_filter(self) -> None:
        auth_file = self.workspace / "src" / "auth" / "session.py"
        billing_file = self.workspace / "src" / "billing" / "pay.py"
        auth_file.parent.mkdir(parents=True, exist_ok=True)
        billing_file.parent.mkdir(parents=True, exist_ok=True)
        auth_file.write_text("phase4a\n", encoding="utf-8")
        billing_file.write_text("phase4a\n", encoding="utf-8")
        self.record("phase4a touched auth", kind="context_block", touched_files=["src/auth/session.py"])
        self.record("phase4a touched billing", kind="context_block", touched_files=["src/billing/pay.py"])
        pack_path = self._create_exported_pack(
            pack_name="phase4a_review_touched_paths",
            output_dir=self.root / "phase4a_review_touched_paths",
            kinds=["context_block"],
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        reviewed = self._pack_review_import(
            pack_id=str(imported["structuredContent"]["pack_id"]),
            touched_paths=["src/auth/session.py"],
            sample_limit=20,
        )
        payload = reviewed["structuredContent"]
        self.assertGreater(int(payload["selection"]["selected_rows"]), 0)
        self.assertTrue(
            all(
                any(str(item.get("path")) == "src/auth/session.py" for item in sample.get("touched_files", []))
                for sample in payload["samples"]
            )
        )

    def test_pack_review_import_query_filter_optional(self) -> None:
        self.record("PHASE4A text query marker", kind="context_block")
        self.record("phase4a plain row", kind="context_block", title="Phase4A Title Marker")
        pack_path = self._create_exported_pack(
            pack_name="phase4a_review_query",
            output_dir=self.root / "phase4a_review_query",
            kinds=["context_block"],
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        pack_id = str(imported["structuredContent"]["pack_id"])
        text_match = self._pack_review_import(pack_id=pack_id, query="text query marker", sample_limit=20)
        self.assertGreaterEqual(int(text_match["structuredContent"]["selection"]["selected_rows"]), 1)
        title_match = self._pack_review_import(pack_id=pack_id, query="title marker", sample_limit=20)
        self.assertGreaterEqual(int(title_match["structuredContent"]["selection"]["selected_rows"]), 1)
        self.assertFalse(
            any(
                str(item.get("code")) == "unsupported_filter_query"
                for item in title_match["structuredContent"]["warnings"]
                if isinstance(item, dict)
            )
        )

    def test_pack_review_import_memory_ids_outside_pack_warning(self) -> None:
        self.record("phase4a memory ids in-pack", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase4a_review_memory_ids",
            output_dir=self.root / "phase4a_review_memory_ids",
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        imported_id = str(imported["structuredContent"]["imported_rows"][0]["memory_id"])
        outside = self.record("phase4a memory id outside pack", kind="context_block")
        reviewed = self._pack_review_import(
            pack_id=str(imported["structuredContent"]["pack_id"]),
            memory_ids=[imported_id, str(outside["id"])],
        )
        warning_codes = {
            str(item.get("code"))
            for item in reviewed["structuredContent"]["warnings"]
            if isinstance(item, dict) and item.get("code") is not None
        }
        self.assertIn("memory_ids_outside_pack_filtered", warning_codes)
        self.assertEqual(int(reviewed["structuredContent"]["selection"]["selected_rows"]), 1)

    def test_pack_review_import_samples_bounded_no_source_ids(self) -> None:
        for idx in range(4):
            self.record(f"phase4a review bounded sample {idx}", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase4a_review_sample_bound",
            output_dir=self.root / "phase4a_review_sample_bound",
            kinds=["context_block"],
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        reviewed = self._pack_review_import(
            pack_id=str(imported["structuredContent"]["pack_id"]),
            include_samples=True,
            sample_limit=2,
        )
        samples = reviewed["structuredContent"]["samples"]
        self.assertLessEqual(len(samples), 2)
        self.assertTrue(all(not str(sample["row_id_in_pack"]).startswith("mem_") for sample in samples))

    def test_pack_promote_preview_basic(self) -> None:
        self.record("phase4a promote preview basic", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase4a_promote_basic",
            output_dir=self.root / "phase4a_promote_basic",
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        before_promoted = 0
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            before_promoted = int(
                conn.execute("SELECT COUNT(*) FROM memories WHERE namespace = ? AND origin = ?", ("local", "promoted")).fetchone()[0]
            )
        finally:
            conn.close()
        preview = self._pack_promote_preview(pack_id=str(imported["structuredContent"]["pack_id"]))
        payload = preview["structuredContent"]
        self.assertEqual(str(payload["promotion_plan"]["target_namespace"]), "local")
        self.assertEqual(str(payload["promotion_plan"]["target_origin"]), "promoted")
        self.assertEqual(
            int(payload["promotion_plan"]["would_create_memory_count"]),
            min(int(payload["selection"]["selected_rows"]), int(payload["selection"]["limit"])),
        )
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            after_promoted = int(
                conn.execute("SELECT COUNT(*) FROM memories WHERE namespace = ? AND origin = ?", ("local", "promoted")).fetchone()[0]
            )
        finally:
            conn.close()
        self.assertEqual(before_promoted, after_promoted)

    def test_pack_promote_preview_unknown_pack(self) -> None:
        self.record("phase4a promote unknown seed", kind="context_block")
        before = {
            "memories": self._table_count("memories"),
            "packs": self._table_count("imported_packs"),
            "maps": self._table_count("imported_pack_rows"),
            "topics": self._table_count("memory_topics"),
            "files": self._table_count("memory_files"),
            "exports": self._table_count("exported_packs"),
        }
        failed = self._pack_promote_preview_error(pack_id="missing-pack-phase4a")
        self.assertIn("pack_not_found", failed["content"][0]["text"])
        after = {
            "memories": self._table_count("memories"),
            "packs": self._table_count("imported_packs"),
            "maps": self._table_count("imported_pack_rows"),
            "topics": self._table_count("memory_topics"),
            "files": self._table_count("memory_files"),
            "exports": self._table_count("exported_packs"),
        }
        self.assertEqual(before, after)

    def test_pack_promote_preview_filters_reuse_review_selection(self) -> None:
        a = self.record("phase4a reuse review sel a", kind="context_block")
        b = self.record("phase4a reuse review sel b", kind="context_block")
        self.record("phase4a reuse review sel c", kind="hippocampus_entry")
        server.topic_add({"memory_id": a["id"], "topic": "reuse-topic", "source": "operator"})
        server.topic_add({"memory_id": b["id"], "topic": "reuse-topic", "source": "operator"})
        pack_path = self._create_exported_pack(
            pack_name="phase4a_reuse_selection",
            output_dir=self.root / "phase4a_reuse_selection",
            kinds=["context_block", "hippocampus_entry"],
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        pack_id = str(imported["structuredContent"]["pack_id"])
        review = self._pack_review_import(
            pack_id=pack_id,
            topics=["reuse-topic"],
            kinds=["context_block"],
            sample_limit=50,
            limit=50,
        )
        preview = self._pack_promote_preview(
            pack_id=pack_id,
            topics=["reuse-topic"],
            kinds=["context_block"],
            sample_limit=50,
            limit=50,
        )
        review_rows = [str(item["row_id_in_pack"]) for item in review["structuredContent"]["samples"]]
        preview_rows = [str(item["row_id_in_pack"]) for item in preview["structuredContent"]["candidate_rows"]]
        self.assertEqual(set(review_rows), set(preview_rows))
        self.assertEqual(review_rows, preview_rows)

    def test_pack_promote_preview_provenance_plan(self) -> None:
        self.record("phase4a provenance plan", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase4a_promote_provenance",
            output_dir=self.root / "phase4a_promote_provenance",
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        pack_id = str(imported["structuredContent"]["pack_id"])
        preview = self._pack_promote_preview(pack_id=pack_id, sample_limit=20)
        rows = preview["structuredContent"]["candidate_rows"]
        self.assertTrue(rows)
        for row in rows:
            provenance = row["provenance"]
            self.assertEqual(str(provenance["promoted_from_pack_id"]), pack_id)
            self.assertEqual(str(provenance["promoted_from_row_id_in_pack"]), str(row["row_id_in_pack"]))
            self.assertEqual(str(provenance["promoted_from_imported_memory_id"]), str(row["imported_memory_id"]))
            self.assertEqual(str(provenance["original_import_freshness"]), str(row["import_freshness"]))

    def test_pack_promote_preview_candidate_git_fields(self) -> None:
        self.record("phase4a candidate git fields", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase4a_promote_git_fields",
            output_dir=self.root / "phase4a_promote_git_fields",
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        preview = self._pack_promote_preview(pack_id=str(imported["structuredContent"]["pack_id"]))
        for row in preview["structuredContent"]["candidate_rows"]:
            self.assertIn("git_sha", row)
            self.assertIn("git_branch", row)
            self.assertIn("git_dirty", row)

    def test_pack_promote_preview_no_filters_warns(self) -> None:
        self.record("phase4a promote no filter warn", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase4a_promote_no_filters",
            output_dir=self.root / "phase4a_promote_no_filters",
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        preview = self._pack_promote_preview(pack_id=str(imported["structuredContent"]["pack_id"]))
        warning_codes = {
            str(item.get("code"))
            for item in preview["structuredContent"]["warnings"]
            if isinstance(item, dict) and item.get("code") is not None
        }
        self.assertIn("preview_all_pack_rows", warning_codes)

    def test_pack_promote_preview_limited_warning(self) -> None:
        for idx in range(5):
            self.record(f"phase4a promote limited {idx}", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase4a_promote_limited",
            output_dir=self.root / "phase4a_promote_limited",
            kinds=["context_block"],
            limit=200,
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        preview = self._pack_promote_preview(
            pack_id=str(imported["structuredContent"]["pack_id"]),
            kinds=["context_block"],
            limit=2,
        )
        self.assertTrue(bool(preview["structuredContent"]["selection"]["limited"]))
        warning_codes = {
            str(item.get("code"))
            for item in preview["structuredContent"]["warnings"]
            if isinstance(item, dict) and item.get("code") is not None
        }
        self.assertIn("promotion_preview_limited", warning_codes)

    def test_pack_promote_preview_rejects_non_quarantine(self) -> None:
        self._insert_imported_pack_unchecked(
            pack_id="phase4a-invalid-pack",
            trust_level="legacy-invalid",
            namespace="pack:legacy:phase4a-invalid-pack",
        )
        failed = self._pack_promote_preview_error(pack_id="phase4a-invalid-pack")
        self.assertIn("unsupported_trust_level_for_promotion_preview", failed["content"][0]["text"])

    def test_pack_review_actions_read_only(self) -> None:
        self.record("phase4a read-only check seed", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase4a_read_only_actions",
            output_dir=self.root / "phase4a_read_only_actions",
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        pack_id = str(imported["structuredContent"]["pack_id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            alias_tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'alias_%' ORDER BY name"
                ).fetchall()
            ]
            fts_tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'memories_fts%' ORDER BY name"
                ).fetchall()
            ]
            before = {
                "memories": self._table_count("memories"),
                "imported_packs": self._table_count("imported_packs"),
                "imported_pack_rows": self._table_count("imported_pack_rows"),
                "memory_topics": self._table_count("memory_topics"),
                "memory_files": self._table_count("memory_files"),
                "exported_packs": self._table_count("exported_packs"),
                "alias": {name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]) for name in alias_tables},
                "fts": {name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]) for name in fts_tables},
            }
        finally:
            conn.close()
        self._pack_list_imports(pack_id=pack_id)
        self._pack_review_import(pack_id=pack_id)
        self._pack_promote_preview(pack_id=pack_id)
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            after = {
                "memories": self._table_count("memories"),
                "imported_packs": self._table_count("imported_packs"),
                "imported_pack_rows": self._table_count("imported_pack_rows"),
                "memory_topics": self._table_count("memory_topics"),
                "memory_files": self._table_count("memory_files"),
                "exported_packs": self._table_count("exported_packs"),
                "alias": {name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]) for name in before["alias"]},
                "fts": {name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]) for name in before["fts"]},
            }
        finally:
            conn.close()
        self.assertEqual(before, after)

    def test_pack_promote_preview_does_not_allocate_real_ids(self) -> None:
        self.record("phase4a no allocation seed", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase4a_no_allocation",
            output_dir=self.root / "phase4a_no_allocation",
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            before_ids = {
                str(row[0]) for row in conn.execute("SELECT id FROM memories").fetchall()
            }
        finally:
            conn.close()
        preview = self._pack_promote_preview(pack_id=str(imported["structuredContent"]["pack_id"]))
        self.assertTrue(all(bool(row["would_generate_memory_id"]) for row in preview["structuredContent"]["candidate_rows"]))
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            after_ids = {
                str(row[0]) for row in conn.execute("SELECT id FROM memories").fetchall()
            }
        finally:
            conn.close()
        self.assertEqual(before_ids, after_ids)

    def test_pack_review_row_id_natural_ordering(self) -> None:
        for idx in range(4):
            self.record(f"phase4a natural ordering row {idx}", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase4a_rowid_natural",
            output_dir=self.root / "phase4a_rowid_natural",
            kinds=["context_block"],
            limit=200,
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        pack_id = str(imported["structuredContent"]["pack_id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            rows = conn.execute(
                "SELECT row_id_in_pack FROM imported_pack_rows WHERE pack_id = ? ORDER BY row_id_in_pack ASC",
                (pack_id,),
            ).fetchall()
            renamed = ["ctx_2", "ctx_10", "ctx_999", "ctx_1000"]
            for idx, row in enumerate(rows[:4]):
                conn.execute(
                    "UPDATE imported_pack_rows SET row_id_in_pack = ? WHERE pack_id = ? AND row_id_in_pack = ?",
                    (renamed[idx], pack_id, str(row[0])),
                )
            conn.commit()
        finally:
            conn.close()
        reviewed = self._pack_review_import(
            pack_id=pack_id,
            kinds=["context_block"],
            include_samples=True,
            sample_limit=20,
            limit=20,
        )
        row_ids = [str(sample["row_id_in_pack"]) for sample in reviewed["structuredContent"]["samples"]]
        self.assertEqual(row_ids[:4], ["ctx_2", "ctx_10", "ctx_999", "ctx_1000"])

    def test_pack_promote_requires_confirm(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_requires_confirm", rows=2)
        row_id = self._pack_rows(pack_id)[0][0]
        failed = self._pack_promote_error(pack_id=pack_id, row_ids=[row_id])
        self.assertEqual(self._pack_error_code(failed), "confirm_promote_required")
        self.assertEqual(self._table_count("promoted_pack_rows"), 0)
        self.assertEqual(self._table_count("promotion_audit"), 0)

    def test_pack_promote_requires_filter_or_allow_all(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_requires_filter", rows=2)
        failed = self._pack_promote_error(pack_id=pack_id, confirm_promote=True)
        self.assertEqual(self._pack_error_code(failed), "promote_all_requires_explicit_allow")
        self.assertEqual(self._table_count("promoted_pack_rows"), 0)
        self.assertEqual(self._table_count("promotion_audit"), 0)

    def test_pack_promote_explicit_row_ids_success(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_row_ids_success", rows=3)
        row_id = self._pack_rows(pack_id)[0][0]
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        payload = promoted["structuredContent"]
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(int(payload["selection"]["promoted_rows"]), 1)
        self.assertTrue(str(payload["promotion_id"]).startswith("promotion_"))

    def test_pack_promote_creates_promotion_audit_row(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_audit_row", rows=2)
        row_id = self._pack_rows(pack_id)[0][0]
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True, limit=17)
        payload = promoted["structuredContent"]
        promotion_id = str(payload["promotion_id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            row = conn.execute(
                """
                SELECT promotion_id, pack_id, filters_json, row_count, limited, allow_promote_all, allow_limited_promotion
                FROM promotion_audit
                WHERE promotion_id = ?
                """,
                (promotion_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(str(row[0]), promotion_id)
        self.assertEqual(str(row[1]), pack_id)
        parsed = json.loads(str(row[2]))
        self.assertEqual(parsed["row_ids"], [row_id])
        self.assertEqual(int(row[3]), 1)
        self.assertEqual(int(row[4]), 0)
        self.assertEqual(int(row[5]), 0)
        self.assertEqual(int(row[6]), 0)

    def test_pack_promote_creates_new_local_ids(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_new_ids", rows=1)
        row_id, imported_memory_id, _kind = self._pack_rows(pack_id)[0]
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        row = promoted["structuredContent"]["promoted_rows"][0]
        promoted_memory_id = str(row["promoted_memory_id"])
        self.assertTrue(promoted_memory_id.startswith("mem_"))
        self.assertNotEqual(promoted_memory_id, imported_memory_id)
        self.assertNotEqual(promoted_memory_id, row_id)

    def test_pack_promote_sets_namespace_origin(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_namespace_origin", rows=1)
        row_id = self._pack_rows(pack_id)[0][0]
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        promoted_memory_id = str(promoted["structuredContent"]["promoted_rows"][0]["promoted_memory_id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            row = conn.execute("SELECT namespace, origin FROM memories WHERE id = ?", (promoted_memory_id,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(str(row[0]), "local")
        self.assertEqual(str(row[1]), "promoted")

    def test_pack_promote_preserves_text_kind_git_freshness(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_preserve_core", rows=1)
        row_id, imported_memory_id, _kind = self._pack_rows(pack_id)[0]
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        promoted_memory_id = str(promoted["structuredContent"]["promoted_rows"][0]["promoted_memory_id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            imported_row = conn.execute(
                "SELECT kind, text, title, git_sha, git_branch, git_dirty, import_freshness FROM memories WHERE id = ?",
                (imported_memory_id,),
            ).fetchone()
            promoted_row = conn.execute(
                "SELECT kind, text, title, git_sha, git_branch, git_dirty, import_freshness FROM memories WHERE id = ?",
                (promoted_memory_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(str(promoted_row[0]), str(imported_row[0]))
        self.assertEqual(str(promoted_row[1]), str(imported_row[1]))
        self.assertEqual(promoted_row[2], imported_row[2])
        self.assertEqual(promoted_row[3], imported_row[3])
        self.assertEqual(promoted_row[4], imported_row[4])
        self.assertEqual(promoted_row[5], imported_row[5])
        self.assertEqual(promoted_row[6], imported_row[6])

    def test_pack_promote_copies_topics_with_source_promotion(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_topics_copy", rows=1)
        row_id, imported_memory_id, _kind = self._pack_rows(pack_id)[0]
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        promoted_memory_id = str(promoted["structuredContent"]["promoted_rows"][0]["promoted_memory_id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            imported_topics = [
                (str(row[0]), str(row[1]))
                for row in conn.execute(
                    "SELECT topic, source FROM memory_topics WHERE memory_id = ? ORDER BY topic ASC",
                    (imported_memory_id,),
                ).fetchall()
            ]
            promoted_topics = [
                (str(row[0]), str(row[1]))
                for row in conn.execute(
                    "SELECT topic, source FROM memory_topics WHERE memory_id = ? ORDER BY topic ASC",
                    (promoted_memory_id,),
                ).fetchall()
            ]
        finally:
            conn.close()
        self.assertEqual([topic for topic, _source in promoted_topics], [topic for topic, _source in imported_topics])
        self.assertTrue(all(source == "promotion" for _topic, source in promoted_topics))

    def test_pack_promote_copies_memory_files(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_files_copy", rows=1)
        row_id, imported_memory_id, kind_name = self._pack_rows(pack_id)[0]
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        promoted_memory_id = str(promoted["structuredContent"]["promoted_rows"][0]["promoted_memory_id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            imported_files = {
                (str(row[0]), str(row[1]), str(row[2]))
                for row in conn.execute(
                    "SELECT memory_table, path, file_sha FROM memory_files WHERE memory_id = ? ORDER BY path ASC",
                    (imported_memory_id,),
                ).fetchall()
            }
            promoted_files = {
                (str(row[0]), str(row[1]), str(row[2]))
                for row in conn.execute(
                    "SELECT memory_table, path, file_sha FROM memory_files WHERE memory_id = ? ORDER BY path ASC",
                    (promoted_memory_id,),
                ).fetchall()
            }
        finally:
            conn.close()
        self.assertTrue(imported_files)
        self.assertEqual(
            {(kind_name, path, file_sha) for _tbl, path, file_sha in imported_files},
            promoted_files,
        )

    def test_pack_promote_promoted_pack_rows_mapping(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_mapping", rows=1)
        row_id, imported_memory_id, kind_name = self._pack_rows(pack_id)[0]
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        payload = promoted["structuredContent"]
        promoted_memory_id = str(payload["promoted_rows"][0]["promoted_memory_id"])
        promotion_id = str(payload["promotion_id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            mapped = conn.execute(
                """
                SELECT imported_memory_id, promoted_memory_id, kind, original_import_freshness, promotion_id
                FROM promoted_pack_rows
                WHERE pack_id = ? AND row_id_in_pack = ?
                """,
                (pack_id, row_id),
            ).fetchone()
            audit = conn.execute(
                "SELECT promotion_id FROM promotion_audit WHERE promotion_id = ?",
                (promotion_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(mapped)
        self.assertEqual(str(mapped[0]), imported_memory_id)
        self.assertEqual(str(mapped[1]), promoted_memory_id)
        self.assertEqual(str(mapped[2]), kind_name)
        self.assertIn(str(mapped[3] or "unknown"), {"verified", "stale", "missing", "unknown"})
        self.assertEqual(str(mapped[4]), promotion_id)
        self.assertEqual(str(audit[0]), promotion_id)

    def test_pack_promote_rejects_duplicate_promotion(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_duplicate_reject", rows=1)
        row_id = self._pack_rows(pack_id)[0][0]
        self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        before = {
            "memories": self._table_count("memories"),
            "topics": self._table_count("memory_topics"),
            "files": self._table_count("memory_files"),
            "maps": self._table_count("promoted_pack_rows"),
            "audit": self._table_count("promotion_audit"),
        }
        failed = self._pack_promote_error(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        self.assertEqual(self._pack_error_code(failed), "pack_rows_already_promoted")
        after = {
            "memories": self._table_count("memories"),
            "topics": self._table_count("memory_topics"),
            "files": self._table_count("memory_files"),
            "maps": self._table_count("promoted_pack_rows"),
            "audit": self._table_count("promotion_audit"),
        }
        self.assertEqual(before, after)

    def test_pack_promote_transaction_rollback_on_failure(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_rollback", rows=2)
        row_ids = [row_id for row_id, _memory_id, _kind in self._pack_rows(pack_id)]
        before = {
            "promoted_memories": self._table_count("memories"),
            "promoted_maps": self._table_count("promoted_pack_rows"),
            "promotion_audit": self._table_count("promotion_audit"),
        }
        with mock.patch.object(server, "make_id", return_value="mem_phase4b_fixed_id"):
            failed = self._pack_promote_error(pack_id=pack_id, row_ids=row_ids, confirm_promote=True)
        self.assertIn(
            self._pack_error_code(failed),
            {"pack_promote_integrity_error", "pack_promote_failed"},
        )
        after = {
            "promoted_memories": self._table_count("memories"),
            "promoted_maps": self._table_count("promoted_pack_rows"),
            "promotion_audit": self._table_count("promotion_audit"),
        }
        self.assertEqual(before, after)

    def test_pack_promote_selection_matches_preview_matrix(self) -> None:
        matrix = [
            {"topics": ["phase4b-sel-topic-00"], "import_freshness": ["from_row_0"]},
            {"kinds": ["context_block"], "touched_paths": ["src/phase4b/auth.py"]},
            {"memory_ids": "first_two"},
            {"row_ids": "first_two", "kinds": ["context_block"]},
        ]
        for idx, filter_payload in enumerate(matrix):
            marker = f"phase4b_selection_matrix_{idx}"
            pack_id, _imported_sc = self._create_phase4b_imported_pack(marker=marker, rows=4)
            rows = self._pack_rows(pack_id)
            conn = sqlite3.connect(str(self.sqlite_file))
            try:
                if idx == 0:
                    first_memory_id = rows[0][1]
                    conn.execute("UPDATE memory_topics SET topic = ? WHERE memory_id = ?", ("phase4b-sel-topic-00", first_memory_id))
                    freshness = conn.execute(
                        "SELECT COALESCE(NULLIF(import_freshness, ''), 'unknown') FROM memories WHERE id = ?",
                        (first_memory_id,),
                    ).fetchone()
                    filter_payload = {
                        "topics": ["phase4b-sel-topic-00"],
                        "import_freshness": [str(freshness[0] if freshness else "unknown")],
                    }
                    conn.commit()
                if idx == 2:
                    filter_payload = {"memory_ids": [rows[0][1], rows[1][1]]}
                if idx == 3:
                    filter_payload = {"row_ids": [rows[0][0], rows[1][0]], "kinds": ["context_block"]}
            finally:
                conn.close()
            preview = self._pack_promote_preview(
                pack_id=pack_id,
                include_samples=False,
                sample_limit=50,
                limit=100,
                **filter_payload,
            )
            preview_rows = [str(item["row_id_in_pack"]) for item in preview["structuredContent"]["candidate_rows"]]
            promoted = self._pack_promote(
                pack_id=pack_id,
                confirm_promote=True,
                limit=100,
                **filter_payload,
            )
            promoted_rows = [str(item["row_id_in_pack"]) for item in promoted["structuredContent"]["promoted_rows"]]
            self.assertEqual(preview_rows, promoted_rows)

    def test_pack_promote_limited_requires_override(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_limited_guard", rows=6)
        failed = self._pack_promote_error(
            pack_id=pack_id,
            kinds=["context_block", "hippocampus_entry"],
            limit=2,
            confirm_promote=True,
        )
        self.assertEqual(self._pack_error_code(failed), "limited_promotion_requires_explicit_allow")
        promoted = self._pack_promote(
            pack_id=pack_id,
            kinds=["context_block", "hippocampus_entry"],
            limit=2,
            allow_limited_promotion=True,
            confirm_promote=True,
        )
        self.assertIn("limited_promotion", self._pack_warning_codes(promoted))
        promotion_id = str(promoted["structuredContent"]["promotion_id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            limited_flag = int(
                conn.execute("SELECT limited FROM promotion_audit WHERE promotion_id = ?", (promotion_id,)).fetchone()[0]
            )
        finally:
            conn.close()
        self.assertEqual(limited_flag, 1)

    def test_pack_promote_query_filter_rejected(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_query_reject", rows=2)
        before = self._table_count("promoted_pack_rows")
        failed = self._pack_promote_error(
            pack_id=pack_id,
            query="should fail",
            row_ids=[self._pack_rows(pack_id)[0][0]],
            confirm_promote=True,
        )
        self.assertEqual(self._pack_error_code(failed), "query_filter_not_allowed_for_promotion")
        self.assertEqual(self._table_count("promoted_pack_rows"), before)

    def test_pack_promote_rejects_non_quarantine(self) -> None:
        self._insert_imported_pack_unchecked(
            pack_id="phase4b-invalid-pack",
            trust_level="legacy-invalid",
            namespace="pack:legacy:phase4b-invalid-pack",
        )
        failed = self._pack_promote_error(
            pack_id="phase4b-invalid-pack",
            allow_promote_all=True,
            confirm_promote=True,
        )
        self.assertEqual(self._pack_error_code(failed), "unsupported_trust_level_for_promotion")

    def test_pack_promote_retrieval_visibility(self) -> None:
        marker = "phase4b_retrieval_visibility_unique_marker"
        pack_id, imported_sc = self._create_phase4b_imported_pack(marker=marker, rows=1, kinds=["context_block"])
        row_id, imported_memory_id, _kind = self._pack_rows(pack_id)[0]
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        promoted_memory_id = str(promoted["structuredContent"]["promoted_rows"][0]["promoted_memory_id"])
        namespace = str(imported_sc["namespace"])
        default = server.search_memories({"query": marker, "limit": 50})
        include_quarantine = server.search_memories({"query": marker, "limit": 50, "include_quarantine": True})
        origin_promoted = server.search_memories({"query": marker, "limit": 50, "origin": "promoted"})
        origins_promoted = server.search_memories({"query": marker, "limit": 50, "origins": ["promoted"]})
        default_ids = {str(item["id"]) for item in default["structuredContent"]["matches"]}
        quarantine_ids = {str(item["id"]) for item in include_quarantine["structuredContent"]["matches"]}
        promoted_ids = {str(item["id"]) for item in origin_promoted["structuredContent"]["matches"]}
        promoted_ids_v2 = {str(item["id"]) for item in origins_promoted["structuredContent"]["matches"]}
        self.assertIn(promoted_memory_id, default_ids)
        self.assertNotIn(imported_memory_id, default_ids)
        self.assertIn(imported_memory_id, quarantine_ids)
        self.assertIn(promoted_memory_id, promoted_ids)
        self.assertIn(promoted_memory_id, promoted_ids_v2)
        explicit_quarantine = server.search_memories({"query": marker, "limit": 50, "namespace": namespace})
        explicit_ids = {str(item["id"]) for item in explicit_quarantine["structuredContent"]["matches"]}
        self.assertIn(imported_memory_id, explicit_ids)

    def test_pack_promote_does_not_mutate_quarantine_or_pack_tables(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_non_mutating_pack_tables", rows=2)
        row_id, imported_memory_id, _kind = self._pack_rows(pack_id)[0]
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            alias_tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'alias_%' ORDER BY name"
                ).fetchall()
            ]
            before = {
                "imported_packs": self._table_count("imported_packs"),
                "imported_pack_rows": self._table_count("imported_pack_rows"),
                "exported_packs": self._table_count("exported_packs"),
                "aliases": {name: self._table_count(name) for name in alias_tables},
                "imported_row_exists": int(
                    conn.execute("SELECT COUNT(*) FROM memories WHERE id = ? AND origin = 'imported'", (imported_memory_id,)).fetchone()[0]
                ),
            }
        finally:
            conn.close()
        self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            after = {
                "imported_packs": self._table_count("imported_packs"),
                "imported_pack_rows": self._table_count("imported_pack_rows"),
                "exported_packs": self._table_count("exported_packs"),
                "aliases": {name: self._table_count(name) for name in before["aliases"]},
                "imported_row_exists": int(
                    conn.execute("SELECT COUNT(*) FROM memories WHERE id = ? AND origin = 'imported'", (imported_memory_id,)).fetchone()[0]
                ),
            }
        finally:
            conn.close()
        self.assertEqual(before, after)

    def test_pack_promote_output_capped(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(
            marker="phase4b_output_capped",
            rows=120,
            kinds=["context_block"],
        )
        promoted = self._pack_promote(
            pack_id=pack_id,
            kinds=["context_block"],
            allow_promote_all=True,
            confirm_promote=True,
            limit=500,
        )
        payload = promoted["structuredContent"]
        self.assertGreater(int(payload["selection"]["promoted_rows"]), 100)
        self.assertEqual(len(payload["promoted_rows"]), 100)
        self.assertIn("promoted_rows_truncated", self._pack_warning_codes(promoted))

    def test_pack_promote_memory_ids_outside_pack_warning(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_outside_pack_a", rows=2)
        other_pack_id, _other_sc = self._create_phase4b_imported_pack(marker="phase4b_outside_pack_b", rows=1)
        valid_memory_id = self._pack_rows(pack_id)[0][1]
        other_memory_id = self._pack_rows(other_pack_id)[0][1]
        outside_local = self.record("phase4b outside local id", kind="context_block")
        promoted = self._pack_promote(
            pack_id=pack_id,
            memory_ids=[valid_memory_id, str(outside_local["id"]), other_memory_id],
            confirm_promote=True,
        )
        self.assertIn("memory_ids_outside_pack_filtered", self._pack_warning_codes(promoted))
        self.assertEqual(int(promoted["structuredContent"]["selection"]["promoted_rows"]), 1)

    def test_pack_promote_memory_ids_outside_pack_empty_selection_warning(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_outside_only_a", rows=1)
        other_pack_id, _other_sc = self._create_phase4b_imported_pack(marker="phase4b_outside_only_b", rows=1)
        other_memory_id = self._pack_rows(other_pack_id)[0][1]
        outside_local = self.record("phase4b outside-only local id", kind="context_block")
        failed = self._pack_promote_error(
            pack_id=pack_id,
            memory_ids=[str(outside_local["id"]), other_memory_id],
            confirm_promote=True,
        )
        self.assertEqual(self._pack_error_code(failed), "selected_rows_empty")
        self.assertIn("memory_ids_outside_pack_filtered", self._pack_warning_codes(failed))

    def test_pack_promote_empty_selection_fails(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_empty_selection", rows=1)
        before = {
            "maps": self._table_count("promoted_pack_rows"),
            "audit": self._table_count("promotion_audit"),
        }
        failed = self._pack_promote_error(
            pack_id=pack_id,
            topics=["no-such-topic-for-phase4b"],
            confirm_promote=True,
        )
        self.assertEqual(self._pack_error_code(failed), "selected_rows_empty")
        after = {
            "maps": self._table_count("promoted_pack_rows"),
            "audit": self._table_count("promotion_audit"),
        }
        self.assertEqual(before, after)

    def test_pack_promote_no_source_db_id_leak_in_output(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_no_source_id_leak", rows=1)
        row_id, imported_memory_id, _kind = self._pack_rows(pack_id)[0]
        secret_literal = "mem_source_exporter_db_777"
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            conn.execute("UPDATE memories SET text = ? WHERE id = ?", (secret_literal, imported_memory_id))
            conn.commit()
        finally:
            conn.close()
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        serialized = json.dumps(promoted["structuredContent"], ensure_ascii=False)
        self.assertNotIn(secret_literal, serialized)

    def test_pack_review_import_shows_promoted_status(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_review_promotion_status", rows=2)
        row_id = self._pack_rows(pack_id)[0][0]
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        promoted_id = str(promoted["structuredContent"]["promoted_rows"][0]["promoted_memory_id"])
        reviewed = self._pack_review_import(pack_id=pack_id, include_samples=True, sample_limit=50)
        samples = reviewed["structuredContent"]["samples"]
        self.assertTrue(samples)
        promoted_samples = [sample for sample in samples if str(sample["row_id_in_pack"]) == row_id]
        self.assertEqual(len(promoted_samples), 1)
        promoted_sample = promoted_samples[0]
        self.assertEqual(str(promoted_sample.get("promoted_to_memory_id")), promoted_id)
        self.assertTrue(str(promoted_sample.get("promotion_id", "")).startswith("promotion_"))
        self.assertTrue(bool(promoted_sample.get("promoted_at")))
        unpromoted = [sample for sample in samples if str(sample["row_id_in_pack"]) != row_id]
        self.assertTrue(any(sample.get("promoted_to_memory_id") is None for sample in unpromoted))

    def test_pack_promote_read_only_inputs_not_pack_zip(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase4b_no_zip_reads", rows=1)
        row_id = self._pack_rows(pack_id)[0][0]
        with mock.patch.object(server.zipfile, "ZipFile", side_effect=AssertionError("zipfile access not expected")):
            promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        self.assertFalse(promoted["isError"], promoted)

    def test_memory_packs_full_lifecycle_end_to_end(self) -> None:
        marker = "phase196_e2e"
        touched_a = self.workspace / "src" / "phase196" / "auth.py"
        touched_b = self.workspace / "src" / "phase196" / "billing.py"
        touched_a.parent.mkdir(parents=True, exist_ok=True)
        touched_b.parent.mkdir(parents=True, exist_ok=True)
        touched_a.write_text("AUTH='phase196'\n", encoding="utf-8")
        touched_b.write_text("BILLING='phase196'\n", encoding="utf-8")

        local_a = self.record(
            f"{marker} local context email test.user@example.test",
            kind="context_block",
            title=f"{marker} title context",
            touched_files=["src/phase196/auth.py"],
        )
        local_b = self.record(
            f"{marker} local hippocampus key AKIA1234567890ABCDEF",
            kind="hippocampus_entry",
            title=f"{marker} title hippocampus",
            touched_files=["src/phase196/billing.py"],
        )
        source_ids = [str(local_a["id"]), str(local_b["id"])]
        topic_name = f"{marker}-topic"
        for memory_id in source_ids:
            added = server.topic_add({"memory_id": memory_id, "topic": topic_name, "source": "operator"})
            self.assertFalse(added["isError"], added)

        preview = self._pack_preview(topics=[topic_name], kinds=["context_block", "hippocampus_entry"], limit=100)
        preview_sc = preview["structuredContent"]
        self.assertGreaterEqual(int(preview_sc["selection"]["total_rows"]), 2)

        redaction_preview = self._pack_redaction_preview(
            topics=[topic_name],
            kinds=["context_block", "hippocampus_entry"],
            include_redacted_samples=True,
            max_redacted_samples=10,
        )
        redaction_sc = redaction_preview["structuredContent"]
        self.assertGreater(int(redaction_sc["redaction"]["total_matches"]), 0)

        export = self._pack_export(
            pack_name=marker,
            output_dir=str(self.root / marker),
            allow_unsigned=True,
            topics=[topic_name],
            kinds=["context_block", "hippocampus_entry"],
            limit=100,
        )
        export_sc = export["structuredContent"]
        pack_path = Path(str(export_sc["output_path"]))
        self.assertTrue(pack_path.exists())

        inspect = self._pack_inspect(pack_path=str(pack_path))
        inspect_sc = inspect["structuredContent"]
        self.assertEqual(str(inspect_sc["status"]), "valid")
        self.assertEqual(str(inspect_sc["import_recommendation"]), "quarantine_only")

        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        imported_sc = imported["structuredContent"]
        pack_id = str(imported_sc["pack_id"])
        import_namespace = str(imported_sc["namespace"])
        imported_rows = self._pack_rows(pack_id)
        self.assertTrue(imported_rows)

        listed = self._pack_list_imports(pack_id=pack_id)
        packs = listed["structuredContent"]["packs"]
        self.assertEqual(len(packs), 1)
        self.assertEqual(str(packs[0]["pack_id"]), pack_id)

        reviewed = self._pack_review_import(pack_id=pack_id, topics=[topic_name], include_samples=True, sample_limit=50)
        self.assertGreaterEqual(int(reviewed["structuredContent"]["selection"]["selected_rows"]), 2)

        promote_row_id = str(imported_rows[0][0])
        promote_imported_memory_id = str(imported_rows[0][1])
        preview_promote = self._pack_promote_preview(pack_id=pack_id, row_ids=[promote_row_id], limit=100)
        self.assertEqual(int(preview_promote["structuredContent"]["selection"]["selected_rows"]), 1)

        promoted = self._pack_promote(pack_id=pack_id, row_ids=[promote_row_id], confirm_promote=True)
        promoted_sc = promoted["structuredContent"]
        promoted_memory_id = str(promoted_sc["promoted_rows"][0]["promoted_memory_id"])
        promotion_id = str(promoted_sc["promotion_id"])

        default_search = server.search_memories({"query": marker, "limit": 50})
        include_quarantine = server.search_memories({"query": marker, "limit": 50, "include_quarantine": True})
        promoted_only = server.search_memories({"query": marker, "limit": 50, "origins": ["promoted"]})
        self.assertFalse(default_search["isError"], default_search)
        self.assertFalse(include_quarantine["isError"], include_quarantine)
        self.assertFalse(promoted_only["isError"], promoted_only)
        default_ids = {str(row["id"]) for row in default_search["structuredContent"]["matches"]}
        quarantine_ids = {str(row["id"]) for row in include_quarantine["structuredContent"]["matches"]}
        promoted_ids = {str(row["id"]) for row in promoted_only["structuredContent"]["matches"]}
        self.assertIn(promoted_memory_id, default_ids)
        self.assertNotIn(promote_imported_memory_id, default_ids)
        self.assertIn(promote_imported_memory_id, quarantine_ids)
        self.assertIn(promoted_memory_id, promoted_ids)
        self.assertNotIn(promote_imported_memory_id, promoted_ids)

        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            imported_row_after = conn.execute(
                "SELECT namespace, origin FROM memories WHERE id = ?",
                (promote_imported_memory_id,),
            ).fetchone()
            promoted_row_after = conn.execute(
                "SELECT namespace, origin FROM memories WHERE id = ?",
                (promoted_memory_id,),
            ).fetchone()
            map_row = conn.execute(
                "SELECT promoted_memory_id, promotion_id FROM promoted_pack_rows WHERE pack_id = ? AND row_id_in_pack = ?",
                (pack_id, promote_row_id),
            ).fetchone()
            audit_row = conn.execute(
                "SELECT promotion_id, row_count FROM promotion_audit WHERE promotion_id = ?",
                (promotion_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(imported_row_after)
        self.assertIsNotNone(promoted_row_after)
        self.assertIsNotNone(map_row)
        self.assertIsNotNone(audit_row)
        assert imported_row_after is not None
        assert promoted_row_after is not None
        assert map_row is not None
        self.assertEqual(str(imported_row_after["namespace"]), import_namespace)
        self.assertEqual(str(imported_row_after["origin"]), "imported")
        self.assertEqual(str(promoted_row_after["namespace"]), "local")
        self.assertEqual(str(promoted_row_after["origin"]), "promoted")
        self.assertEqual(str(map_row["promoted_memory_id"]), promoted_memory_id)

        members = self._read_zip_members(pack_path)
        required = set(server.PACK_REQUIRED_MEMBERS)
        self.assertTrue(required.issubset(set(members)))
        zipped_text = "\n".join(
            members[name].decode("utf-8", errors="replace")
            for name in sorted(required)
        )
        for source_id in source_ids:
            self.assertNotIn(source_id, zipped_text)
        self.assertNotIn("test.user@example.test", zipped_text)
        self.assertNotIn("AKIA1234567890ABCDEF", zipped_text)
        self.assertIn("[REDACTED:email]", zipped_text)
        self.assertIn("[REDACTED:aws_access_key]", zipped_text)

        import_payload = json.dumps(imported_sc, ensure_ascii=False)
        promote_payload = json.dumps(promoted_sc, ensure_ascii=False)
        for source_id in source_ids:
            self.assertNotIn(source_id, import_payload)
            self.assertNotIn(source_id, promote_payload)

    def test_memory_packs_schema_migrations_idempotent(self) -> None:
        required_tables = {
            "memories",
            "memory_topics",
            "memory_files",
            "imported_packs",
            "exported_packs",
            "imported_pack_rows",
            "promoted_pack_rows",
            "promotion_audit",
            "trusted_signers",
            "alias_concepts",
            "alias_terms",
            "alias_proposals",
            "alias_proposal_events",
        }

        def _assert_current_schema(expected_memory_rows: int) -> None:
            conn = sqlite3.connect(str(self.sqlite_file))
            try:
                schema_value = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                self.assertIsNotNone(schema_value)
                assert schema_value is not None
                self.assertEqual(int(schema_value[0]), 7)
                tables = {
                    str(row[0])
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                self.assertTrue(required_tables.issubset(tables))
                memory_cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
                self.assertTrue(
                    {"namespace", "origin", "import_freshness", "git_sha", "git_branch", "git_dirty"}.issubset(memory_cols)
                )
                imported_cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(imported_packs)").fetchall()}
                self.assertIn("received_zip_sha256", imported_cols)
                promoted_cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(promoted_pack_rows)").fetchall()}
                self.assertIn("promotion_id", promoted_cols)
                memory_count = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
                self.assertEqual(memory_count, expected_memory_rows)
                _ = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
            finally:
                conn.close()

        fixtures: list[tuple[str, Any]] = []

        def _setup_empty() -> int:
            self._reset_sqlite_file()
            return 0

        fixtures.append(("empty", _setup_empty))

        def _setup_pre_memory_packs() -> int:
            self._reset_sqlite_file()
            legacy = server.new_memory("phase196-legacy", "note", "legacy row for migration", "", [])
            legacy.pop("namespace", None)
            legacy.pop("origin", None)
            legacy.pop("import_freshness", None)
            self._create_pre_phase1_schema([legacy], with_fts=True)
            return 1

        fixtures.append(("pre_memory_packs", _setup_pre_memory_packs))

        def _setup_post_import_pre_promotion() -> int:
            self._reset_sqlite_file()
            legacy = server.new_memory("phase196-prepromote", "note", "post-import pre-promotion fixture", "", [])
            legacy.pop("namespace", None)
            legacy.pop("origin", None)
            legacy.pop("import_freshness", None)
            self._create_pre_phase1_schema([legacy], with_fts=True)
            conn = sqlite3.connect(str(self.sqlite_file))
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_files (
                        memory_table TEXT NOT NULL,
                        memory_id TEXT NOT NULL,
                        path TEXT NOT NULL,
                        file_sha TEXT NOT NULL,
                        PRIMARY KEY (memory_table, memory_id, path)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_topics (
                        memory_id TEXT NOT NULL,
                        topic TEXT NOT NULL,
                        created_at TEXT,
                        source TEXT,
                        PRIMARY KEY (memory_id, topic)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS imported_packs (
                        pack_id TEXT PRIMARY KEY,
                        pack_name TEXT NOT NULL,
                        source_label TEXT,
                        trust_level TEXT NOT NULL,
                        namespace TEXT NOT NULL,
                        imported_at TEXT NOT NULL,
                        manifest_json TEXT NOT NULL,
                        freshness_summary_json TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS imported_pack_rows (
                        pack_id TEXT NOT NULL,
                        row_id_in_pack TEXT NOT NULL,
                        memory_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        imported_at TEXT NOT NULL,
                        PRIMARY KEY (pack_id, row_id_in_pack),
                        UNIQUE(memory_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS exported_packs (
                        pack_id TEXT PRIMARY KEY,
                        pack_name TEXT NOT NULL,
                        exported_at TEXT NOT NULL,
                        row_count INTEGER NOT NULL,
                        redaction_count INTEGER NOT NULL,
                        signed INTEGER NOT NULL DEFAULT 0,
                        manifest_json TEXT NOT NULL
                    )
                    """
                )
                conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', '5')")
                conn.commit()
            finally:
                conn.close()
            server._SQLITE_BOOTSTRAPPED.clear()
            return 1

        fixtures.append(("post_import_pre_promotion", _setup_post_import_pre_promotion))

        def _setup_current() -> int:
            self._reset_sqlite_file()
            server.load_store()
            return 0

        fixtures.append(("current", _setup_current))

        for fixture_name, setup_fn in fixtures:
            with self.subTest(fixture=fixture_name):
                expected_rows = int(setup_fn())
                server.load_store()
                server.load_store()
                _assert_current_schema(expected_rows)

    def test_memory_packs_action_dispatch_complete(self) -> None:
        marker = "phase196_dispatch"
        seed = self.record(
            f"{marker} seed",
            kind="context_block",
            touched_files=["src/phase196/dispatch.py"],
        )
        added = server.topic_add({"memory_id": str(seed["id"]), "topic": f"{marker}-topic", "source": "operator"})
        self.assertFalse(added["isError"], added)

        export = server.mnemo_gateway(
            {
                "action": "pack_export",
                "params": {
                    "pack_name": marker,
                    "output_dir": str(self.root / marker),
                    "allow_unsigned": True,
                    "topics": [f"{marker}-topic"],
                    "kinds": ["context_block"],
                    "limit": 100,
                },
            }
        )
        self.assertFalse(export["isError"], export)
        pack_path = str(export["structuredContent"]["output_path"])
        imported = server.mnemo_gateway(
            {"action": "pack_import", "params": {"pack_path": pack_path, "allow_unsigned_quarantine": True}}
        )
        self.assertFalse(imported["isError"], imported)
        pack_id = str(imported["structuredContent"]["pack_id"])
        row_id = str(self._pack_rows(pack_id)[0][0])

        dispatch_calls = [
            ("pack_preview", {}),
            ("pack_redaction_preview", {}),
            ("pack_export", {"pack_name": f"{marker}_2", "output_dir": str(self.root / f"{marker}_2"), "allow_unsigned": True}),
            ("pack_inspect", {"pack_path": pack_path}),
            ("pack_import", {"pack_path": pack_path, "allow_unsigned_quarantine": True}),
            ("pack_list_imports", {"pack_id": pack_id}),
            ("pack_review_import", {"pack_id": pack_id}),
            ("pack_promote_preview", {"pack_id": pack_id, "row_ids": [row_id]}),
            ("pack_promote", {"pack_id": pack_id, "row_ids": [row_id], "confirm_promote": True}),
            ("signer_add", {"signer_id": f"{marker}.signer", "secret": "dispatch-secret-012345678901234567890"}),
            ("signer_list", {}),
            ("signer_disable", {"signer_id": f"{marker}.signer"}),
            ("signer_enable", {"signer_id": f"{marker}.signer"}),
        ]
        for action_name, params in dispatch_calls:
            with self.subTest(action=action_name):
                result = server.mnemo_gateway({"action": action_name, "params": params})
                payload_text = json.dumps(result.get("structuredContent", {}), ensure_ascii=False)
                self.assertNotIn("unknown action", payload_text.lower())

    def test_memory_packs_read_only_actions_do_not_mutate(self) -> None:
        marker = "phase196_read_only"
        seeded = self.record(
            f"{marker} seed text test.user@example.test",
            kind="context_block",
            title=f"{marker} title",
            touched_files=["src/phase196/read_only.py"],
        )
        added = server.topic_add({"memory_id": str(seeded["id"]), "topic": f"{marker}-topic", "source": "operator"})
        self.assertFalse(added["isError"], added)
        pack_path = self._create_exported_pack(
            pack_name=marker,
            output_dir=self.root / marker,
            topics=[f"{marker}-topic"],
            kinds=["context_block"],
            limit=100,
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        pack_id = str(imported["structuredContent"]["pack_id"])
        row_id = str(self._pack_rows(pack_id)[0][0])
        before = self._read_only_snapshot()

        self._pack_preview(topics=[f"{marker}-topic"])
        self._pack_redaction_preview(topics=[f"{marker}-topic"])
        self._pack_inspect(pack_path=str(pack_path))
        self._pack_list_imports(pack_id=pack_id)
        self._pack_review_import(pack_id=pack_id, row_ids=[row_id], include_samples=True, sample_limit=5)
        self._pack_promote_preview(pack_id=pack_id, row_ids=[row_id], include_samples=True, sample_limit=5)

        after = self._read_only_snapshot()
        self.assertEqual(before, after)

    def test_memory_packs_export_artifact_safety(self) -> None:
        marker = "phase196_artifact"
        source = self.record(
            f"{marker} email test.user@example.test jwt eyJhbGciOiJIUzI1NiJ9.aaaa.bbbb",
            kind="context_block",
            title=f"{marker} title",
            touched_files=["src/phase196/artifact.py"],
        )
        added = server.topic_add({"memory_id": str(source["id"]), "topic": f"{marker}-topic", "source": "operator"})
        self.assertFalse(added["isError"], added)
        export = self._pack_export(
            pack_name=marker,
            output_dir=str(self.root / marker),
            allow_unsigned=True,
            topics=[f"{marker}-topic"],
            kinds=["context_block"],
        )
        export_sc = export["structuredContent"]
        pack_path = Path(str(export_sc["output_path"]))
        members = self._read_zip_members(pack_path)
        required = set(server.PACK_REQUIRED_MEMBERS)
        self.assertTrue(required.issubset(set(members)))

        manifest = json.loads(members["manifest.json"].decode("utf-8"))
        memories_lines = [line for line in members["content/memories.jsonl"].decode("utf-8").splitlines() if line.strip()]
        for line in memories_lines:
            self.assertIsInstance(json.loads(line), dict)
        recomputed = self._recompute_pack_content_hash(members, list(manifest["content_hash"]["covered_members"]))
        self.assertEqual(recomputed, str(manifest["content_hash"]["value"]))
        self.assertEqual(recomputed, str(export_sc["content_hash"]["value"]))

        required_text = "\n".join(members[name].decode("utf-8", errors="replace") for name in sorted(required))
        self.assertNotIn(str(source["id"]), required_text)
        self.assertNotIn("test.user@example.test", required_text)
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9.aaaa.bbbb", required_text)
        self.assertIn("[REDACTED:email]", required_text)
        self.assertIn("[REDACTED:jwt]", required_text)

        redactions = json.loads(members["provenance/redactions.json"].decode("utf-8"))
        self.assertEqual(int(redactions["total_matches"]), int(manifest["redaction"]["total_matches"]))
        self.assertEqual(int(redactions["affected_rows"]), int(manifest["redaction"]["affected_rows"]))

        inspected = self._pack_inspect(pack_path=str(pack_path))
        self.assertEqual(str(inspected["structuredContent"]["status"]), "valid")
        self.assertEqual(str(inspected["structuredContent"]["import_recommendation"]), "quarantine_only")

        tampered = self.root / marker / "tampered.zip"
        self._rewrite_zip(pack_path, tampered, replace_members={"content/memories.jsonl": b"{bad-json}\n"})
        invalid = server.pack_inspect({"pack_path": str(tampered)})
        self.assertFalse(invalid["isError"], invalid)
        self.assertEqual(str((invalid.get("structuredContent") or {}).get("status")), "invalid")

    def test_memory_packs_error_codes_stable(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase196_errors_base", rows=2)
        row_id = self._pack_rows(pack_id)[0][0]

        missing_pack = self._pack_review_import_error(pack_id="phase196_missing_pack")
        self.assertEqual(self._pack_error_code(missing_pack), "pack_not_found")

        require_allow_all = self._pack_promote_error(pack_id=pack_id, confirm_promote=True)
        self.assertEqual(self._pack_error_code(require_allow_all), "promote_all_requires_explicit_allow")

        reject_query = self._pack_promote_error(
            pack_id=pack_id,
            row_ids=[row_id],
            confirm_promote=True,
            query="not-allowed",
        )
        self.assertEqual(self._pack_error_code(reject_query), "query_filter_not_allowed_for_promotion")

        promoted_once = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        self.assertFalse(promoted_once["isError"], promoted_once)
        duplicate = self._pack_promote_error(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        self.assertEqual(self._pack_error_code(duplicate), "pack_rows_already_promoted")

        pack_a, _ = self._create_phase4b_imported_pack(marker="phase196_errors_outside_a", rows=1)
        pack_b, _ = self._create_phase4b_imported_pack(marker="phase196_errors_outside_b", rows=1)
        outside_id = self._pack_rows(pack_b)[0][1]
        outside_local = self.record("phase196 outside memory", kind="context_block")
        collapsed = self._pack_promote_error(
            pack_id=pack_a,
            memory_ids=[outside_id, str(outside_local["id"])],
            confirm_promote=True,
        )
        self.assertEqual(self._pack_error_code(collapsed), "selected_rows_empty")
        self.assertIn("memory_ids_outside_pack_filtered", self._pack_warning_codes(collapsed))

        self._insert_imported_pack_unchecked(
            pack_id="phase196_invalid_trust_pack",
            trust_level="legacy_invalid",
            namespace="pack:legacy:phase196-invalid-trust-pack",
        )
        trusted_fail = self._pack_promote_error(
            pack_id="phase196_invalid_trust_pack",
            allow_promote_all=True,
            confirm_promote=True,
        )
        self.assertEqual(self._pack_error_code(trusted_fail), "unsupported_trust_level_for_promotion")

        self.record("phase196 import duplicate seed", kind="context_block")
        pack_path = self._create_exported_pack(
            pack_name="phase196_import_duplicate",
            output_dir=self.root / "phase196_import_duplicate",
        )
        self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        same_bytes = self._pack_import_error(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        self.assertIn("pack_already_imported", same_bytes["content"][0]["text"])
        variant = self.root / "phase196_import_duplicate" / "variant.zip"
        self._rewrite_zip(pack_path, variant, extra_members={"extra/collision.txt": b"phase196"})
        collision = self._pack_import_error(pack_path=str(variant), allow_unsigned_quarantine=True)
        self.assertIn("pack_id_collision_distinct_content", collision["content"][0]["text"])

        inspect_base = self._create_exported_pack(
            pack_name="phase196_inspect_codes",
            output_dir=self.root / "phase196_inspect_codes",
        )
        with zipfile.ZipFile(inspect_base, "r") as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            members = {name: archive.read(name) for name in archive.namelist()}
        bad_covered_manifest = dict(manifest)
        bad_hash = dict(manifest["content_hash"])
        bad_hash["covered_members"] = ["content/memories.jsonl"]
        bad_hash["value"] = hashlib.sha256(
            f"content/memories.jsonl\t{hashlib.sha256(members['content/memories.jsonl']).hexdigest()}\n".encode("utf-8")
        ).hexdigest()
        bad_covered_manifest["content_hash"] = bad_hash
        covered_path = self.root / "phase196_inspect_codes" / "covered_mismatch.zip"
        self._rewrite_zip(
            inspect_base,
            covered_path,
            replace_members={
                "manifest.json": (json.dumps(bad_covered_manifest, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            },
        )
        covered = server.pack_inspect({"pack_path": str(covered_path)})
        self.assertFalse(covered["isError"], covered)
        self.assertEqual(str((covered.get("structuredContent") or {}).get("status")), "invalid")
        covered_codes = {
            str(item.get("code"))
            for item in (covered.get("structuredContent", {}) or {}).get("errors", [])
            if isinstance(item, dict)
        }
        self.assertIn("covered_members_mismatch", covered_codes)

        leaked_path = self.root / "phase196_inspect_codes" / "source_id_leak.zip"
        self._rewrite_zip(
            inspect_base,
            leaked_path,
            replace_members={"content/memories.jsonl": b'{"row_id_in_pack":"ctx_001","kind":"context_block","namespace_at_export":"local","origin_at_export":"local","text_fields":{"text":"mem_secret_source_777","title":""},"topics":[],"created_at_in_source":null,"git_sha_at_write":null,"git_branch_at_write":null,"git_dirty_at_write":0,"touched_files":[],"import_freshness_at_export":null,"redaction_applied":true}\n'},
        )
        leaked = server.pack_inspect({"pack_path": str(leaked_path)})
        self.assertFalse(leaked["isError"], leaked)
        self.assertEqual(str((leaked.get("structuredContent") or {}).get("status")), "invalid")
        leak_codes = {
            str(item.get("code"))
            for item in (leaked.get("structuredContent", {}) or {}).get("errors", [])
            if isinstance(item, dict)
        }
        self.assertIn("source_memory_id_leak", leak_codes)

    def test_memory_packs_retrieval_boundaries(self) -> None:
        marker = "phase196_boundaries"
        pack_id, imported_sc = self._create_phase4b_imported_pack(marker=marker, rows=1, kinds=["context_block"])
        pack_rows = self._pack_rows(pack_id)
        self.assertEqual(len(pack_rows), 1)
        row_id, imported_memory_id, _kind = pack_rows[0]
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        promoted_memory_id = str(promoted["structuredContent"]["promoted_rows"][0]["promoted_memory_id"])
        namespace = str(imported_sc.get("namespace", ""))

        query_text = f"{marker} memory row 0"
        default = server.search_memories({"query": query_text, "limit": 50})
        imported_only = server.search_memories({"query": query_text, "limit": 50, "include_imported": True})
        quarantine = server.search_memories({"query": query_text, "limit": 50, "include_quarantine": True})
        by_namespace = server.search_memories({"query": query_text, "limit": 50, "namespace": namespace})
        promoted_only = server.search_memories({"query": query_text, "limit": 50, "origins": ["promoted"]})

        default_ids = {str(row["id"]) for row in default["structuredContent"]["matches"]}
        imported_only_ids = {str(row["id"]) for row in imported_only["structuredContent"]["matches"]}
        quarantine_ids = {str(row["id"]) for row in quarantine["structuredContent"]["matches"]}
        namespace_ids = {str(row["id"]) for row in by_namespace["structuredContent"]["matches"]}
        promoted_ids = {str(row["id"]) for row in promoted_only["structuredContent"]["matches"]}

        self.assertIn(promoted_memory_id, default_ids)
        self.assertNotIn(imported_memory_id, default_ids)
        self.assertNotIn(imported_memory_id, imported_only_ids)
        self.assertIn(imported_memory_id, quarantine_ids)
        self.assertIn(imported_memory_id, namespace_ids)
        self.assertIn(promoted_memory_id, promoted_ids)
        self.assertNotIn(imported_memory_id, promoted_ids)

    def test_memory_packs_promotion_audit_integrity(self) -> None:
        pack_id, _imported_sc = self._create_phase4b_imported_pack(marker="phase196_audit", rows=2)
        row_ids = [row[0] for row in self._pack_rows(pack_id)]
        promoted = self._pack_promote(
            pack_id=pack_id,
            row_ids=row_ids,
            confirm_promote=True,
            limit=100,
        )
        promoted_sc = promoted["structuredContent"]
        promotion_id = str(promoted_sc["promotion_id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            audit = conn.execute(
                "SELECT filters_json, row_count, limited, allow_promote_all, allow_limited_promotion FROM promotion_audit WHERE promotion_id = ?",
                (promotion_id,),
            ).fetchone()
            linked_rows = conn.execute(
                "SELECT row_id_in_pack, imported_memory_id, promoted_memory_id, promotion_id FROM promoted_pack_rows WHERE promotion_id = ? ORDER BY row_id_in_pack ASC",
                (promotion_id,),
            ).fetchall()
        finally:
            conn.close()
        self.assertIsNotNone(audit)
        assert audit is not None
        parsed_filters = json.loads(str(audit["filters_json"]))
        self.assertEqual(parsed_filters.get("row_ids"), row_ids)
        self.assertEqual(int(audit["row_count"]), len(row_ids))
        self.assertEqual(int(audit["limited"]), 0)
        self.assertEqual(int(audit["allow_promote_all"]), 0)
        self.assertEqual(int(audit["allow_limited_promotion"]), 0)
        self.assertEqual(len(linked_rows), len(row_ids))
        self.assertTrue(all(str(row["promotion_id"]) == promotion_id for row in linked_rows))

    def test_memory_packs_no_source_db_id_leak_outputs(self) -> None:
        marker = "phase196_no_source_leak"
        source_a = self.record(f"{marker} local row A", kind="context_block")
        source_b = self.record(f"{marker} local row B", kind="hippocampus_entry")
        topic = f"{marker}-topic"
        server.topic_add({"memory_id": str(source_a["id"]), "topic": topic, "source": "operator"})
        server.topic_add({"memory_id": str(source_b["id"]), "topic": topic, "source": "operator"})
        source_ids = [str(source_a["id"]), str(source_b["id"])]

        export = self._pack_export(
            pack_name=marker,
            output_dir=str(self.root / marker),
            allow_unsigned=True,
            topics=[topic],
            kinds=["context_block", "hippocampus_entry"],
        )
        export_sc = export["structuredContent"]
        pack_path = Path(str(export_sc["output_path"]))
        import_result = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        import_sc = import_result["structuredContent"]
        pack_id = str(import_sc["pack_id"])
        review = self._pack_review_import(pack_id=pack_id, include_samples=True, sample_limit=20)
        row_id = self._pack_rows(pack_id)[0][0]
        promote = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)

        payloads = [
            json.dumps(export_sc, ensure_ascii=False),
            json.dumps(import_sc, ensure_ascii=False),
            json.dumps(review["structuredContent"], ensure_ascii=False),
            json.dumps(promote["structuredContent"], ensure_ascii=False),
        ]
        required_members = self._read_zip_members(pack_path)
        payloads.append(
            "\n".join(
                required_members[name].decode("utf-8", errors="replace")
                for name in sorted(server.PACK_REQUIRED_MEMBERS)
            )
        )
        for source_id in source_ids:
            for payload in payloads:
                self.assertNotIn(source_id, payload)

    def test_memory_packs_current_db_schema_bootstrap_no_content_bootstrap_for_preview(self) -> None:
        self._reset_sqlite_file()
        result = server.pack_preview({})
        self.assertFalse(result["isError"], result)
        self.assertTrue(self._table_exists("memories"))
        self.assertEqual(self._table_count("memories"), 0)
        self.assertEqual(self._table_count("exported_packs"), 0)
        self.assertEqual(self._table_count("imported_packs"), 0)

    def test_signer_add_list_disable_enable(self) -> None:
        secret = "s" * 32
        added = self._signer_add(signer_id="alice.dev", secret=secret, label="Alice Dev")
        listed = self._signer_list()
        disabled = self._signer_disable(signer_id="alice.dev")
        enabled = self._signer_enable(signer_id="alice.dev")

        signers = listed["structuredContent"]["signers"]
        self.assertTrue(any(str(item.get("signer_id")) == "alice.dev" for item in signers))
        self.assertEqual(str(disabled["structuredContent"]["signer_status"]), "disabled")
        self.assertEqual(str(enabled["structuredContent"]["signer_status"]), "active")

        payload_blob = "\n".join(
            json.dumps(result.get("structuredContent", {}), ensure_ascii=False)
            for result in (added, listed, disabled, enabled)
        )
        self.assertNotIn(secret, payload_blob)

    def test_signer_add_duplicate_rejected(self) -> None:
        secret = "dup-secret-012345678901234567890123"
        self._signer_add(signer_id="dup.signer", secret=secret)
        dup = self._signer_add_error(signer_id="dup.signer", secret=secret)
        self.assertEqual(self._pack_error_code(dup), "signer_already_exists")

    def test_signer_secret_too_short_rejected(self) -> None:
        short = "short-secret"
        signer_fail = self._signer_add_error(signer_id="short.signer", secret=short)
        self.assertEqual(self._pack_error_code(signer_fail), "secret_too_short")

        self.record("phase200 short secret export", kind="context_block")
        export_fail = self._pack_export_error(
            pack_name="phase200_short_secret_export",
            output_dir=str(self.root / "phase200_short_secret_export"),
            sign_pack=True,
            signer_id="short.signer",
            signing_secret=short,
        )
        self.assertEqual(self._pack_error_code(export_fail), "secret_too_short")

        unsigned_pack = self._create_exported_pack(
            pack_name="phase200_short_secret_inspect",
            output_dir=self.root / "phase200_short_secret_inspect",
        )
        inspect_fail = server.pack_inspect({"pack_path": str(unsigned_pack), "verification_secret": short})
        self.assertTrue(inspect_fail["isError"], inspect_fail)
        self.assertEqual(self._pack_error_code(inspect_fail), "secret_too_short")

    def test_secret_fingerprint_recipe_stable(self) -> None:
        secret_a = "A" * 32
        secret_b = "B" * 32
        expected_a = hashlib.sha256(secret_a.encode("utf-8")).hexdigest()[:32]
        self.assertEqual(server._secret_fingerprint(secret_a), expected_a)
        self.assertNotEqual(server._secret_fingerprint(secret_a), server._secret_fingerprint(secret_b))

    def test_pack_export_signed_hmac_creates_signature_member(self) -> None:
        marker = "phase200_signed_export_member"
        row = self.record(f"{marker} source", kind="context_block")
        topic = f"{marker}-topic"
        server.topic_add({"memory_id": str(row["id"]), "topic": topic, "source": "operator"})
        secret = "signed-export-secret-0123456789012345"
        pack_path, export_sc = self._create_signed_exported_pack(
            pack_name=marker,
            output_dir=self.root / marker,
            signer_id="alice.sign",
            signing_secret=secret,
            topics=[topic],
            kinds=["context_block"],
        )
        members = self._read_zip_members(pack_path)
        self.assertIn("signature/signature.json", members)
        manifest = json.loads(members["manifest.json"].decode("utf-8"))
        self.assertTrue(bool(manifest.get("signed")))
        self.assertIn("signature", manifest)
        warning_codes = self._pack_warning_codes({"structuredContent": export_sc})
        self.assertIn("local_hmac_not_public_key", warning_codes)
        self.assertNotIn("unsigned_development_pack", warning_codes)

    def test_pack_export_unsigned_still_supported(self) -> None:
        marker = "phase200_unsigned_still_supported"
        row = self.record(f"{marker} source", kind="context_block")
        topic = f"{marker}-topic"
        server.topic_add({"memory_id": str(row["id"]), "topic": topic, "source": "operator"})
        pack_path = self._create_exported_pack(
            pack_name=marker,
            output_dir=self.root / marker,
            topics=[topic],
            kinds=["context_block"],
        )
        members = self._read_zip_members(pack_path)
        manifest = json.loads(members["manifest.json"].decode("utf-8"))
        self.assertFalse(bool(manifest.get("signed")))
        self.assertEqual(str(manifest.get("unsigned_reason", "")), "operator_chose_unsigned")

        legacy_manifest = dict(manifest)
        legacy_manifest["unsigned_reason"] = "signing_not_implemented"
        legacy_path = self.root / marker / "legacy_unsigned_reason.zip"
        self._rewrite_zip(
            pack_path,
            legacy_path,
            replace_members={
                "manifest.json": (json.dumps(legacy_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            },
        )
        inspected = self._pack_inspect(pack_path=str(legacy_path))
        self.assertEqual(str(inspected["structuredContent"]["status"]), "valid")
        self.assertEqual(str(inspected["structuredContent"]["import_recommendation"]), "quarantine_only")

    def test_pack_inspect_unsigned_pack_still_quarantine_only(self) -> None:
        row = self.record("phase200 unsigned quarantine source", kind="context_block")
        topic = "phase200-unsigned-quarantine-topic"
        server.topic_add({"memory_id": str(row["id"]), "topic": topic, "source": "operator"})
        pack_path = self._create_exported_pack(
            pack_name="phase200_unsigned_quarantine_only",
            output_dir=self.root / "phase200_unsigned_quarantine_only",
            topics=[topic],
            kinds=["context_block"],
        )
        inspected = self._pack_inspect(
            pack_path=str(pack_path),
            verification_secret="verification-secret-0123456789012345",
        )
        sc = inspected["structuredContent"]
        self.assertEqual(str(sc["status"]), "valid")
        self.assertEqual(str(sc["import_recommendation"]), "quarantine_only")
        self.assertEqual(str(sc["signature"]["trust_classification"]), "unsigned")
        self.assertIn("verification_secret_unused_for_unsigned_pack", self._pack_warning_codes(inspected))

    def test_pack_inspect_signed_hmac_verified_with_secret(self) -> None:
        secret = "verified-secret-01234567890123456789"
        self._signer_add(signer_id="verified.signer", secret=secret, trust_level="trusted")
        source = self.record("phase200 signed verified source", kind="context_block")
        topic = "phase200-signed-verified-topic"
        server.topic_add({"memory_id": str(source["id"]), "topic": topic, "source": "operator"})
        pack_path, _ = self._create_signed_exported_pack(
            pack_name="phase200_signed_verified",
            output_dir=self.root / "phase200_signed_verified",
            signer_id="verified.signer",
            signing_secret=secret,
            topics=[topic],
            kinds=["context_block"],
        )
        inspected = self._pack_inspect(pack_path=str(pack_path), verification_secret=secret)
        sc = inspected["structuredContent"]
        self.assertEqual(str(sc["status"]), "valid")
        self.assertTrue(bool(sc["signature"]["verified"]))
        self.assertEqual(str(sc["signature"]["trust_classification"]), "trusted_signer")

    def test_pack_inspect_signed_hmac_invalid_secret_rejected(self) -> None:
        secret = "inspect-invalid-secret-012345678901234"
        source = self.record("phase200 signed invalid secret source", kind="context_block")
        topic = "phase200-signed-invalid-secret-topic"
        server.topic_add({"memory_id": str(source["id"]), "topic": topic, "source": "operator"})
        pack_path, _ = self._create_signed_exported_pack(
            pack_name="phase200_signed_invalid_secret",
            output_dir=self.root / "phase200_signed_invalid_secret",
            signer_id="invalid.secret.signer",
            signing_secret=secret,
            topics=[topic],
            kinds=["context_block"],
        )
        inspected = self._pack_inspect(
            pack_path=str(pack_path),
            verification_secret="wrong-secret-01234567890123456789012",
        )
        sc = inspected["structuredContent"]
        self.assertEqual(str(sc["status"]), "invalid")
        self.assertEqual(str(sc["signature"]["trust_classification"]), "invalid_signature")
        self.assertEqual(str(sc["import_recommendation"]), "reject")

    def test_pack_inspect_signed_hmac_no_secret_quarantine(self) -> None:
        secret = "inspect-no-secret-01234567890123456789"
        source = self.record("phase200 signed no secret source", kind="context_block")
        topic = "phase200-signed-no-secret-topic"
        server.topic_add({"memory_id": str(source["id"]), "topic": topic, "source": "operator"})
        pack_path, _ = self._create_signed_exported_pack(
            pack_name="phase200_signed_no_secret",
            output_dir=self.root / "phase200_signed_no_secret",
            signer_id="no.secret.signer",
            signing_secret=secret,
            topics=[topic],
            kinds=["context_block"],
        )
        inspected = self._pack_inspect(pack_path=str(pack_path))
        sc = inspected["structuredContent"]
        self.assertEqual(str(sc["status"]), "valid")
        self.assertEqual(str(sc["import_recommendation"]), "quarantine_only")
        self.assertTrue(bool(sc["signature"]["present"]))
        self.assertFalse(bool(sc["signature"]["verified"]))
        self.assertIn("signature_not_verified", self._pack_warning_codes(inspected))

    def test_pack_inspect_unknown_signer_quarantine(self) -> None:
        secret = "unknown-signer-secret-012345678901234"
        source = self.record("phase200 unknown signer source", kind="context_block")
        topic = "phase200-unknown-signer-topic"
        server.topic_add({"memory_id": str(source["id"]), "topic": topic, "source": "operator"})
        pack_path, _ = self._create_signed_exported_pack(
            pack_name="phase200_unknown_signer",
            output_dir=self.root / "phase200_unknown_signer",
            signer_id="unknown.signer",
            signing_secret=secret,
            topics=[topic],
            kinds=["context_block"],
        )
        inspected = self._pack_inspect(pack_path=str(pack_path), verification_secret=secret)
        sc = inspected["structuredContent"]
        self.assertEqual(str(sc["status"]), "valid")
        self.assertEqual(str(sc["signature"]["trust_classification"]), "unknown_signer")
        self.assertEqual(str(sc["import_recommendation"]), "quarantine_only")

    def test_pack_inspect_secret_fingerprint_mismatch_rejected(self) -> None:
        secret_a = "mismatch-secret-a-012345678901234567"
        secret_b = "mismatch-secret-b-012345678901234567"
        self._signer_add(signer_id="alice.mismatch", secret=secret_a, trust_level="trusted")
        source = self.record("phase200 mismatch source", kind="context_block")
        topic = "phase200-mismatch-topic"
        server.topic_add({"memory_id": str(source["id"]), "topic": topic, "source": "operator"})
        pack_path, _ = self._create_signed_exported_pack(
            pack_name="phase200_mismatch",
            output_dir=self.root / "phase200_mismatch",
            signer_id="alice.mismatch",
            signing_secret=secret_b,
            topics=[topic],
            kinds=["context_block"],
        )
        inspected = self._pack_inspect(pack_path=str(pack_path), verification_secret=secret_b)
        sc = inspected["structuredContent"]
        self.assertEqual(str(sc["status"]), "invalid")
        self.assertEqual(str(sc["signature"]["trust_classification"]), "secret_fingerprint_mismatch")
        self.assertEqual(str(sc["import_recommendation"]), "reject")

    def test_pack_inspect_blocked_signer_rejected(self) -> None:
        secret = "blocked-signer-secret-0123456789012345"
        self._signer_add(signer_id="blocked.signer", secret=secret, trust_level="blocked")
        source = self.record("phase200 blocked signer source", kind="context_block")
        topic = "phase200-blocked-signer-topic"
        server.topic_add({"memory_id": str(source["id"]), "topic": topic, "source": "operator"})
        pack_path, _ = self._create_signed_exported_pack(
            pack_name="phase200_blocked_signer",
            output_dir=self.root / "phase200_blocked_signer",
            signer_id="blocked.signer",
            signing_secret=secret,
            topics=[topic],
            kinds=["context_block"],
        )
        inspected = self._pack_inspect(pack_path=str(pack_path), verification_secret=secret)
        sc = inspected["structuredContent"]
        self.assertEqual(str(sc["status"]), "invalid")
        self.assertEqual(str(sc["signature"]["trust_classification"]), "blocked_signer")
        self.assertEqual(str(sc["import_recommendation"]), "reject")

    def test_pack_inspect_disabled_signer_rejected(self) -> None:
        secret = "disabled-signer-secret-012345678901234"
        self._signer_add(signer_id="disabled.signer", secret=secret, trust_level="trusted")
        self._signer_disable(signer_id="disabled.signer")
        source = self.record("phase200 disabled signer source", kind="context_block")
        topic = "phase200-disabled-signer-topic"
        server.topic_add({"memory_id": str(source["id"]), "topic": topic, "source": "operator"})
        pack_path, _ = self._create_signed_exported_pack(
            pack_name="phase200_disabled_signer",
            output_dir=self.root / "phase200_disabled_signer",
            signer_id="disabled.signer",
            signing_secret=secret,
            topics=[topic],
            kinds=["context_block"],
        )
        inspected = self._pack_inspect(pack_path=str(pack_path), verification_secret=secret)
        sc = inspected["structuredContent"]
        self.assertEqual(str(sc["status"]), "invalid")
        self.assertEqual(str(sc["signature"]["trust_classification"]), "disabled_signer")
        self.assertEqual(str(sc["import_recommendation"]), "reject")

    def test_signer_disable_then_enable_reclassification(self) -> None:
        secret = "reclassification-secret-012345678901234"
        self._signer_add(signer_id="reclass.signer", secret=secret, trust_level="trusted")
        source = self.record("phase200 reclassification source", kind="context_block")
        topic = "phase200-reclassification-topic"
        server.topic_add({"memory_id": str(source["id"]), "topic": topic, "source": "operator"})
        pack_path, _ = self._create_signed_exported_pack(
            pack_name="phase200_reclassification",
            output_dir=self.root / "phase200_reclassification",
            signer_id="reclass.signer",
            signing_secret=secret,
            topics=[topic],
            kinds=["context_block"],
        )

        trusted = self._pack_inspect(pack_path=str(pack_path), verification_secret=secret)
        self.assertEqual(str(trusted["structuredContent"]["signature"]["trust_classification"]), "trusted_signer")
        self.assertEqual(str(trusted["structuredContent"]["status"]), "valid")

        self._signer_disable(signer_id="reclass.signer")
        disabled = self._pack_inspect(pack_path=str(pack_path), verification_secret=secret)
        self.assertEqual(str(disabled["structuredContent"]["signature"]["trust_classification"]), "disabled_signer")
        self.assertEqual(str(disabled["structuredContent"]["status"]), "invalid")

        self._signer_enable(signer_id="reclass.signer")
        enabled = self._pack_inspect(pack_path=str(pack_path), verification_secret=secret)
        self.assertEqual(str(enabled["structuredContent"]["signature"]["trust_classification"]), "trusted_signer")
        self.assertEqual(str(enabled["structuredContent"]["status"]), "valid")

    def test_pack_import_signed_pack_quarantine_only(self) -> None:
        secret = "signed-import-secret-012345678901234567"
        self._signer_add(signer_id="signed.import.signer", secret=secret, trust_level="trusted")
        source = self.record("phase200 signed import source", kind="context_block")
        topic = "phase200-signed-import-topic"
        server.topic_add({"memory_id": str(source["id"]), "topic": topic, "source": "operator"})
        pack_path, _ = self._create_signed_exported_pack(
            pack_name="phase200_signed_import",
            output_dir=self.root / "phase200_signed_import",
            signer_id="signed.import.signer",
            signing_secret=secret,
            topics=[topic],
            kinds=["context_block"],
        )
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        sc = imported["structuredContent"]
        self.assertEqual(str(sc["trust_level"]), "quarantine")
        self.assertTrue(str(sc["namespace"]).startswith("pack:quarantine:"))

    def test_signature_tampered_content_rejected(self) -> None:
        secret = "tamper-content-secret-0123456789012345"
        source = self.record("phase200 tamper content source", kind="context_block")
        topic = "phase200-tamper-content-topic"
        server.topic_add({"memory_id": str(source["id"]), "topic": topic, "source": "operator"})
        pack_path, _ = self._create_signed_exported_pack(
            pack_name="phase200_tamper_content",
            output_dir=self.root / "phase200_tamper_content",
            signer_id="tamper.content.signer",
            signing_secret=secret,
            topics=[topic],
            kinds=["context_block"],
        )
        tampered = self.root / "phase200_tamper_content" / "tampered_content.zip"
        self._rewrite_zip(pack_path, tampered, replace_members={"content/memories.jsonl": b"{bad-json}\n"})
        inspected = self._pack_inspect(pack_path=str(tampered), verification_secret=secret)
        sc = inspected["structuredContent"]
        self.assertEqual(str(sc["status"]), "invalid")
        self.assertFalse(bool(sc["content_hash"]["valid"]))

    def test_signature_tampered_signature_rejected(self) -> None:
        secret = "tamper-signature-secret-012345678901234"
        source = self.record("phase200 tamper signature source", kind="context_block")
        topic = "phase200-tamper-signature-topic"
        server.topic_add({"memory_id": str(source["id"]), "topic": topic, "source": "operator"})
        pack_path, _ = self._create_signed_exported_pack(
            pack_name="phase200_tamper_signature",
            output_dir=self.root / "phase200_tamper_signature",
            signer_id="tamper.signature.signer",
            signing_secret=secret,
            topics=[topic],
            kinds=["context_block"],
        )
        members = self._read_zip_members(pack_path)
        signature_payload = json.loads(members["signature/signature.json"].decode("utf-8"))
        signature_payload["signature_value"] = "00" * 32
        tampered = self.root / "phase200_tamper_signature" / "tampered_signature.zip"
        self._rewrite_zip(
            pack_path,
            tampered,
            replace_members={
                "signature/signature.json": (
                    json.dumps(signature_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode("utf-8")
            },
        )
        inspected = self._pack_inspect(pack_path=str(tampered), verification_secret=secret)
        sc = inspected["structuredContent"]
        self.assertEqual(str(sc["status"]), "invalid")
        self.assertEqual(str(sc["signature"]["trust_classification"]), "invalid_signature")

    def test_signature_tampered_member_fields_rejected(self) -> None:
        secret = "tamper-fields-secret-01234567890123456"
        source = self.record("phase200 tamper fields source", kind="context_block")
        topic = "phase200-tamper-fields-topic"
        server.topic_add({"memory_id": str(source["id"]), "topic": topic, "source": "operator"})
        pack_path, _ = self._create_signed_exported_pack(
            pack_name="phase200_tamper_fields",
            output_dir=self.root / "phase200_tamper_fields",
            signer_id="tamper.fields.signer",
            signing_secret=secret,
            topics=[topic],
            kinds=["context_block"],
        )
        members = self._read_zip_members(pack_path)
        manifest = json.loads(members["manifest.json"].decode("utf-8"))
        signature_payload = json.loads(members["signature/signature.json"].decode("utf-8"))

        cases: list[tuple[str, dict[str, Any], dict[str, Any], str]] = []

        sig_signer = dict(signature_payload)
        sig_signer["signer_id"] = "tampered.signer"
        cases.append(("signer_id", manifest, sig_signer, "invalid_signature"))

        sig_payload_version = dict(signature_payload)
        sig_payload_version["signature_payload_version"] = "tampered-v2"
        cases.append(("signature_payload_version", manifest, sig_payload_version, "invalid_signature"))

        sig_fingerprint = dict(signature_payload)
        sig_fingerprint["secret_fingerprint"] = "f" * 32
        cases.append(("secret_fingerprint", manifest, sig_fingerprint, "invalid_signature"))

        manifest_member = json.loads(json.dumps(manifest))
        manifest_member["signature"]["signature_member"] = "signature/other.json"
        cases.append(("signature_member_path", manifest_member, signature_payload, "unsupported_signature"))

        for label, manifest_payload, signature_payload_case, expected_classification in cases:
            with self.subTest(field=label):
                tampered = self.root / "phase200_tamper_fields" / f"tampered_{label}.zip"
                replace_members = {
                    "manifest.json": (
                        json.dumps(manifest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                    ).encode("utf-8"),
                    "signature/signature.json": (
                        json.dumps(signature_payload_case, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                    ).encode("utf-8"),
                }
                self._rewrite_zip(pack_path, tampered, replace_members=replace_members)
                inspected = self._pack_inspect(pack_path=str(tampered), verification_secret=secret)
                sc = inspected["structuredContent"]
                self.assertIn(str(sc["status"]), {"invalid", "unsupported"})
                self.assertEqual(str(sc["signature"]["trust_classification"]), expected_classification)

    def test_signature_payload_stable(self) -> None:
        manifest = {
            "pack_id": "pack_20260526T120000Z_a3f29b1c",
            "pack_schema_version": 1,
            "content_hash": {"value": "abc123"},
            "redaction_ruleset_version": "baseline-v1",
            "signature": {
                "signer_id": "payload.signer",
                "signature_algorithm": "hmac-sha256-local-v1",
                "secret_fingerprint": "1234567890abcdef1234567890abcdef",
            },
        }
        secret = "payload-secret-01234567890123456789012"
        sig1 = server._pack_sign_hmac_v1(manifest, secret)
        sig2 = server._pack_sign_hmac_v1(manifest, secret)
        self.assertEqual(sig1, sig2)

        manifest_changed = json.loads(json.dumps(manifest))
        manifest_changed["content_hash"]["value"] = "def456"
        self.assertNotEqual(sig1, server._pack_sign_hmac_v1(manifest_changed, secret))

        manifest_changed = json.loads(json.dumps(manifest))
        manifest_changed["signature"]["signer_id"] = "payload.signer.changed"
        self.assertNotEqual(sig1, server._pack_sign_hmac_v1(manifest_changed, secret))

        manifest_changed = json.loads(json.dumps(manifest))
        manifest_changed["signature"]["secret_fingerprint"] = "abcdefabcdefabcdefabcdefabcdefab"
        self.assertNotEqual(sig1, server._pack_sign_hmac_v1(manifest_changed, secret))

        old_version = server.PACK_SIGNATURE_PAYLOAD_VERSION_V1
        try:
            server.PACK_SIGNATURE_PAYLOAD_VERSION_V1 = "memory-pack-signing-v2"
            self.assertNotEqual(sig1, server._pack_sign_hmac_v1(manifest, secret))
        finally:
            server.PACK_SIGNATURE_PAYLOAD_VERSION_V1 = old_version

    def test_secret_not_logged_or_returned(self) -> None:
        secret = "secret-no-leak-0123456789012345678901"
        signer = self._signer_add(signer_id="no.leak.signer", secret=secret)
        source = self.record("phase200 no leak source", kind="context_block")
        topic = "phase200-no-leak-topic"
        server.topic_add({"memory_id": str(source["id"]), "topic": topic, "source": "operator"})
        pack_path, export_sc = self._create_signed_exported_pack(
            pack_name="phase200_no_leak",
            output_dir=self.root / "phase200_no_leak",
            signer_id="no.leak.signer",
            signing_secret=secret,
            topics=[topic],
            kinds=["context_block"],
        )
        inspected = self._pack_inspect(pack_path=str(pack_path), verification_secret=secret)
        blob = "\n".join(
            [
                json.dumps(signer.get("structuredContent", {}), ensure_ascii=False),
                json.dumps(export_sc, ensure_ascii=False),
                json.dumps(inspected.get("structuredContent", {}), ensure_ascii=False),
            ]
        )
        self.assertNotIn(secret, blob)
        self.assertNotIn("signing_secret", blob)
        self.assertNotIn("verification_secret", blob)

    def test_action_logging_scrubs_secret_params(self) -> None:
        secret = "log-scrub-secret-012345678901234567890"
        nested_secret = "nested-log-secret-0123456789012345678"
        server.append_query_log(
            "mnemo_search",
            {
                "query": "phase200 scrub",
                "signing_secret": secret,
                "nested": {"verification_secret": nested_secret, "inner": {"secret": secret}},
            },
            [],
        )
        server.append_event_log(
            "create",
            "mem_log_scrub",
            {"details": {"secret": secret, "verification_secret": nested_secret}},
        )
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            rows = conn.execute("SELECT data_json FROM events ORDER BY created_at ASC, rowid ASC").fetchall()
        finally:
            conn.close()
        payload = "\n".join(str(row[0]) for row in rows)
        self.assertNotIn(secret, payload)
        self.assertNotIn(nested_secret, payload)
        self.assertIn("[REDACTED]", payload)

    def test_migration_6_to_7_idempotent(self) -> None:
        self._reset_sqlite_file()
        server.load_store()
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            conn.execute("DROP TABLE IF EXISTS trusted_signers")
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', '6')")
            conn.commit()
        finally:
            conn.close()
        server._SQLITE_BOOTSTRAPPED.clear()
        server._SQLITE_SCHEMA_READY.clear()

        server.load_store()
        server.load_store()
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            schema_value = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            self.assertIsNotNone(schema_value)
            assert schema_value is not None
            self.assertEqual(int(schema_value[0]), 7)
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            self.assertIn("trusted_signers", tables)
        finally:
            conn.close()

    def test_docs_action_list_mentions_signing_lifecycle(self) -> None:
        readme = (Path(__file__).resolve().parent / "README.md").read_text(encoding="utf-8")
        tool_reference = (Path(__file__).resolve().parent / "docs" / "tool_reference.md").read_text(encoding="utf-8")
        combined = f"{readme}\n{tool_reference}"
        self.assertIn("signer_add", combined)
        self.assertIn("sign_pack", combined)
        self.assertIn("Memory Packs lifecycle recap", combined)

    def test_full_lifecycle_still_passes_unsigned(self) -> None:
        marker = "phase200_unsigned_lifecycle"
        seeded = self.record(f"{marker} source", kind="context_block", title=f"{marker} title")
        topic = f"{marker}-topic"
        server.topic_add({"memory_id": str(seeded["id"]), "topic": topic, "source": "operator"})
        pack_path = self._create_exported_pack(
            pack_name=marker,
            output_dir=self.root / marker,
            topics=[topic],
            kinds=["context_block"],
        )
        inspected = self._pack_inspect(pack_path=str(pack_path))
        self.assertEqual(str(inspected["structuredContent"]["status"]), "valid")
        self.assertEqual(str(inspected["structuredContent"]["signature"]["trust_classification"]), "unsigned")
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        pack_id = str(imported["structuredContent"]["pack_id"])
        row_id = str(self._pack_rows(pack_id)[0][0])
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        promoted_id = str(promoted["structuredContent"]["promoted_rows"][0]["promoted_memory_id"])
        searched = server.search_memories({"query": marker, "limit": 20})
        ids = {str(item["id"]) for item in searched["structuredContent"]["matches"]}
        self.assertIn(promoted_id, ids)

    def test_full_lifecycle_signed_quarantine_promote(self) -> None:
        marker = "phase200_signed_lifecycle"
        secret = "signed-lifecycle-secret-012345678901234"
        self._signer_add(signer_id="signed.lifecycle.signer", secret=secret, trust_level="trusted")
        first = self.record(
            f"{marker} context email test.user@example.test",
            kind="context_block",
            title=f"{marker} title context",
        )
        second = self.record(
            f"{marker} hip AWS AKIA1234567890ABCDEF",
            kind="hippocampus_entry",
            title=f"{marker} title hip",
        )
        topic = f"{marker}-topic"
        server.topic_add({"memory_id": str(first["id"]), "topic": topic, "source": "operator"})
        server.topic_add({"memory_id": str(second["id"]), "topic": topic, "source": "operator"})

        pack_path, _ = self._create_signed_exported_pack(
            pack_name=marker,
            output_dir=self.root / marker,
            signer_id="signed.lifecycle.signer",
            signing_secret=secret,
            topics=[topic],
            kinds=["context_block", "hippocampus_entry"],
            limit=100,
        )
        inspected = self._pack_inspect(pack_path=str(pack_path), verification_secret=secret)
        self.assertEqual(str(inspected["structuredContent"]["status"]), "valid")
        self.assertEqual(str(inspected["structuredContent"]["signature"]["trust_classification"]), "trusted_signer")
        self.assertEqual(str(inspected["structuredContent"]["import_recommendation"]), "quarantine_only")

        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        imported_sc = imported["structuredContent"]
        self.assertEqual(str(imported_sc["trust_level"]), "quarantine")
        pack_id = str(imported_sc["pack_id"])

        review = self._pack_review_import(pack_id=pack_id, include_samples=True, sample_limit=20)
        self.assertGreaterEqual(int(review["structuredContent"]["selection"]["selected_rows"]), 2)

        row_id = str(self._pack_rows(pack_id)[0][0])
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        promoted_id = str(promoted["structuredContent"]["promoted_rows"][0]["promoted_memory_id"])

        promoted_only = server.search_memories({"query": marker, "limit": 50, "origins": ["promoted"]})
        promoted_ids = {str(item["id"]) for item in promoted_only["structuredContent"]["matches"]}
        self.assertIn(promoted_id, promoted_ids)

    def test_signing_secret_scrubbing_recursive(self) -> None:
        payload = {
            "signing_secret": "abc",
            "outer": {
                "verification_secret": "def",
                "plain": "ok",
                "inner_list": [
                    {"secret": "ghi"},
                    {"items": [{"signing_secret": "jkl"}]},
                ],
            },
            "tuple_items": ({"secret": "mno"}, {"safe": "value"}),
        }
        scrubbed = server.scrub_secret_params(payload)
        self.assertEqual(scrubbed["signing_secret"], "[REDACTED]")
        self.assertEqual(scrubbed["outer"]["verification_secret"], "[REDACTED]")
        self.assertEqual(scrubbed["outer"]["plain"], "ok")
        self.assertEqual(scrubbed["outer"]["inner_list"][0]["secret"], "[REDACTED]")
        self.assertEqual(scrubbed["outer"]["inner_list"][1]["items"][0]["signing_secret"], "[REDACTED]")
        self.assertEqual(scrubbed["tuple_items"][0]["secret"], "[REDACTED]")
        self.assertEqual(scrubbed["tuple_items"][1]["safe"], "value")

    def test_signing_secret_not_returned_or_logged(self) -> None:
        secret = "stabilization-secret-not-logged-0123456789"
        wrong_secret = "stabilization-secret-wrong-value-01234567"
        self._signer_add(signer_id="stable.no.leak.signer", secret=secret, trust_level="trusted")
        row = self.record("phase201 secret no leak row", kind="context_block")
        topic = "phase201-secret-no-leak-topic"
        server.topic_add({"memory_id": str(row["id"]), "topic": topic, "source": "operator"})
        pack_path, export_sc = self._create_signed_exported_pack(
            pack_name="phase201_secret_no_leak",
            output_dir=self.root / "phase201_secret_no_leak",
            signer_id="stable.no.leak.signer",
            signing_secret=secret,
            topics=[topic],
            kinds=["context_block"],
        )
        inspected = self._pack_inspect(pack_path=str(pack_path), verification_secret=wrong_secret)
        server.append_query_log(
            "mnemo_search",
            {
                "query": "phase201 secret no leak",
                "signing_secret": secret,
                "verification_secret": wrong_secret,
                "nested": {"secret": secret},
            },
            [],
        )
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            rows = conn.execute("SELECT data_json FROM events ORDER BY created_at ASC, rowid ASC").fetchall()
        finally:
            conn.close()
        serialized = "\n".join(
            [
                json.dumps(export_sc, ensure_ascii=False),
                json.dumps(inspected["structuredContent"], ensure_ascii=False),
                "\n".join(str(item[0]) for item in rows),
            ]
        )
        self.assertNotIn(secret, serialized)
        self.assertNotIn(wrong_secret, serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_signing_classification_matrix(self) -> None:
        observed: dict[str, tuple[str, str, str]] = {}

        unsigned_row = self.record("phase201 unsigned matrix", kind="context_block")
        unsigned_topic = "phase201-matrix-unsigned-topic"
        server.topic_add({"memory_id": str(unsigned_row["id"]), "topic": unsigned_topic, "source": "operator"})
        unsigned_pack = self._create_exported_pack(
            pack_name="phase201_matrix_unsigned",
            output_dir=self.root / "phase201_matrix_unsigned",
            topics=[unsigned_topic],
            kinds=["context_block"],
        )
        unsigned_inspect = self._pack_inspect(pack_path=str(unsigned_pack))
        unsigned_sc = unsigned_inspect["structuredContent"]
        observed["unsigned"] = (
            str(unsigned_sc["signature"]["trust_classification"]),
            str(unsigned_sc["status"]),
            str(unsigned_sc["import_recommendation"]),
        )

        trusted_secret = "phase201-matrix-trusted-secret-0123456789"
        self._signer_add(signer_id="phase201.matrix.trusted", secret=trusted_secret, trust_level="trusted")
        trusted_row = self.record("phase201 trusted matrix", kind="context_block")
        trusted_topic = "phase201-matrix-trusted-topic"
        server.topic_add({"memory_id": str(trusted_row["id"]), "topic": trusted_topic, "source": "operator"})
        trusted_pack, _ = self._create_signed_exported_pack(
            pack_name="phase201_matrix_trusted",
            output_dir=self.root / "phase201_matrix_trusted",
            signer_id="phase201.matrix.trusted",
            signing_secret=trusted_secret,
            topics=[trusted_topic],
            kinds=["context_block"],
        )
        trusted_no_secret = self._pack_inspect(pack_path=str(trusted_pack))
        trusted_no_secret_sc = trusted_no_secret["structuredContent"]
        observed["signature_not_verified"] = (
            str(trusted_no_secret_sc["signature"]["trust_classification"]),
            str(trusted_no_secret_sc["status"]),
            str(trusted_no_secret_sc["import_recommendation"]),
        )
        trusted_verified = self._pack_inspect(pack_path=str(trusted_pack), verification_secret=trusted_secret)
        trusted_verified_sc = trusted_verified["structuredContent"]
        observed["trusted_signer"] = (
            str(trusted_verified_sc["signature"]["trust_classification"]),
            str(trusted_verified_sc["status"]),
            str(trusted_verified_sc["import_recommendation"]),
        )
        trusted_wrong_secret = self._pack_inspect(
            pack_path=str(trusted_pack),
            verification_secret="phase201-matrix-wrong-secret-012345678",
        )
        trusted_wrong_secret_sc = trusted_wrong_secret["structuredContent"]
        observed["invalid_signature"] = (
            str(trusted_wrong_secret_sc["signature"]["trust_classification"]),
            str(trusted_wrong_secret_sc["status"]),
            str(trusted_wrong_secret_sc["import_recommendation"]),
        )

        unknown_secret = "phase201-matrix-unknown-secret-0123456789"
        unknown_row = self.record("phase201 unknown matrix", kind="context_block")
        unknown_topic = "phase201-matrix-unknown-topic"
        server.topic_add({"memory_id": str(unknown_row["id"]), "topic": unknown_topic, "source": "operator"})
        unknown_pack, _ = self._create_signed_exported_pack(
            pack_name="phase201_matrix_unknown",
            output_dir=self.root / "phase201_matrix_unknown",
            signer_id="phase201.matrix.unknown",
            signing_secret=unknown_secret,
            topics=[unknown_topic],
            kinds=["context_block"],
        )
        unknown_inspect = self._pack_inspect(pack_path=str(unknown_pack), verification_secret=unknown_secret)
        unknown_sc = unknown_inspect["structuredContent"]
        observed["unknown_signer"] = (
            str(unknown_sc["signature"]["trust_classification"]),
            str(unknown_sc["status"]),
            str(unknown_sc["import_recommendation"]),
        )

        blocked_secret = "phase201-matrix-blocked-secret-0123456789"
        self._signer_add(signer_id="phase201.matrix.blocked", secret=blocked_secret, trust_level="blocked")
        blocked_row = self.record("phase201 blocked matrix", kind="context_block")
        blocked_topic = "phase201-matrix-blocked-topic"
        server.topic_add({"memory_id": str(blocked_row["id"]), "topic": blocked_topic, "source": "operator"})
        blocked_pack, _ = self._create_signed_exported_pack(
            pack_name="phase201_matrix_blocked",
            output_dir=self.root / "phase201_matrix_blocked",
            signer_id="phase201.matrix.blocked",
            signing_secret=blocked_secret,
            topics=[blocked_topic],
            kinds=["context_block"],
        )
        blocked_inspect = self._pack_inspect(pack_path=str(blocked_pack), verification_secret=blocked_secret)
        blocked_sc = blocked_inspect["structuredContent"]
        observed["blocked_signer"] = (
            str(blocked_sc["signature"]["trust_classification"]),
            str(blocked_sc["status"]),
            str(blocked_sc["import_recommendation"]),
        )

        disabled_secret = "phase201-matrix-disabled-secret-012345678"
        self._signer_add(signer_id="phase201.matrix.disabled", secret=disabled_secret, trust_level="trusted")
        self._signer_disable(signer_id="phase201.matrix.disabled")
        disabled_row = self.record("phase201 disabled matrix", kind="context_block")
        disabled_topic = "phase201-matrix-disabled-topic"
        server.topic_add({"memory_id": str(disabled_row["id"]), "topic": disabled_topic, "source": "operator"})
        disabled_pack, _ = self._create_signed_exported_pack(
            pack_name="phase201_matrix_disabled",
            output_dir=self.root / "phase201_matrix_disabled",
            signer_id="phase201.matrix.disabled",
            signing_secret=disabled_secret,
            topics=[disabled_topic],
            kinds=["context_block"],
        )
        disabled_inspect = self._pack_inspect(pack_path=str(disabled_pack), verification_secret=disabled_secret)
        disabled_sc = disabled_inspect["structuredContent"]
        observed["disabled_signer"] = (
            str(disabled_sc["signature"]["trust_classification"]),
            str(disabled_sc["status"]),
            str(disabled_sc["import_recommendation"]),
        )

        mismatch_secret_a = "phase201-matrix-mismatch-secret-a-0123456"
        mismatch_secret_b = "phase201-matrix-mismatch-secret-b-0123456"
        self._signer_add(signer_id="phase201.matrix.mismatch", secret=mismatch_secret_a, trust_level="trusted")
        mismatch_row = self.record("phase201 mismatch matrix", kind="context_block")
        mismatch_topic = "phase201-matrix-mismatch-topic"
        server.topic_add({"memory_id": str(mismatch_row["id"]), "topic": mismatch_topic, "source": "operator"})
        mismatch_pack, _ = self._create_signed_exported_pack(
            pack_name="phase201_matrix_mismatch",
            output_dir=self.root / "phase201_matrix_mismatch",
            signer_id="phase201.matrix.mismatch",
            signing_secret=mismatch_secret_b,
            topics=[mismatch_topic],
            kinds=["context_block"],
        )
        mismatch_inspect = self._pack_inspect(pack_path=str(mismatch_pack), verification_secret=mismatch_secret_b)
        mismatch_sc = mismatch_inspect["structuredContent"]
        observed["secret_fingerprint_mismatch"] = (
            str(mismatch_sc["signature"]["trust_classification"]),
            str(mismatch_sc["status"]),
            str(mismatch_sc["import_recommendation"]),
        )

        members = self._read_zip_members(trusted_pack)
        tampered_manifest = json.loads(members["manifest.json"].decode("utf-8"))
        tampered_signature = json.loads(members[server.PACK_SIGNATURE_MEMBER].decode("utf-8"))
        tampered_manifest["signature"]["signature_algorithm"] = "hmac-sha512-local-v1"
        tampered_signature["signature_algorithm"] = "hmac-sha512-local-v1"
        unsupported_path = self.root / "phase201_matrix_trusted" / "unsupported_signature_algorithm.zip"
        self._rewrite_zip(
            trusted_pack,
            unsupported_path,
            replace_members={
                "manifest.json": (
                    json.dumps(tampered_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode("utf-8"),
                server.PACK_SIGNATURE_MEMBER: (
                    json.dumps(tampered_signature, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode("utf-8"),
            },
        )
        unsupported_inspect = self._pack_inspect(pack_path=str(unsupported_path), verification_secret=trusted_secret)
        unsupported_sc = unsupported_inspect["structuredContent"]
        observed["unsupported_signature"] = (
            str(unsupported_sc["signature"]["trust_classification"]),
            str(unsupported_sc["status"]),
            str(unsupported_sc["import_recommendation"]),
        )

        expected = {
            "unsigned": ("unsigned", "valid", "quarantine_only"),
            "signature_not_verified": ("signature_not_verified", "valid", "quarantine_only"),
            "trusted_signer": ("trusted_signer", "valid", "quarantine_only"),
            "unknown_signer": ("unknown_signer", "valid", "quarantine_only"),
            "disabled_signer": ("disabled_signer", "invalid", "reject"),
            "blocked_signer": ("blocked_signer", "invalid", "reject"),
            "invalid_signature": ("invalid_signature", "invalid", "reject"),
            "secret_fingerprint_mismatch": ("secret_fingerprint_mismatch", "invalid", "reject"),
            "unsupported_signature": ("unsupported_signature", "unsupported", "reject"),
        }
        for key, value in expected.items():
            with self.subTest(classification=key):
                self.assertEqual(observed.get(key), value)

    def test_signing_tamper_cases(self) -> None:
        secret = "phase201-tamper-secret-01234567890123456"
        source = self.record("phase201 tamper matrix source", kind="context_block")
        topic = "phase201-tamper-matrix-topic"
        server.topic_add({"memory_id": str(source["id"]), "topic": topic, "source": "operator"})
        pack_path, _ = self._create_signed_exported_pack(
            pack_name="phase201_tamper_matrix",
            output_dir=self.root / "phase201_tamper_matrix",
            signer_id="phase201.tamper.signer",
            signing_secret=secret,
            topics=[topic],
            kinds=["context_block"],
        )
        members = self._read_zip_members(pack_path)
        manifest = json.loads(members["manifest.json"].decode("utf-8"))
        signature_payload = json.loads(members[server.PACK_SIGNATURE_MEMBER].decode("utf-8"))

        cases: list[tuple[str, dict[str, Any], str, str, set[str]]] = []

        cases.append(
            (
                "tamper_content_member",
                {"content/memories.jsonl": b"{bad-json}\n"},
                "invalid",
                "reject",
                {"trusted_signer", "unknown_signer", "signature_not_verified", "invalid_signature"},
            )
        )

        tampered_signature_value = json.loads(json.dumps(signature_payload))
        tampered_signature_value["signature_value"] = "00" * 32
        cases.append(
            (
                "tamper_signature_value",
                {
                    server.PACK_SIGNATURE_MEMBER: (
                        json.dumps(tampered_signature_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                    ).encode("utf-8")
                },
                "invalid",
                "reject",
                {"invalid_signature"},
            )
        )

        tampered_signature_signer = json.loads(json.dumps(signature_payload))
        tampered_signature_signer["signer_id"] = "phase201.tamper.changed"
        cases.append(
            (
                "tamper_signature_signer_id",
                {
                    server.PACK_SIGNATURE_MEMBER: (
                        json.dumps(tampered_signature_signer, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                    ).encode("utf-8")
                },
                "invalid",
                "reject",
                {"invalid_signature"},
            )
        )

        tampered_manifest_signer = json.loads(json.dumps(manifest))
        tampered_manifest_signer["signature"]["signer_id"] = "phase201.tamper.changed.manifest"
        cases.append(
            (
                "tamper_manifest_signer_id",
                {
                    "manifest.json": (
                        json.dumps(tampered_manifest_signer, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                    ).encode("utf-8")
                },
                "invalid",
                "reject",
                {"invalid_signature"},
            )
        )

        tampered_payload_version = json.loads(json.dumps(manifest))
        tampered_payload_version_sig = json.loads(json.dumps(signature_payload))
        tampered_payload_version["signature"]["signature_payload_version"] = "memory-pack-signing-v2"
        tampered_payload_version_sig["signature_payload_version"] = "memory-pack-signing-v2"
        cases.append(
            (
                "tamper_payload_version",
                {
                    "manifest.json": (
                        json.dumps(tampered_payload_version, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                    ).encode("utf-8"),
                    server.PACK_SIGNATURE_MEMBER: (
                        json.dumps(tampered_payload_version_sig, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                    ).encode("utf-8"),
                },
                "unsupported",
                "reject",
                {"unsupported_signature"},
            )
        )

        tampered_algorithm = json.loads(json.dumps(manifest))
        tampered_algorithm_sig = json.loads(json.dumps(signature_payload))
        tampered_algorithm["signature"]["signature_algorithm"] = "hmac-sha512-local-v1"
        tampered_algorithm_sig["signature_algorithm"] = "hmac-sha512-local-v1"
        cases.append(
            (
                "tamper_signature_algorithm",
                {
                    "manifest.json": (
                        json.dumps(tampered_algorithm, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                    ).encode("utf-8"),
                    server.PACK_SIGNATURE_MEMBER: (
                        json.dumps(tampered_algorithm_sig, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                    ).encode("utf-8"),
                },
                "unsupported",
                "reject",
                {"unsupported_signature"},
            )
        )

        tampered_fingerprint = json.loads(json.dumps(signature_payload))
        tampered_fingerprint["secret_fingerprint"] = "f" * 32
        cases.append(
            (
                "tamper_secret_fingerprint",
                {
                    server.PACK_SIGNATURE_MEMBER: (
                        json.dumps(tampered_fingerprint, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                    ).encode("utf-8")
                },
                "invalid",
                "reject",
                {"invalid_signature", "secret_fingerprint_mismatch"},
            )
        )

        tampered_member_path = json.loads(json.dumps(manifest))
        tampered_member_path["signature"]["signature_member"] = "signature/other.json"
        cases.append(
            (
                "tamper_signature_member_path",
                {
                    "manifest.json": (
                        json.dumps(tampered_member_path, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                    ).encode("utf-8")
                },
                "unsupported",
                "reject",
                {"unsupported_signature"},
            )
        )

        for label, replace_members, expected_status, expected_recommendation, expected_classifications in cases:
            with self.subTest(case=label):
                tampered = self.root / "phase201_tamper_matrix" / f"{label}.zip"
                self._rewrite_zip(pack_path, tampered, replace_members=replace_members)
                inspected = self._pack_inspect(pack_path=str(tampered), verification_secret=secret)
                sc = inspected["structuredContent"]
                self.assertEqual(str(sc["status"]), expected_status)
                self.assertEqual(str(sc["import_recommendation"]), expected_recommendation)
                self.assertIn(str(sc["signature"]["trust_classification"]), expected_classifications)

        missing_signature_member = self.root / "phase201_tamper_matrix" / "missing_signature_member.zip"
        self._rewrite_zip(pack_path, missing_signature_member, remove_members={server.PACK_SIGNATURE_MEMBER})
        missing_inspect = self._pack_inspect(pack_path=str(missing_signature_member), verification_secret=secret)
        missing_sc = missing_inspect["structuredContent"]
        self.assertEqual(str(missing_sc["status"]), "invalid")
        self.assertEqual(str(missing_sc["import_recommendation"]), "reject")
        self.assertEqual(str(missing_sc["signature"]["trust_classification"]), "invalid_signature")

        unsigned_row = self.record("phase201 unsigned extra signature source", kind="context_block")
        unsigned_topic = "phase201-unsigned-extra-signature-topic"
        server.topic_add({"memory_id": str(unsigned_row["id"]), "topic": unsigned_topic, "source": "operator"})
        unsigned_pack = self._create_exported_pack(
            pack_name="phase201_unsigned_extra_signature",
            output_dir=self.root / "phase201_unsigned_extra_signature",
            topics=[unsigned_topic],
            kinds=["context_block"],
        )
        unsigned_extra = self.root / "phase201_unsigned_extra_signature" / "unsigned_with_signature_member.zip"
        self._rewrite_zip(
            unsigned_pack,
            unsigned_extra,
            extra_members={
                server.PACK_SIGNATURE_MEMBER: (
                    json.dumps(
                        {
                            "signature_schema_version": 1,
                            "signature_algorithm": server.PACK_SIGNATURE_ALGORITHM_HMAC_LOCAL,
                            "signature_payload_version": server.PACK_SIGNATURE_PAYLOAD_VERSION_V1,
                            "signer_id": "fake.signer",
                            "secret_fingerprint": "0" * 32,
                            "signed_at": "2026-05-26T00:00:00Z",
                            "signature_value": "0" * 64,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
            },
        )
        unsigned_extra_inspect = self._pack_inspect(pack_path=str(unsigned_extra))
        unsigned_extra_sc = unsigned_extra_inspect["structuredContent"]
        self.assertEqual(str(unsigned_extra_sc["status"]), "valid")
        self.assertEqual(str(unsigned_extra_sc["import_recommendation"]), "quarantine_only")
        self.assertEqual(str(unsigned_extra_sc["signature"]["trust_classification"]), "unsigned")

    def test_signer_registry_state_cycle(self) -> None:
        secret = "phase201-state-cycle-secret-01234567890123"
        self._signer_add(signer_id="phase201.state.cycle", secret=secret, trust_level="trusted")
        disabled = self._signer_disable(signer_id="phase201.state.cycle")
        self.assertEqual(str(disabled["structuredContent"]["signer_status"]), "disabled")
        enabled = self._signer_enable(signer_id="phase201.state.cycle")
        self.assertEqual(str(enabled["structuredContent"]["signer_status"]), "active")

        disable_missing = self._signer_disable_error(signer_id="phase201.missing.signer")
        self.assertEqual(self._pack_error_code(disable_missing), "signer_not_found")
        enable_missing = self._signer_enable_error(signer_id="phase201.missing.signer")
        self.assertEqual(self._pack_error_code(enable_missing), "signer_not_found")

        invalid_signer_id = self._signer_add_error(signer_id="no spaces allowed", secret=secret)
        self.assertIn("signer_id must match", str(invalid_signer_id["content"][0]["text"]))
        unicode_signer_id = self._signer_add_error(signer_id="žsigner", secret=secret)
        self.assertIn("signer_id must match", str(unicode_signer_id["content"][0]["text"]))

    def test_migration_7_idempotent(self) -> None:
        self._reset_sqlite_file()
        server.load_store()
        self._signer_add(signer_id="phase201.migration.signer", secret="phase201-migration-secret-012345678901")
        source = self.record("phase201 migration row", kind="context_block")
        server.topic_add({"memory_id": str(source["id"]), "topic": "phase201-migration-topic", "source": "operator"})
        export = self._pack_export(
            pack_name="phase201_migration_pack",
            output_dir=str(self.root / "phase201_migration_pack"),
            allow_unsigned=True,
            topics=["phase201-migration-topic"],
            kinds=["context_block"],
        )
        self.assertFalse(export["isError"], export)

        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            signer_rows_before = conn.execute("SELECT COUNT(*) FROM trusted_signers").fetchone()[0]
            imported_before = conn.execute("SELECT COUNT(*) FROM imported_packs").fetchone()[0]
            exported_before = conn.execute("SELECT COUNT(*) FROM exported_packs").fetchone()[0]
        finally:
            conn.close()

        server._SQLITE_BOOTSTRAPPED.clear()
        server._SQLITE_SCHEMA_READY.clear()
        server.load_store()
        server._SQLITE_BOOTSTRAPPED.clear()
        server._SQLITE_SCHEMA_READY.clear()
        server.load_store()

        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            schema_value = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            self.assertIsNotNone(schema_value)
            assert schema_value is not None
            self.assertEqual(int(schema_value[0]), 7)

            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertIn("trusted_signers", tables)

            indexes = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
            self.assertIn("idx_trusted_signers_status", indexes)
            self.assertIn("idx_trusted_signers_trust_level", indexes)

            signer_rows_after = conn.execute("SELECT COUNT(*) FROM trusted_signers").fetchone()[0]
            imported_after = conn.execute("SELECT COUNT(*) FROM imported_packs").fetchone()[0]
            exported_after = conn.execute("SELECT COUNT(*) FROM exported_packs").fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(int(signer_rows_before), int(signer_rows_after))
        self.assertEqual(int(imported_before), int(imported_after))
        self.assertEqual(int(exported_before), int(exported_after))

    def test_signed_pack_import_remains_quarantine_only(self) -> None:
        secret = "phase201-import-policy-secret-012345678901"
        self._signer_add(signer_id="phase201.import.policy", secret=secret, trust_level="trusted")
        source = self.record("phase201 import policy row", kind="context_block")
        topic = "phase201-import-policy-topic"
        server.topic_add({"memory_id": str(source["id"]), "topic": topic, "source": "operator"})
        pack_path, _ = self._create_signed_exported_pack(
            pack_name="phase201_import_policy_pack",
            output_dir=self.root / "phase201_import_policy_pack",
            signer_id="phase201.import.policy",
            signing_secret=secret,
            topics=[topic],
            kinds=["context_block"],
        )
        inspected = self._pack_inspect(pack_path=str(pack_path), verification_secret=secret)
        self.assertEqual(str(inspected["structuredContent"]["signature"]["trust_classification"]), "trusted_signer")
        self.assertEqual(str(inspected["structuredContent"]["import_recommendation"]), "quarantine_only")

        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        imported_sc = imported["structuredContent"]
        self.assertEqual(str(imported_sc["trust_level"]), "quarantine")
        self.assertTrue(str(imported_sc["namespace"]).startswith("pack:quarantine:"))
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            row = conn.execute(
                "SELECT trust_level, namespace FROM imported_packs WHERE pack_id = ?",
                (str(imported_sc["pack_id"]),),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(str(row[0]), "quarantine")
        self.assertTrue(str(row[1]).startswith("pack:quarantine:"))

    def test_pack_export_signed_unsigned_regression(self) -> None:
        marker = "phase201_export_regression"
        row = self.record(f"{marker} source", kind="context_block")
        topic = f"{marker}-topic"
        server.topic_add({"memory_id": str(row["id"]), "topic": topic, "source": "operator"})

        unsigned_pack = self._create_exported_pack(
            pack_name=f"{marker}_unsigned",
            output_dir=self.root / f"{marker}_unsigned",
            topics=[topic],
            kinds=["context_block"],
        )
        unsigned_members = self._read_zip_members(unsigned_pack)
        unsigned_manifest = json.loads(unsigned_members["manifest.json"].decode("utf-8"))
        self.assertEqual(int(unsigned_manifest.get("pack_schema_version", 0)), 1)
        self.assertFalse(bool(unsigned_manifest.get("signed")))
        self.assertIn(str(unsigned_manifest.get("unsigned_reason", "")), {"operator_chose_unsigned", "signing_not_implemented"})
        self.assertNotIn(server.PACK_SIGNATURE_MEMBER, unsigned_members)

        secret = "phase201-export-signed-secret-0123456789012"
        signed_pack, signed_sc = self._create_signed_exported_pack(
            pack_name=f"{marker}_signed",
            output_dir=self.root / f"{marker}_signed",
            signer_id="phase201.export.regression",
            signing_secret=secret,
            topics=[topic],
            kinds=["context_block"],
        )
        warning_codes = self._pack_warning_codes({"structuredContent": signed_sc})
        self.assertIn("local_hmac_not_public_key", warning_codes)

        signed_members = self._read_zip_members(signed_pack)
        signed_manifest = json.loads(signed_members["manifest.json"].decode("utf-8"))
        self.assertTrue(bool(signed_manifest.get("signed")))
        self.assertIn("signature", signed_manifest)
        self.assertIn(server.PACK_SIGNATURE_MEMBER, signed_members)
        self.assertNotIn(secret, "\n".join(blob.decode("utf-8", errors="ignore") for blob in signed_members.values()))
        covered_members = list((signed_manifest.get("content_hash", {}) or {}).get("covered_members", []))
        self.assertNotIn(server.PACK_SIGNATURE_MEMBER, covered_members)

    def test_pack_inspect_signature_regression(self) -> None:
        marker = "phase201_inspect_signature_regression"
        row = self.record(f"{marker} row", kind="context_block")
        topic = f"{marker}-topic"
        server.topic_add({"memory_id": str(row["id"]), "topic": topic, "source": "operator"})

        unsigned_pack = self._create_exported_pack(
            pack_name=f"{marker}_unsigned",
            output_dir=self.root / f"{marker}_unsigned",
            topics=[topic],
            kinds=["context_block"],
        )
        unsigned_inspect = self._pack_inspect(
            pack_path=str(unsigned_pack),
            verification_secret="phase201-inspect-regression-secret-123456789",
        )
        self.assertIn("verification_secret_unused_for_unsigned_pack", self._pack_warning_codes(unsigned_inspect))

        secret = "phase201-inspect-regression-signed-secret-012"
        self._signer_add(signer_id="phase201.inspect.regression", secret=secret, trust_level="trusted")
        signed_pack, _ = self._create_signed_exported_pack(
            pack_name=f"{marker}_signed",
            output_dir=self.root / f"{marker}_signed",
            signer_id="phase201.inspect.regression",
            signing_secret=secret,
            topics=[topic],
            kinds=["context_block"],
        )

        before = self._read_only_snapshot()
        signed_no_secret = self._pack_inspect(pack_path=str(signed_pack))
        signed_verified = self._pack_inspect(pack_path=str(signed_pack), verification_secret=secret)
        signed_wrong = self._pack_inspect(
            pack_path=str(signed_pack),
            verification_secret="phase201-inspect-regression-wrong-secret-12",
        )
        after = self._read_only_snapshot()

        self.assertEqual(before["counts"], after["counts"])
        self.assertEqual(before["digests"], after["digests"])

        self.assertIn("signature_not_verified", self._pack_warning_codes(signed_no_secret))
        self.assertIn("local_hmac_not_public_key", self._pack_warning_codes(signed_no_secret))
        self.assertTrue(bool(signed_verified["structuredContent"]["signature"]["verified"]))
        self.assertEqual(
            str(signed_verified["structuredContent"]["signature"]["trust_classification"]),
            "trusted_signer",
        )
        self.assertEqual(str(signed_wrong["structuredContent"]["status"]), "invalid")
        self.assertEqual(
            str(signed_wrong["structuredContent"]["signature"]["trust_classification"]),
            "invalid_signature",
        )

        members = self._read_zip_members(signed_pack)
        manifest = json.loads(members["manifest.json"].decode("utf-8"))
        signature_payload = json.loads(members[server.PACK_SIGNATURE_MEMBER].decode("utf-8"))
        manifest["signature"]["signature_algorithm"] = "hmac-sha512-local-v1"
        signature_payload["signature_algorithm"] = "hmac-sha512-local-v1"
        unsupported = self.root / f"{marker}_signed" / "unsupported_signature_algorithm.zip"
        self._rewrite_zip(
            signed_pack,
            unsupported,
            replace_members={
                "manifest.json": (
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode("utf-8"),
                server.PACK_SIGNATURE_MEMBER: (
                    json.dumps(signature_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode("utf-8"),
            },
        )
        unsupported_inspect = self._pack_inspect(pack_path=str(unsupported), verification_secret=secret)
        self.assertEqual(str(unsupported_inspect["structuredContent"]["status"]), "unsupported")
        self.assertEqual(
            str(unsupported_inspect["structuredContent"]["signature"]["trust_classification"]),
            "unsupported_signature",
        )

    def test_docs_signing_lifecycle_recap(self) -> None:
        readme = (Path(__file__).resolve().parent / "README.md").read_text(encoding="utf-8")
        tool_reference = (Path(__file__).resolve().parent / "docs" / "tool_reference.md").read_text(encoding="utf-8")
        combined = f"{readme}\n{tool_reference}"
        self.assertIn("signer_add", combined)
        self.assertIn("signer_list", combined)
        self.assertIn("sign_pack", combined)
        self.assertIn("verification_secret", combined)
        self.assertIn("allow_trusted_import", combined)
        self.assertIn("trusted import policy", combined.lower())
        self.assertIn("not public-key signing", combined.lower())
        self.assertIn("not non-repudiation", combined.lower())

    def test_trusted_import_policy_docs_present(self) -> None:
        readme = (Path(__file__).resolve().parent / "README.md").read_text(encoding="utf-8")
        tool_reference = (Path(__file__).resolve().parent / "docs" / "tool_reference.md").read_text(encoding="utf-8")
        combined = f"{readme}\n{tool_reference}"
        self.assertIn("Trusted Import Policy", readme)
        self.assertIn("Trusted Import Policy", tool_reference)
        self.assertIn("Trusted import is NOT local adoption", combined)
        self.assertIn("Trusted import is NOT automatic promotion", combined)
        self.assertIn("Trusted import is NOT default retrieval", combined)
        self.assertIn("pack:trusted:<pack_id>", combined)
        self.assertIn("manual promotion remains explicit", combined.lower())

    def test_retrieval_semantics_include_flags_after_5b(self) -> None:
        marker = "phase210-flag-matrix-marker"
        trusted_ns = "pack:trusted:phase210-flag-trusted"
        quarantine_ns = "pack:quarantine:phase210-flag-quarantine"
        self._insert_imported_pack(pack_id="phase210-flag-trusted", trust_level="trusted", namespace=trusted_ns)
        self._insert_imported_pack(pack_id="phase210-flag-quarantine", trust_level="quarantine", namespace=quarantine_ns)
        trusted_row = self.record(f"{marker} trusted", kind="note", namespace=trusted_ns, origin="imported")
        quarantine_row = self.record(f"{marker} quarantine", kind="note", namespace=quarantine_ns, origin="imported")

        default = server.search_memories({"query": marker, "limit": 20})
        imported_only = server.search_memories({"query": marker, "limit": 20, "include_imported": True})
        quarantine_only = server.search_memories({"query": marker, "limit": 20, "include_quarantine": True})
        both = server.search_memories({"query": marker, "limit": 20, "include_imported": True, "include_quarantine": True})
        explicit_trusted = server.search_memories(
            {"query": marker, "limit": 20, "namespace": trusted_ns, "include_quarantine": True}
        )
        explicit_quarantine = server.search_memories(
            {"query": marker, "limit": 20, "namespace": quarantine_ns, "include_imported": True}
        )

        default_ids = {str(item["id"]) for item in default["structuredContent"]["matches"]}
        imported_ids = {str(item["id"]) for item in imported_only["structuredContent"]["matches"]}
        quarantine_ids = {str(item["id"]) for item in quarantine_only["structuredContent"]["matches"]}
        both_ids = {str(item["id"]) for item in both["structuredContent"]["matches"]}
        explicit_trusted_ids = {str(item["id"]) for item in explicit_trusted["structuredContent"]["matches"]}
        explicit_quarantine_ids = {str(item["id"]) for item in explicit_quarantine["structuredContent"]["matches"]}

        self.assertNotIn(str(trusted_row["id"]), default_ids)
        self.assertNotIn(str(quarantine_row["id"]), default_ids)
        self.assertIn(str(trusted_row["id"]), imported_ids)
        self.assertNotIn(str(quarantine_row["id"]), imported_ids)
        self.assertIn(str(quarantine_row["id"]), quarantine_ids)
        self.assertNotIn(str(trusted_row["id"]), quarantine_ids)
        self.assertIn(str(trusted_row["id"]), both_ids)
        self.assertIn(str(quarantine_row["id"]), both_ids)
        self.assertEqual(explicit_trusted_ids, {str(trusted_row["id"])})
        self.assertEqual(explicit_quarantine_ids, {str(quarantine_row["id"])})

    def test_include_imported_only_returns_trusted_namespaces_after_5b(self) -> None:
        marker = "phase210-include-imported-trusted-only"
        trusted_ns = "pack:trusted:phase210-imported-only-trusted"
        quarantine_ns = "pack:quarantine:phase210-imported-only-quarantine"
        self._insert_imported_pack(pack_id="phase210-imported-only-trusted", trust_level="trusted", namespace=trusted_ns)
        self._insert_imported_pack(
            pack_id="phase210-imported-only-quarantine",
            trust_level="quarantine",
            namespace=quarantine_ns,
        )
        trusted_row = self.record(f"{marker} trusted", kind="note", namespace=trusted_ns, origin="imported")
        quarantine_row = self.record(f"{marker} quarantine", kind="note", namespace=quarantine_ns, origin="imported")
        result = server.search_memories({"query": marker, "limit": 20, "include_imported": True})
        ids = {str(item["id"]) for item in result["structuredContent"]["matches"]}
        self.assertIn(str(trusted_row["id"]), ids)
        self.assertNotIn(str(quarantine_row["id"]), ids)

    def test_pack_import_requires_one_import_target(self) -> None:
        row = self.record("phase210 import target policy", kind="context_block")
        topic = "phase210-import-target-policy-topic"
        server.topic_add({"memory_id": str(row["id"]), "topic": topic, "source": "operator"})
        pack_path = self._create_exported_pack(
            pack_name="phase210_import_target_policy",
            output_dir=self.root / "phase210_import_target_policy",
            topics=[topic],
            kinds=["context_block"],
        )
        before = self._table_count("imported_packs")

        neither = self._pack_import_error(pack_path=str(pack_path))
        self.assertEqual(self._pack_error_code(neither), "import_target_not_allowed")
        both = self._pack_import_error(
            pack_path=str(pack_path),
            allow_unsigned_quarantine=True,
            allow_trusted_import=True,
            verification_secret="phase210-both-targets-secret-0123456789012",
        )
        self.assertEqual(self._pack_error_code(both), "ambiguous_import_target")
        self.assertEqual(before, self._table_count("imported_packs"))

    def test_pack_import_trusted_requires_verification_secret(self) -> None:
        fixture = self._create_signed_pack_fixture(marker="phase210-trusted-requires-secret")
        failed = self._pack_import_error(pack_path=str(fixture["pack_path"]), allow_trusted_import=True)
        self.assertEqual(self._pack_error_code(failed), "trusted_import_requires_verification_secret")

    def test_pack_import_trusted_rejects_short_verification_secret(self) -> None:
        fixture = self._create_signed_pack_fixture(marker="phase210-trusted-short-secret")
        failed = self._pack_import_error(
            pack_path=str(fixture["pack_path"]),
            allow_trusted_import=True,
            verification_secret="short",
        )
        self.assertEqual(self._pack_error_code(failed), "secret_too_short")
        error_payload = failed.get("structuredContent", {}).get("error", {})
        self.assertEqual(str(error_payload.get("field", "")), "verification_secret")

    def test_pack_import_trusted_signed_pack_success(self) -> None:
        fixture = self._create_signed_pack_fixture(marker="phase210-trusted-import-success")
        imported = self._pack_import(
            pack_path=str(fixture["pack_path"]),
            allow_trusted_import=True,
            verification_secret=str(fixture["secret"]),
        )
        sc = imported["structuredContent"]
        self.assertEqual(str(sc["trust_level"]), "trusted")
        self.assertTrue(str(sc["namespace"]).startswith("pack:trusted:"))
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            row = conn.execute(
                "SELECT trust_level, namespace FROM imported_packs WHERE pack_id = ?",
                (str(sc["pack_id"]),),
            ).fetchone()
            memories = conn.execute(
                "SELECT namespace, origin FROM memories WHERE id IN (SELECT memory_id FROM imported_pack_rows WHERE pack_id = ?)",
                (str(sc["pack_id"]),),
            ).fetchall()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(str(row[0]), "trusted")
        self.assertTrue(str(row[1]).startswith("pack:trusted:"))
        self.assertTrue(all(str(item[0]).startswith("pack:trusted:") for item in memories))
        self.assertTrue(all(str(item[1]) == "imported" for item in memories))

    def test_pack_import_trusted_does_not_create_local_rows(self) -> None:
        fixture = self._create_signed_pack_fixture(marker="phase210-trusted-no-local")
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            before_local_namespace = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE namespace = ?",
                    (server.DEFAULT_MEMORY_NAMESPACE,),
                ).fetchone()[0]
            )
            before_promoted = int(conn.execute("SELECT COUNT(*) FROM memories WHERE origin = 'promoted'").fetchone()[0])
        finally:
            conn.close()
        imported = self._pack_import(
            pack_path=str(fixture["pack_path"]),
            allow_trusted_import=True,
            verification_secret=str(fixture["secret"]),
        )
        sc = imported["structuredContent"]
        self.assertEqual(str(sc["trust_level"]), "trusted")
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            after_local_namespace = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE namespace = ?",
                    (server.DEFAULT_MEMORY_NAMESPACE,),
                ).fetchone()[0]
            )
            after_promoted = int(conn.execute("SELECT COUNT(*) FROM memories WHERE origin = 'promoted'").fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(before_local_namespace, after_local_namespace)
        self.assertEqual(before_promoted, after_promoted)

    def test_pack_import_trusted_rejects_unverified_or_unknown(self) -> None:
        unknown_secret = "phase210-unknown-signer-secret-01234567890123"
        source = self.record("phase210 unknown signer trusted import", kind="context_block")
        topic = "phase210-unknown-signer-topic"
        server.topic_add({"memory_id": str(source["id"]), "topic": topic, "source": "operator"})
        pack_path, _ = self._create_signed_exported_pack(
            pack_name="phase210_unknown_signer_pack",
            output_dir=self.root / "phase210_unknown_signer_pack",
            signer_id="phase210.unknown.signer",
            signing_secret=unknown_secret,
            topics=[topic],
            kinds=["context_block"],
        )
        failed = self._pack_import_error(
            pack_path=str(pack_path),
            allow_trusted_import=True,
            verification_secret=unknown_secret,
        )
        self.assertEqual(self._pack_error_code(failed), "trusted_import_requires_verified_trusted_signer")

    def test_pack_import_trusted_rejects_invalid_blocked_disabled_mismatch(self) -> None:
        server.load_store()
        base_count = self._table_count("imported_packs")

        invalid_fixture = self._create_signed_pack_fixture(marker="phase210-trusted-invalid-signature")
        invalid = self._pack_import_error(
            pack_path=str(invalid_fixture["pack_path"]),
            allow_trusted_import=True,
            verification_secret="phase210-invalid-secret-wrong-012345678901",
        )
        self.assertEqual(self._pack_error_code(invalid), "trusted_import_requires_verified_trusted_signer")

        blocked_fixture = self._create_signed_pack_fixture(
            marker="phase210-trusted-blocked-signer",
            trust_level="blocked",
        )
        blocked = self._pack_import_error(
            pack_path=str(blocked_fixture["pack_path"]),
            allow_trusted_import=True,
            verification_secret=str(blocked_fixture["secret"]),
        )
        self.assertEqual(self._pack_error_code(blocked), "trusted_import_requires_verified_trusted_signer")

        disabled_fixture = self._create_signed_pack_fixture(marker="phase210-trusted-disabled-signer")
        self._signer_disable(signer_id=str(disabled_fixture["signer_id"]))
        disabled = self._pack_import_error(
            pack_path=str(disabled_fixture["pack_path"]),
            allow_trusted_import=True,
            verification_secret=str(disabled_fixture["secret"]),
        )
        self.assertEqual(self._pack_error_code(disabled), "trusted_import_requires_verified_trusted_signer")

        mismatch_signer = "phase210.mismatch.signer"
        secret_a = "phase210-mismatch-secret-A-0123456789012345"
        secret_b = "phase210-mismatch-secret-B-0123456789012345"
        self._signer_add(signer_id=mismatch_signer, secret=secret_a, trust_level="trusted")
        row = self.record("phase210 mismatch source", kind="context_block")
        mismatch_topic = "phase210-mismatch-topic"
        server.topic_add({"memory_id": str(row["id"]), "topic": mismatch_topic, "source": "operator"})
        mismatch_pack, _ = self._create_signed_exported_pack(
            pack_name="phase210_mismatch_pack",
            output_dir=self.root / "phase210_mismatch_pack",
            signer_id=mismatch_signer,
            signing_secret=secret_b,
            topics=[mismatch_topic],
            kinds=["context_block"],
        )
        mismatch = self._pack_import_error(
            pack_path=str(mismatch_pack),
            allow_trusted_import=True,
            verification_secret=secret_b,
        )
        self.assertEqual(self._pack_error_code(mismatch), "trusted_import_requires_verified_trusted_signer")
        self.assertEqual(base_count, self._table_count("imported_packs"))

    def test_pack_import_quarantine_fallback_for_signed_pack(self) -> None:
        fixture = self._create_signed_pack_fixture(marker="phase210-signed-quarantine-fallback")
        imported = self._pack_import(pack_path=str(fixture["pack_path"]), allow_unsigned_quarantine=True)
        sc = imported["structuredContent"]
        self.assertEqual(str(sc["trust_level"]), "quarantine")
        self.assertTrue(str(sc["namespace"]).startswith("pack:quarantine:"))

    def test_pack_import_reimport_collision_across_trust_levels(self) -> None:
        fixture_q = self._create_signed_pack_fixture(marker="phase210-cross-level-q-first")
        imported_q = self._pack_import(pack_path=str(fixture_q["pack_path"]), allow_unsigned_quarantine=True)
        again_trusted = self._pack_import_error(
            pack_path=str(fixture_q["pack_path"]),
            allow_trusted_import=True,
            verification_secret=str(fixture_q["secret"]),
        )
        self.assertEqual(self._pack_error_code(again_trusted), "pack_already_imported")
        self.assertTrue(str(imported_q["structuredContent"]["namespace"]).startswith("pack:quarantine:"))

        fixture_t = self._create_signed_pack_fixture(marker="phase210-cross-level-t-first")
        imported_t = self._pack_import(
            pack_path=str(fixture_t["pack_path"]),
            allow_trusted_import=True,
            verification_secret=str(fixture_t["secret"]),
        )
        again_quarantine = self._pack_import_error(
            pack_path=str(fixture_t["pack_path"]),
            allow_unsigned_quarantine=True,
        )
        self.assertEqual(self._pack_error_code(again_quarantine), "pack_already_imported")
        self.assertTrue(str(imported_t["structuredContent"]["namespace"]).startswith("pack:trusted:"))

    def test_pack_list_imports_mixed_trust_filter(self) -> None:
        trusted_fixture = self._create_signed_pack_fixture(marker="phase210-list-mixed-trusted")
        trusted_import = self._pack_import(
            pack_path=str(trusted_fixture["pack_path"]),
            allow_trusted_import=True,
            verification_secret=str(trusted_fixture["secret"]),
        )
        q_row = self.record("phase210-list-mixed-quarantine source", kind="context_block")
        q_topic = "phase210-list-mixed-quarantine-topic"
        server.topic_add({"memory_id": str(q_row["id"]), "topic": q_topic, "source": "operator"})
        q_pack = self._create_exported_pack(
            pack_name="phase210_list_mixed_quarantine",
            output_dir=self.root / "phase210_list_mixed_quarantine",
            topics=[q_topic],
            kinds=["context_block"],
        )
        quarantine_import = self._pack_import(pack_path=str(q_pack), allow_unsigned_quarantine=True)

        trusted_only = self._pack_list_imports(trust_level="trusted")
        quarantine_only = self._pack_list_imports(trust_level="quarantine")
        all_rows = self._pack_list_imports()
        trusted_ids = {str(item["pack_id"]) for item in trusted_only["structuredContent"]["packs"]}
        quarantine_ids = {str(item["pack_id"]) for item in quarantine_only["structuredContent"]["packs"]}
        all_ids = {str(item["pack_id"]) for item in all_rows["structuredContent"]["packs"]}
        self.assertIn(str(trusted_import["structuredContent"]["pack_id"]), trusted_ids)
        self.assertNotIn(str(quarantine_import["structuredContent"]["pack_id"]), trusted_ids)
        self.assertIn(str(quarantine_import["structuredContent"]["pack_id"]), quarantine_ids)
        self.assertNotIn(str(trusted_import["structuredContent"]["pack_id"]), quarantine_ids)
        self.assertIn(str(trusted_import["structuredContent"]["pack_id"]), all_ids)
        self.assertIn(str(quarantine_import["structuredContent"]["pack_id"]), all_ids)

    def test_pack_review_import_trusted_pack(self) -> None:
        fixture = self._create_trusted_import_fixture(marker="phase210-review-trusted")
        imported_sc = fixture["imported"]["structuredContent"]
        reviewed = self._pack_review_import(pack_id=str(imported_sc["pack_id"]), include_samples=True, sample_limit=5)
        sc = reviewed["structuredContent"]
        self.assertEqual(str(sc["pack"]["trust_level"]), "trusted")
        self.assertTrue(str(sc["pack"]["namespace"]).startswith("pack:trusted:"))
        if sc["samples"]:
            self.assertTrue(all(str(item["namespace"]).startswith("pack:trusted:") for item in sc["samples"]))
            self.assertTrue(all(str(item["origin"]) == "imported" for item in sc["samples"]))

    def test_pack_inspect_trusted_import_available_required(self) -> None:
        unsigned_row = self.record("phase210 trusted import available unsigned", kind="context_block")
        unsigned_topic = "phase210-trusted-available-unsigned-topic"
        server.topic_add({"memory_id": str(unsigned_row["id"]), "topic": unsigned_topic, "source": "operator"})
        unsigned_pack = self._create_exported_pack(
            pack_name="phase210_trusted_available_unsigned",
            output_dir=self.root / "phase210_trusted_available_unsigned",
            topics=[unsigned_topic],
            kinds=["context_block"],
        )
        unsigned_inspect = self._pack_inspect(pack_path=str(unsigned_pack))
        self.assertIn("trusted_import_available", unsigned_inspect["structuredContent"])
        self.assertFalse(bool(unsigned_inspect["structuredContent"]["trusted_import_available"]))

        trusted_fixture = self._create_signed_pack_fixture(marker="phase210-trusted-available-trusted")
        trusted_verified = self._pack_inspect(
            pack_path=str(trusted_fixture["pack_path"]),
            verification_secret=str(trusted_fixture["secret"]),
        )
        trusted_no_secret = self._pack_inspect(pack_path=str(trusted_fixture["pack_path"]))
        trusted_wrong = self._pack_inspect(
            pack_path=str(trusted_fixture["pack_path"]),
            verification_secret="phase210-trusted-available-wrong-012345678901",
        )
        self.assertTrue(bool(trusted_verified["structuredContent"]["trusted_import_available"]))
        self.assertFalse(bool(trusted_no_secret["structuredContent"]["trusted_import_available"]))
        self.assertFalse(bool(trusted_wrong["structuredContent"]["trusted_import_available"]))

        unknown_secret = "phase210-trusted-available-unknown-01234567890123"
        src = self.record("phase210 trusted available unknown signer", kind="context_block")
        unknown_topic = "phase210-trusted-available-unknown-topic"
        server.topic_add({"memory_id": str(src["id"]), "topic": unknown_topic, "source": "operator"})
        unknown_pack, _ = self._create_signed_exported_pack(
            pack_name="phase210_trusted_available_unknown",
            output_dir=self.root / "phase210_trusted_available_unknown",
            signer_id="phase210.unknown.available.signer",
            signing_secret=unknown_secret,
            topics=[unknown_topic],
            kinds=["context_block"],
        )
        unknown_inspect = self._pack_inspect(pack_path=str(unknown_pack), verification_secret=unknown_secret)
        self.assertFalse(bool(unknown_inspect["structuredContent"]["trusted_import_available"]))

        blocked_fixture = self._create_signed_pack_fixture(
            marker="phase210-trusted-available-blocked",
            trust_level="blocked",
        )
        blocked_inspect = self._pack_inspect(
            pack_path=str(blocked_fixture["pack_path"]),
            verification_secret=str(blocked_fixture["secret"]),
        )
        self.assertFalse(bool(blocked_inspect["structuredContent"]["trusted_import_available"]))

        disabled_fixture = self._create_signed_pack_fixture(marker="phase210-trusted-available-disabled")
        self._signer_disable(signer_id=str(disabled_fixture["signer_id"]))
        disabled_inspect = self._pack_inspect(
            pack_path=str(disabled_fixture["pack_path"]),
            verification_secret=str(disabled_fixture["secret"]),
        )
        self.assertFalse(bool(disabled_inspect["structuredContent"]["trusted_import_available"]))

        mismatch_signer = "phase210.trusted.available.mismatch"
        secret_a = "phase210-trusted-available-mismatch-A-0123456789"
        secret_b = "phase210-trusted-available-mismatch-B-0123456789"
        self._signer_add(signer_id=mismatch_signer, secret=secret_a, trust_level="trusted")
        mismatch_source = self.record("phase210 trusted available mismatch source", kind="context_block")
        mismatch_topic = "phase210-trusted-available-mismatch-topic"
        server.topic_add({"memory_id": str(mismatch_source["id"]), "topic": mismatch_topic, "source": "operator"})
        mismatch_pack, _ = self._create_signed_exported_pack(
            pack_name="phase210_trusted_available_mismatch",
            output_dir=self.root / "phase210_trusted_available_mismatch",
            signer_id=mismatch_signer,
            signing_secret=secret_b,
            topics=[mismatch_topic],
            kinds=["context_block"],
        )
        mismatch_inspect = self._pack_inspect(pack_path=str(mismatch_pack), verification_secret=secret_b)
        self.assertFalse(bool(mismatch_inspect["structuredContent"]["trusted_import_available"]))

        unsupported_members = self._read_zip_members(Path(str(trusted_fixture["pack_path"])))
        unsupported_manifest = json.loads(unsupported_members["manifest.json"].decode("utf-8"))
        unsupported_sig = json.loads(unsupported_members[server.PACK_SIGNATURE_MEMBER].decode("utf-8"))
        unsupported_manifest["signature"]["signature_algorithm"] = "hmac-sha512-local-v1"
        unsupported_sig["signature_algorithm"] = "hmac-sha512-local-v1"
        unsupported_path = self.root / "phase210_trusted_available_trusted" / "unsupported_signature.zip"
        unsupported_path.parent.mkdir(parents=True, exist_ok=True)
        self._rewrite_zip(
            Path(str(trusted_fixture["pack_path"])),
            unsupported_path,
            replace_members={
                "manifest.json": (
                    json.dumps(unsupported_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode("utf-8"),
                server.PACK_SIGNATURE_MEMBER: (
                    json.dumps(unsupported_sig, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode("utf-8"),
            },
        )
        unsupported_inspect = self._pack_inspect(
            pack_path=str(unsupported_path),
            verification_secret=str(trusted_fixture["secret"]),
        )
        self.assertFalse(bool(unsupported_inspect["structuredContent"]["trusted_import_available"]))

    def test_pack_promote_preview_trusted_pack(self) -> None:
        fixture = self._create_trusted_import_fixture(marker="phase210-promote-preview-trusted")
        pack_id = str(fixture["imported"]["structuredContent"]["pack_id"])
        row_id = str(self._pack_rows(pack_id)[0][0])
        preview = self._pack_promote_preview(pack_id=pack_id, row_ids=[row_id], include_samples=False)
        sc = preview["structuredContent"]
        self.assertEqual(str(sc["promotion_plan"]["target_namespace"]), "local")
        self.assertEqual(str(sc["promotion_plan"]["target_origin"]), "promoted")
        trusted_warnings = [
            item
            for item in sc["warnings"]
            if isinstance(item, dict) and str(item.get("code")) == "trusted_import_source"
        ]
        self.assertTrue(trusted_warnings)
        self.assertTrue(any(str(item.get("phase", "")) == "preview" for item in trusted_warnings))

    def test_pack_promote_trusted_pack_success(self) -> None:
        fixture = self._create_trusted_import_fixture(marker="phase210-promote-trusted")
        pack_id = str(fixture["imported"]["structuredContent"]["pack_id"])
        row_id = str(self._pack_rows(pack_id)[0][0])
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        sc = promoted["structuredContent"]
        self.assertTrue(sc["promoted_rows"])
        promoted_memory_id = str(sc["promoted_rows"][0]["promoted_memory_id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT namespace, origin FROM memories WHERE id = ?", (promoted_memory_id,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(str(row["namespace"]), "local")
        self.assertEqual(str(row["origin"]), "promoted")
        trusted_warnings = [
            item
            for item in sc["warnings"]
            if isinstance(item, dict) and str(item.get("code")) == "trusted_import_source"
        ]
        self.assertTrue(any(str(item.get("phase", "")) == "promotion" for item in trusted_warnings))

    def test_pack_promote_trusted_pack_does_not_bypass_gates(self) -> None:
        fixture = self._create_trusted_import_fixture(marker="phase210-promote-trusted-gates")
        pack_id = str(fixture["imported"]["structuredContent"]["pack_id"])
        row_id = str(self._pack_rows(pack_id)[0][0])
        missing_confirm = self._pack_promote_error(pack_id=pack_id, row_ids=[row_id])
        self.assertEqual(self._pack_error_code(missing_confirm), "confirm_promote_required")
        missing_filters = self._pack_promote_error(pack_id=pack_id, confirm_promote=True)
        self.assertEqual(self._pack_error_code(missing_filters), "promote_all_requires_explicit_allow")
        query_rejected = self._pack_promote_error(
            pack_id=pack_id,
            row_ids=[row_id],
            query="not allowed",
            confirm_promote=True,
        )
        self.assertEqual(self._pack_error_code(query_rejected), "query_filter_not_allowed_for_promotion")

    def test_pack_promote_rejects_invalid_trust_level_not_trusted_or_quarantine(self) -> None:
        self._insert_imported_pack_unchecked(
            pack_id="phase210-invalid-promote-pack",
            trust_level="legacy-invalid",
            namespace="pack:legacy:phase210-invalid-promote-pack",
        )
        failed = self._pack_promote_error(
            pack_id="phase210-invalid-promote-pack",
            allow_promote_all=True,
            confirm_promote=True,
        )
        self.assertEqual(self._pack_error_code(failed), "unsupported_trust_level_for_promotion")

    def test_pack_promote_preview_rejects_invalid_trust_level_not_trusted_or_quarantine(self) -> None:
        self._insert_imported_pack_unchecked(
            pack_id="phase210-invalid-preview-pack",
            trust_level="legacy-invalid",
            namespace="pack:legacy:phase210-invalid-preview-pack",
        )
        failed = self._pack_promote_preview_error(pack_id="phase210-invalid-preview-pack")
        self.assertEqual(self._pack_error_code(failed), "unsupported_trust_level_for_promotion_preview")

    def test_pack_import_verification_secret_lifecycle_scrubbed(self) -> None:
        fixture = self._create_signed_pack_fixture(marker="phase210-secret-lifecycle")
        unique_secret = str(fixture["secret"])
        imported = self._pack_import(
            pack_path=str(fixture["pack_path"]),
            allow_trusted_import=True,
            verification_secret=unique_secret,
        )
        pack_id = str(imported["structuredContent"]["pack_id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            imported_row = conn.execute(
                "SELECT manifest_json, freshness_summary_json FROM imported_packs WHERE pack_id = ?",
                (pack_id,),
            ).fetchone()
            event_rows = conn.execute("SELECT data_json FROM events ORDER BY created_at ASC, rowid ASC").fetchall()
        finally:
            conn.close()
        serialized = json.dumps(imported["structuredContent"], ensure_ascii=False)
        if imported_row is not None:
            serialized += str(imported_row[0]) + str(imported_row[1])
        serialized += "\n".join(str(row[0]) for row in event_rows)
        self.assertNotIn(unique_secret, serialized)

    def test_pack_promote_trusted_source_provenance(self) -> None:
        fixture = self._create_trusted_import_fixture(marker="phase210-trusted-provenance")
        imported_sc = fixture["imported"]["structuredContent"]
        pack_id = str(imported_sc["pack_id"])
        row_id = str(self._pack_rows(pack_id)[0][0])
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        promoted_memory_id = str(promoted["structuredContent"]["promoted_rows"][0]["promoted_memory_id"])
        expected_fingerprint = server._secret_fingerprint(str(fixture["secret"]))

        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT metadata_json FROM memories WHERE id = ?", (promoted_memory_id,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        assert row is not None
        metadata = json.loads(str(row["metadata_json"] or "{}"))
        pack_meta = metadata.get("pack_promotion", {}) if isinstance(metadata, dict) else {}
        self.assertEqual(str(pack_meta.get("source_trust_level", "")), "trusted")
        self.assertEqual(str(pack_meta.get("source_signer_id", "")), str(fixture["signer_id"]))
        self.assertEqual(str(pack_meta.get("source_secret_fingerprint", "")), expected_fingerprint)

    def test_unsigned_and_quarantine_lifecycle_still_passes(self) -> None:
        marker = "phase210_unsigned_quarantine_lifecycle"
        row = self.record(f"{marker} source", kind="context_block")
        topic = f"{marker}-topic"
        server.topic_add({"memory_id": str(row["id"]), "topic": topic, "source": "operator"})
        pack_path = self._create_exported_pack(
            pack_name=f"{marker}_pack",
            output_dir=self.root / f"{marker}_pack",
            topics=[topic],
            kinds=["context_block"],
        )
        inspected = self._pack_inspect(pack_path=str(pack_path))
        self.assertEqual(str(inspected["structuredContent"]["status"]), "valid")
        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        self.assertEqual(str(imported["structuredContent"]["trust_level"]), "quarantine")
        pack_id = str(imported["structuredContent"]["pack_id"])
        row_id = str(self._pack_rows(pack_id)[0][0])
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        promoted_id = str(promoted["structuredContent"]["promoted_rows"][0]["promoted_memory_id"])
        default = server.search_memories({"query": marker, "limit": 20})
        self.assertIn(promoted_id, {str(item["id"]) for item in default["structuredContent"]["matches"]})

    def test_signed_trusted_import_then_promote_lifecycle(self) -> None:
        marker = "phase210_signed_trusted_lifecycle"
        fixture = self._create_trusted_import_fixture(marker=marker, touched_files=["src/phase210/lifecycle.py"])
        imported_sc = fixture["imported"]["structuredContent"]
        self.assertEqual(str(imported_sc["trust_level"]), "trusted")
        pack_id = str(imported_sc["pack_id"])
        namespace = str(imported_sc["namespace"])
        imported_memory_id = str(imported_sc["imported_rows"][0]["memory_id"])

        reviewed = self._pack_review_import(pack_id=pack_id, include_samples=True, sample_limit=5)
        self.assertGreaterEqual(int(reviewed["structuredContent"]["selection"]["selected_rows"]), 1)
        preview = self._pack_promote_preview(pack_id=pack_id, row_ids=[str(self._pack_rows(pack_id)[0][0])], include_samples=False)
        self.assertFalse(preview["isError"], preview)

        promoted = self._pack_promote(
            pack_id=pack_id,
            row_ids=[str(self._pack_rows(pack_id)[0][0])],
            confirm_promote=True,
        )
        promoted_id = str(promoted["structuredContent"]["promoted_rows"][0]["promoted_memory_id"])

        default = server.search_memories({"query": marker, "limit": 50})
        include_imported = server.search_memories({"query": marker, "limit": 50, "include_imported": True})
        include_quarantine = server.search_memories({"query": marker, "limit": 50, "include_quarantine": True})
        explicit_namespace = server.search_memories({"query": marker, "limit": 50, "namespace": namespace})
        default_ids = {str(item["id"]) for item in default["structuredContent"]["matches"]}
        imported_ids = {str(item["id"]) for item in include_imported["structuredContent"]["matches"]}
        quarantine_ids = {str(item["id"]) for item in include_quarantine["structuredContent"]["matches"]}
        explicit_ids = {str(item["id"]) for item in explicit_namespace["structuredContent"]["matches"]}

        self.assertIn(promoted_id, default_ids)
        self.assertNotIn(imported_memory_id, default_ids)
        self.assertIn(imported_memory_id, imported_ids)
        self.assertNotIn(imported_memory_id, quarantine_ids)
        self.assertIn(imported_memory_id, explicit_ids)

    def test_memory_packs_v1_trusted_import_lifecycle(self) -> None:
        marker = "phase211_v1_trusted_lifecycle"
        fixture = self._create_trusted_import_fixture(marker=marker, touched_files=["src/phase211/trusted.py"])
        inspect_sc = fixture["inspect"]["structuredContent"]
        imported_sc = fixture["imported"]["structuredContent"]
        self.assertEqual(str((inspect_sc.get("signature") or {}).get("trust_classification", "")), "trusted_signer")
        self.assertTrue(bool(inspect_sc.get("trusted_import_available")))
        self.assertEqual(str(imported_sc.get("trust_level", "")), "trusted")
        self.assertTrue(str(imported_sc.get("namespace", "")).startswith("pack:trusted:"))

        pack_id = str(imported_sc["pack_id"])
        imported_memory_id = str(imported_sc["imported_rows"][0]["memory_id"])
        row_id = str(self._pack_rows(pack_id)[0][0])
        pack_path = Path(str(fixture["pack_path"]))
        moved_pack_path = pack_path.with_suffix(".moved")
        if pack_path.exists():
            pack_path.replace(moved_pack_path)

        reviewed = self._pack_review_import(pack_id=pack_id, include_samples=True, sample_limit=5)
        self.assertGreaterEqual(int(reviewed["structuredContent"]["selection"]["selected_rows"]), 1)
        preview = self._pack_promote_preview(pack_id=pack_id, row_ids=[row_id], include_samples=False)
        self.assertFalse(preview["isError"], preview)

        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        promoted_id = str(promoted["structuredContent"]["promoted_rows"][0]["promoted_memory_id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            audit_row = conn.execute(
                "SELECT promotion_id FROM promotion_audit WHERE pack_id = ? ORDER BY promoted_at DESC LIMIT 1",
                (pack_id,),
            ).fetchone()
            mapping_row = conn.execute(
                "SELECT promoted_memory_id FROM promoted_pack_rows WHERE pack_id = ? AND row_id_in_pack = ?",
                (pack_id, row_id),
            ).fetchone()
            imported_ns_row = conn.execute(
                "SELECT namespace, origin FROM memories WHERE id = ?",
                (imported_memory_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(audit_row)
        self.assertIsNotNone(mapping_row)
        self.assertIsNotNone(imported_ns_row)
        assert mapping_row is not None
        assert imported_ns_row is not None
        self.assertEqual(str(mapping_row["promoted_memory_id"]), promoted_id)
        self.assertTrue(str(imported_ns_row["namespace"]).startswith("pack:trusted:"))
        self.assertEqual(str(imported_ns_row["origin"]), "imported")

        default = server.search_memories({"query": marker, "limit": 50})
        include_imported = server.search_memories({"query": marker, "limit": 50, "include_imported": True})
        default_ids = {str(item["id"]) for item in default["structuredContent"]["matches"]}
        imported_ids = {str(item["id"]) for item in include_imported["structuredContent"]["matches"]}
        self.assertIn(promoted_id, default_ids)
        self.assertNotIn(imported_memory_id, default_ids)
        self.assertIn(imported_memory_id, imported_ids)

    def test_memory_packs_v1_quarantine_fallback_signed_lifecycle(self) -> None:
        marker = "phase211_v1_quarantine_fallback"
        fixture = self._create_signed_pack_fixture(marker=marker)
        inspected = self._pack_inspect(
            pack_path=str(fixture["pack_path"]),
            verification_secret=str(fixture["secret"]),
        )
        self.assertEqual(
            str((inspected["structuredContent"].get("signature") or {}).get("trust_classification", "")),
            "trusted_signer",
        )
        imported = self._pack_import(
            pack_path=str(fixture["pack_path"]),
            allow_unsigned_quarantine=True,
            allow_trusted_import=False,
        )
        imported_sc = imported["structuredContent"]
        self.assertEqual(str(imported_sc["trust_level"]), "quarantine")
        self.assertTrue(str(imported_sc["namespace"]).startswith("pack:quarantine:"))
        pack_id = str(imported_sc["pack_id"])
        row_id = str(self._pack_rows(pack_id)[0][0])
        imported_memory_id = str(imported_sc["imported_rows"][0]["memory_id"])

        reviewed = self._pack_review_import(pack_id=pack_id, include_samples=True, sample_limit=5)
        self.assertGreaterEqual(int(reviewed["structuredContent"]["selection"]["selected_rows"]), 1)
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        promoted_id = str(promoted["structuredContent"]["promoted_rows"][0]["promoted_memory_id"])

        default = server.search_memories({"query": marker, "limit": 50})
        include_quarantine = server.search_memories({"query": marker, "limit": 50, "include_quarantine": True})
        default_ids = {str(item["id"]) for item in default["structuredContent"]["matches"]}
        quarantine_ids = {str(item["id"]) for item in include_quarantine["structuredContent"]["matches"]}
        self.assertIn(promoted_id, default_ids)
        self.assertNotIn(imported_memory_id, default_ids)
        self.assertIn(imported_memory_id, quarantine_ids)

    def test_memory_packs_v1_unsigned_lifecycle_still_passes(self) -> None:
        marker = "phase211_v1_unsigned"
        source = self.record(f"{marker} source", kind="context_block", title=f"{marker} title")
        topic = f"{marker}-topic"
        added = server.topic_add({"memory_id": str(source["id"]), "topic": topic, "source": "operator"})
        self.assertFalse(added["isError"], added)
        pack_path = self._create_exported_pack(
            pack_name=f"{marker}_pack",
            output_dir=self.root / f"{marker}_pack",
            topics=[topic],
            kinds=["context_block"],
        )
        inspected = self._pack_inspect(pack_path=str(pack_path))
        self.assertEqual(str(inspected["structuredContent"]["status"]), "valid")
        self.assertFalse(bool(inspected["structuredContent"]["trusted_import_available"]))
        self.assertEqual(
            str((inspected["structuredContent"].get("signature") or {}).get("trust_classification", "")),
            "unsigned",
        )

        trusted_attempt_with_secret = self._pack_import_error(
            pack_path=str(pack_path),
            allow_trusted_import=True,
            verification_secret="phase211-v1-unsigned-trusted-secret-01234567890123",
        )
        trusted_attempt_no_secret = self._pack_import_error(
            pack_path=str(pack_path),
            allow_trusted_import=True,
        )
        self.assertEqual(
            self._pack_error_code(trusted_attempt_with_secret),
            "trusted_import_requires_verified_trusted_signer",
        )
        self.assertEqual(
            self._pack_error_code(trusted_attempt_no_secret),
            "trusted_import_requires_verification_secret",
        )

        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        self.assertEqual(str(imported["structuredContent"]["trust_level"]), "quarantine")
        pack_id = str(imported["structuredContent"]["pack_id"])
        row_id = str(self._pack_rows(pack_id)[0][0])
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        promoted_id = str(promoted["structuredContent"]["promoted_rows"][0]["promoted_memory_id"])
        default = server.search_memories({"query": marker, "limit": 30})
        self.assertIn(promoted_id, {str(item["id"]) for item in default["structuredContent"]["matches"]})

    def test_memory_packs_mixed_trust_retrieval_semantics(self) -> None:
        marker = "phase211_mixed_retrieval"
        local = self.record(f"{marker} local", kind="context_block", title=f"{marker} local title")
        trusted_fixture = self._create_trusted_import_fixture(marker=f"{marker}_trusted")
        trusted_sc = trusted_fixture["imported"]["structuredContent"]
        trusted_pack_id = str(trusted_sc["pack_id"])
        trusted_namespace = str(trusted_sc["namespace"])
        trusted_memory_id = str(trusted_sc["imported_rows"][0]["memory_id"])

        quarantine_fixture = self._create_signed_pack_fixture(marker=f"{marker}_quarantine")
        quarantine_import = self._pack_import(
            pack_path=str(quarantine_fixture["pack_path"]),
            allow_unsigned_quarantine=True,
        )
        quarantine_sc = quarantine_import["structuredContent"]
        quarantine_pack_id = str(quarantine_sc["pack_id"])
        quarantine_namespace = str(quarantine_sc["namespace"])
        quarantine_memory_id = str(quarantine_sc["imported_rows"][0]["memory_id"])

        trusted_row_id = str(self._pack_rows(trusted_pack_id)[0][0])
        promoted = self._pack_promote(
            pack_id=trusted_pack_id,
            row_ids=[trusted_row_id],
            confirm_promote=True,
        )
        promoted_id = str(promoted["structuredContent"]["promoted_rows"][0]["promoted_memory_id"])

        default = server.search_memories({"query": marker, "limit": 80})
        include_imported = server.search_memories({"query": marker, "limit": 80, "include_imported": True})
        include_quarantine = server.search_memories({"query": marker, "limit": 80, "include_quarantine": True})
        both = server.search_memories(
            {"query": marker, "limit": 80, "include_imported": True, "include_quarantine": True}
        )
        default_ids = [str(item["id"]) for item in default["structuredContent"]["matches"]]
        imported_ids = [str(item["id"]) for item in include_imported["structuredContent"]["matches"]]
        quarantine_ids = [str(item["id"]) for item in include_quarantine["structuredContent"]["matches"]]
        both_ids = [str(item["id"]) for item in both["structuredContent"]["matches"]]

        self.assertIn(str(local["id"]), default_ids)
        self.assertIn(promoted_id, default_ids)
        self.assertNotIn(trusted_memory_id, default_ids)
        self.assertNotIn(quarantine_memory_id, default_ids)

        self.assertIn(trusted_memory_id, imported_ids)
        self.assertNotIn(quarantine_memory_id, imported_ids)

        self.assertIn(quarantine_memory_id, quarantine_ids)
        self.assertNotIn(trusted_memory_id, quarantine_ids)

        self.assertIn(trusted_memory_id, both_ids)
        self.assertIn(quarantine_memory_id, both_ids)

        namespace_local = server.search_memories(
            {
                "query": marker,
                "limit": 80,
                "namespace": "local",
                "include_imported": True,
                "include_quarantine": True,
            }
        )
        namespace_trusted = server.search_memories(
            {
                "query": marker,
                "limit": 80,
                "namespace": trusted_namespace,
                "include_quarantine": True,
            }
        )
        namespace_quarantine = server.search_memories(
            {
                "query": marker,
                "limit": 80,
                "namespace": quarantine_namespace,
                "include_imported": True,
            }
        )
        multi_namespace = server.search_memories(
            {
                "query": marker,
                "limit": 80,
                "namespaces": ["local", trusted_namespace],
                "include_quarantine": True,
            }
        )
        ns_local_ids = {str(item["id"]) for item in namespace_local["structuredContent"]["matches"]}
        ns_trusted_ids = {str(item["id"]) for item in namespace_trusted["structuredContent"]["matches"]}
        ns_quarantine_ids = {str(item["id"]) for item in namespace_quarantine["structuredContent"]["matches"]}
        ns_multi_ids = {str(item["id"]) for item in multi_namespace["structuredContent"]["matches"]}

        self.assertIn(str(local["id"]), ns_local_ids)
        self.assertIn(promoted_id, ns_local_ids)
        self.assertNotIn(trusted_memory_id, ns_local_ids)
        self.assertNotIn(quarantine_memory_id, ns_local_ids)

        self.assertEqual(ns_trusted_ids, {trusted_memory_id})
        self.assertEqual(ns_quarantine_ids, {quarantine_memory_id})
        self.assertIn(str(local["id"]), ns_multi_ids)
        self.assertIn(promoted_id, ns_multi_ids)
        self.assertIn(trusted_memory_id, ns_multi_ids)
        self.assertNotIn(quarantine_memory_id, ns_multi_ids)

        origin_promoted = server.search_memories({"query": marker, "limit": 80, "origins": ["promoted"]})
        promoted_only_ids = {str(item["id"]) for item in origin_promoted["structuredContent"]["matches"]}
        self.assertIn(promoted_id, promoted_only_ids)
        self.assertNotIn(trusted_memory_id, promoted_only_ids)
        self.assertNotIn(quarantine_memory_id, promoted_only_ids)

        origin_imported_trusted = server.search_memories(
            {"query": marker, "limit": 80, "namespace": trusted_namespace, "origin": "imported"}
        )
        origin_imported_quarantine = server.search_memories(
            {"query": marker, "limit": 80, "namespace": quarantine_namespace, "origin": "imported"}
        )
        origin_imported_without_flags = server.search_memories({"query": marker, "limit": 80, "origin": "imported"})
        self.assertEqual(
            {str(item["id"]) for item in origin_imported_trusted["structuredContent"]["matches"]},
            {trusted_memory_id},
        )
        self.assertEqual(
            {str(item["id"]) for item in origin_imported_quarantine["structuredContent"]["matches"]},
            {quarantine_memory_id},
        )
        self.assertNotIn(
            trusted_memory_id,
            {str(item["id"]) for item in origin_imported_without_flags["structuredContent"]["matches"]},
        )
        self.assertNotIn(
            quarantine_memory_id,
            {str(item["id"]) for item in origin_imported_without_flags["structuredContent"]["matches"]},
        )

    def test_memory_packs_explicit_namespace_overrides_include_flags(self) -> None:
        marker = "phase211_namespace_override"
        local = self.record(f"{marker} local", kind="context_block")
        trusted_fixture = self._create_trusted_import_fixture(marker=f"{marker}_trusted")
        trusted_sc = trusted_fixture["imported"]["structuredContent"]
        trusted_pack_id = str(trusted_sc["pack_id"])
        trusted_namespace = str(trusted_sc["namespace"])
        trusted_memory_id = str(trusted_sc["imported_rows"][0]["memory_id"])

        quarantine_fixture = self._create_signed_pack_fixture(marker=f"{marker}_quarantine")
        quarantine_import = self._pack_import(
            pack_path=str(quarantine_fixture["pack_path"]),
            allow_unsigned_quarantine=True,
        )
        quarantine_sc = quarantine_import["structuredContent"]
        quarantine_namespace = str(quarantine_sc["namespace"])
        quarantine_memory_id = str(quarantine_sc["imported_rows"][0]["memory_id"])

        trusted_row_id = str(self._pack_rows(trusted_pack_id)[0][0])
        promoted = self._pack_promote(pack_id=trusted_pack_id, row_ids=[trusted_row_id], confirm_promote=True)
        promoted_id = str(promoted["structuredContent"]["promoted_rows"][0]["promoted_memory_id"])

        only_local = server.search_memories(
            {
                "query": marker,
                "limit": 80,
                "namespace": "local",
                "include_imported": True,
                "include_quarantine": True,
            }
        )
        only_trusted = server.search_memories(
            {
                "query": marker,
                "limit": 80,
                "namespace": trusted_namespace,
                "include_quarantine": True,
            }
        )
        only_quarantine = server.search_memories(
            {
                "query": marker,
                "limit": 80,
                "namespace": quarantine_namespace,
                "include_imported": True,
            }
        )
        local_and_trusted = server.search_memories(
            {
                "query": marker,
                "limit": 80,
                "namespaces": ["local", trusted_namespace],
                "include_quarantine": True,
            }
        )
        local_ids = {str(item["id"]) for item in only_local["structuredContent"]["matches"]}
        trusted_ids = {str(item["id"]) for item in only_trusted["structuredContent"]["matches"]}
        quarantine_ids = {str(item["id"]) for item in only_quarantine["structuredContent"]["matches"]}
        local_trusted_ids = {str(item["id"]) for item in local_and_trusted["structuredContent"]["matches"]}

        self.assertIn(str(local["id"]), local_ids)
        self.assertIn(promoted_id, local_ids)
        self.assertNotIn(trusted_memory_id, local_ids)
        self.assertNotIn(quarantine_memory_id, local_ids)
        self.assertEqual(trusted_ids, {trusted_memory_id})
        self.assertEqual(quarantine_ids, {quarantine_memory_id})
        self.assertIn(str(local["id"]), local_trusted_ids)
        self.assertIn(promoted_id, local_trusted_ids)
        self.assertIn(trusted_memory_id, local_trusted_ids)
        self.assertNotIn(quarantine_memory_id, local_trusted_ids)

    def test_include_imported_excludes_quarantine_after_5b(self) -> None:
        # 0.21.x pins post-5b migration semantics:
        # include_imported=true includes trusted imports, quarantine requires include_quarantine=true.
        marker = "phase211_include_imported_migration"
        trusted_ns = "pack:trusted:phase211-include-trusted"
        quarantine_ns = "pack:quarantine:phase211-include-quarantine"
        self._insert_imported_pack(pack_id="phase211-include-trusted", trust_level="trusted", namespace=trusted_ns)
        self._insert_imported_pack(
            pack_id="phase211-include-quarantine",
            trust_level="quarantine",
            namespace=quarantine_ns,
        )
        trusted_row = self.record(f"{marker} trusted", kind="note", namespace=trusted_ns, origin="imported")
        quarantine_row = self.record(f"{marker} quarantine", kind="note", namespace=quarantine_ns, origin="imported")
        imported_only = server.search_memories({"query": marker, "limit": 20, "include_imported": True})
        quarantine_only = server.search_memories({"query": marker, "limit": 20, "include_quarantine": True})
        imported_ids = {str(item["id"]) for item in imported_only["structuredContent"]["matches"]}
        quarantine_ids = {str(item["id"]) for item in quarantine_only["structuredContent"]["matches"]}
        self.assertIn(str(trusted_row["id"]), imported_ids)
        self.assertNotIn(str(quarantine_row["id"]), imported_ids)
        self.assertIn(str(quarantine_row["id"]), quarantine_ids)

    def test_memory_packs_cross_trust_reimport_collision(self) -> None:
        fixture_q = self._create_signed_pack_fixture(marker="phase211-cross-q-first")
        imported_q = self._pack_import(pack_path=str(fixture_q["pack_path"]), allow_unsigned_quarantine=True)
        again_trusted = self._pack_import_error(
            pack_path=str(fixture_q["pack_path"]),
            allow_trusted_import=True,
            verification_secret=str(fixture_q["secret"]),
        )
        self.assertEqual(self._pack_error_code(again_trusted), "pack_already_imported")
        pack_id_q = str(imported_q["structuredContent"]["pack_id"])
        variant = self.root / "phase211-cross-q-first" / "collision_variant.zip"
        variant.parent.mkdir(parents=True, exist_ok=True)
        self._rewrite_zip(
            Path(str(fixture_q["pack_path"])),
            variant,
            extra_members={"extra/collision.txt": b"phase211 distinct bytes"},
        )
        distinct = self._pack_import_error(pack_path=str(variant), allow_unsigned_quarantine=True)
        self.assertEqual(self._pack_error_code(distinct), "pack_id_collision_distinct_content")

        fixture_t = self._create_signed_pack_fixture(marker="phase211-cross-t-first")
        imported_t = self._pack_import(
            pack_path=str(fixture_t["pack_path"]),
            allow_trusted_import=True,
            verification_secret=str(fixture_t["secret"]),
        )
        again_quarantine = self._pack_import_error(
            pack_path=str(fixture_t["pack_path"]),
            allow_unsigned_quarantine=True,
        )
        self.assertEqual(self._pack_error_code(again_quarantine), "pack_already_imported")

        legacy_fixture = self._create_signed_pack_fixture(marker="phase211-cross-legacy")
        legacy_pack_id = str(legacy_fixture["export"]["pack_id"])
        self._insert_imported_pack(
            pack_id=legacy_pack_id,
            trust_level="quarantine",
            namespace=f"pack:quarantine:{legacy_pack_id}",
        )
        legacy_collision = self._pack_import_error(
            pack_path=str(legacy_fixture["pack_path"]),
            allow_unsigned_quarantine=True,
        )
        self.assertEqual(self._pack_error_code(legacy_collision), "pack_already_imported_legacy_unknown_hash")
        self.assertNotEqual(pack_id_q, str(imported_t["structuredContent"]["pack_id"]))

    def test_memory_packs_trusted_import_tamper_rejection(self) -> None:
        fixture = self._create_signed_pack_fixture(marker="phase211-tamper-trusted")
        pack_path = Path(str(fixture["pack_path"]))
        secret = str(fixture["secret"])
        before_imported_packs = self._table_count("imported_packs")
        before_memories = self._table_count("memories")

        members = self._read_zip_members(pack_path)
        tampered_content_path = self.root / "phase211-tamper-trusted" / "tampered_content.zip"
        tampered_content_path.parent.mkdir(parents=True, exist_ok=True)
        tampered_content = members["content/memories.jsonl"] + b"\n"
        self._rewrite_zip(
            pack_path,
            tampered_content_path,
            replace_members={"content/memories.jsonl": tampered_content},
        )
        tampered_content_import = self._pack_import_error(
            pack_path=str(tampered_content_path),
            allow_trusted_import=True,
            verification_secret=secret,
        )
        self.assertEqual(
            self._pack_error_code(tampered_content_import),
            "trusted_import_requires_verified_trusted_signer",
        )

        signature_payload = json.loads(members[server.PACK_SIGNATURE_MEMBER].decode("utf-8"))
        signature_payload["signature_value"] = "0" * len(str(signature_payload.get("signature_value", "")))
        tampered_signature_path = self.root / "phase211-tamper-trusted" / "tampered_signature_value.zip"
        self._rewrite_zip(
            pack_path,
            tampered_signature_path,
            replace_members={
                server.PACK_SIGNATURE_MEMBER: (
                    json.dumps(signature_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode("utf-8")
            },
        )
        tampered_signature_import = self._pack_import_error(
            pack_path=str(tampered_signature_path),
            allow_trusted_import=True,
            verification_secret=secret,
        )
        self.assertEqual(
            self._pack_error_code(tampered_signature_import),
            "trusted_import_requires_verified_trusted_signer",
        )

        signer_tampered = json.loads(members[server.PACK_SIGNATURE_MEMBER].decode("utf-8"))
        signer_tampered["signer_id"] = "phase211.tampered.signer"
        tampered_signer_path = self.root / "phase211-tamper-trusted" / "tampered_signer_id.zip"
        self._rewrite_zip(
            pack_path,
            tampered_signer_path,
            replace_members={
                server.PACK_SIGNATURE_MEMBER: (
                    json.dumps(signer_tampered, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode("utf-8")
            },
        )
        tampered_signer_import = self._pack_import_error(
            pack_path=str(tampered_signer_path),
            allow_trusted_import=True,
            verification_secret=secret,
        )
        self.assertEqual(
            self._pack_error_code(tampered_signer_import),
            "trusted_import_requires_verified_trusted_signer",
        )
        self.assertEqual(before_imported_packs, self._table_count("imported_packs"))
        self.assertEqual(before_memories, self._table_count("memories"))

    def test_upgrade_from_0_21_0_to_0_21_1_no_changes(self) -> None:
        marker = "phase211_upgrade_no_mutation"
        trusted_fixture = self._create_trusted_import_fixture(marker=f"{marker}_trusted")
        trusted_pack_id = str(trusted_fixture["imported"]["structuredContent"]["pack_id"])
        trusted_row_id = str(self._pack_rows(trusted_pack_id)[0][0])
        promoted = self._pack_promote(pack_id=trusted_pack_id, row_ids=[trusted_row_id], confirm_promote=True)
        self.assertFalse(promoted["isError"], promoted)

        quarantine_fixture = self._create_signed_pack_fixture(marker=f"{marker}_quarantine")
        imported_q = self._pack_import(
            pack_path=str(quarantine_fixture["pack_path"]),
            allow_unsigned_quarantine=True,
        )
        self.assertFalse(imported_q["isError"], imported_q)

        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            before_counts = {
                "trusted_signers": int(conn.execute("SELECT COUNT(*) FROM trusted_signers").fetchone()[0]),
                "imported_packs": int(conn.execute("SELECT COUNT(*) FROM imported_packs").fetchone()[0]),
                "imported_pack_rows": int(conn.execute("SELECT COUNT(*) FROM imported_pack_rows").fetchone()[0]),
                "promoted_pack_rows": int(conn.execute("SELECT COUNT(*) FROM promoted_pack_rows").fetchone()[0]),
                "promotion_audit": int(conn.execute("SELECT COUNT(*) FROM promotion_audit").fetchone()[0]),
                "memories": int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]),
            }
            before_imported = conn.execute(
                "SELECT pack_id, trust_level, namespace, COALESCE(received_zip_sha256, '') "
                "FROM imported_packs ORDER BY pack_id ASC"
            ).fetchall()
            before_promoted = conn.execute(
                "SELECT pack_id, row_id_in_pack, imported_memory_id, promoted_memory_id, COALESCE(promotion_id, '') "
                "FROM promoted_pack_rows ORDER BY pack_id ASC, row_id_in_pack ASC"
            ).fetchall()
            before_audit = conn.execute(
                "SELECT promotion_id, pack_id, row_count, limited FROM promotion_audit ORDER BY promotion_id ASC"
            ).fetchall()
            server._sqlite_ensure_schema(conn)
            server._sqlite_ensure_schema(conn)
            schema_version = int(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
            after_counts = {
                "trusted_signers": int(conn.execute("SELECT COUNT(*) FROM trusted_signers").fetchone()[0]),
                "imported_packs": int(conn.execute("SELECT COUNT(*) FROM imported_packs").fetchone()[0]),
                "imported_pack_rows": int(conn.execute("SELECT COUNT(*) FROM imported_pack_rows").fetchone()[0]),
                "promoted_pack_rows": int(conn.execute("SELECT COUNT(*) FROM promoted_pack_rows").fetchone()[0]),
                "promotion_audit": int(conn.execute("SELECT COUNT(*) FROM promotion_audit").fetchone()[0]),
                "memories": int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]),
            }
            after_imported = conn.execute(
                "SELECT pack_id, trust_level, namespace, COALESCE(received_zip_sha256, '') "
                "FROM imported_packs ORDER BY pack_id ASC"
            ).fetchall()
            after_promoted = conn.execute(
                "SELECT pack_id, row_id_in_pack, imported_memory_id, promoted_memory_id, COALESCE(promotion_id, '') "
                "FROM promoted_pack_rows ORDER BY pack_id ASC, row_id_in_pack ASC"
            ).fetchall()
            after_audit = conn.execute(
                "SELECT promotion_id, pack_id, row_count, limited FROM promotion_audit ORDER BY promotion_id ASC"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(schema_version, 7)
        self.assertEqual(before_counts, after_counts)
        self.assertEqual(before_imported, after_imported)
        self.assertEqual(before_promoted, after_promoted)
        self.assertEqual(before_audit, after_audit)

        retrieval = server.search_memories({"query": marker, "limit": 50, "include_imported": True, "include_quarantine": True})
        self.assertGreaterEqual(len(retrieval["structuredContent"]["matches"]), 2)

    def test_pack_review_import_works_without_source_zip(self) -> None:
        trusted_fixture = self._create_trusted_import_fixture(marker="phase211-review-no-zip-trusted")
        trusted_pack_id = str(trusted_fixture["imported"]["structuredContent"]["pack_id"])
        trusted_row_id = str(self._pack_rows(trusted_pack_id)[0][0])
        trusted_zip = Path(str(trusted_fixture["pack_path"]))
        if trusted_zip.exists():
            trusted_zip.unlink()

        quarantine_fixture = self._create_signed_pack_fixture(marker="phase211-review-no-zip-quarantine")
        quarantine_import = self._pack_import(
            pack_path=str(quarantine_fixture["pack_path"]),
            allow_unsigned_quarantine=True,
        )
        quarantine_pack_id = str(quarantine_import["structuredContent"]["pack_id"])
        quarantine_row_id = str(self._pack_rows(quarantine_pack_id)[0][0])
        quarantine_zip = Path(str(quarantine_fixture["pack_path"]))
        if quarantine_zip.exists():
            quarantine_zip.unlink()

        trusted_review = self._pack_review_import(pack_id=trusted_pack_id, include_samples=True, sample_limit=5)
        trusted_preview = self._pack_promote_preview(
            pack_id=trusted_pack_id,
            row_ids=[trusted_row_id],
            include_samples=False,
        )
        quarantine_review = self._pack_review_import(pack_id=quarantine_pack_id, include_samples=True, sample_limit=5)
        quarantine_preview = self._pack_promote_preview(
            pack_id=quarantine_pack_id,
            row_ids=[quarantine_row_id],
            include_samples=False,
        )
        self.assertFalse(trusted_review["isError"], trusted_review)
        self.assertFalse(trusted_preview["isError"], trusted_preview)
        self.assertFalse(quarantine_review["isError"], quarantine_review)
        self.assertFalse(quarantine_preview["isError"], quarantine_preview)

    def test_pack_import_quarantine_with_unused_verification_secret(self) -> None:
        fixture = self._create_signed_pack_fixture(marker="phase211-q-import-unused-secret")
        unique_secret = str(fixture["secret"])
        imported = self._pack_import(
            pack_path=str(fixture["pack_path"]),
            allow_unsigned_quarantine=True,
            verification_secret=unique_secret,
        )
        self.assertIn("verification_secret_unused_for_quarantine_import", self._pack_warning_codes(imported))
        pack_id = str(imported["structuredContent"]["pack_id"])
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            row = conn.execute(
                "SELECT manifest_json, freshness_summary_json FROM imported_packs WHERE pack_id = ?",
                (pack_id,),
            ).fetchone()
            event_rows = conn.execute("SELECT data_json FROM events ORDER BY created_at ASC, rowid ASC").fetchall()
        finally:
            conn.close()
        serialized = json.dumps(imported["structuredContent"], ensure_ascii=False)
        if row is not None:
            serialized += str(row[0]) + str(row[1])
        serialized += "\n".join(str(item[0]) for item in event_rows)
        self.assertNotIn(unique_secret, serialized)

    def test_memory_packs_trusted_import_secret_safety(self) -> None:
        fixture = self._create_signed_pack_fixture(marker="phase211-secret-safety")
        trusted_secret = str(fixture["secret"])
        wrong_secret = "phase211-secret-safety-wrong-01234567890123456"
        failed = self._pack_import_error(
            pack_path=str(fixture["pack_path"]),
            allow_trusted_import=True,
            verification_secret=wrong_secret,
        )
        self.assertEqual(self._pack_error_code(failed), "trusted_import_requires_verified_trusted_signer")

        imported = self._pack_import(
            pack_path=str(fixture["pack_path"]),
            allow_trusted_import=True,
            verification_secret=trusted_secret,
        )
        pack_id = str(imported["structuredContent"]["pack_id"])
        imported_rows = list((imported["structuredContent"] or {}).get("imported_rows", []))
        imported_ids = [str(item["memory_id"]) for item in imported_rows if isinstance(item, dict)]
        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            imported_row = conn.execute(
                "SELECT manifest_json, freshness_summary_json FROM imported_packs WHERE pack_id = ?",
                (pack_id,),
            ).fetchone()
            event_rows = conn.execute("SELECT data_json FROM events ORDER BY created_at ASC, rowid ASC").fetchall()
            metadata_rows = conn.execute(
                "SELECT COALESCE(metadata_json, '') FROM memories WHERE id IN (SELECT memory_id FROM imported_pack_rows WHERE pack_id = ?)",
                (pack_id,),
            ).fetchall()
        finally:
            conn.close()
        serialized = (
            json.dumps(imported, ensure_ascii=False)
            + json.dumps(failed, ensure_ascii=False)
            + "".join(str(item[0]) for item in event_rows)
            + "".join(str(item[0]) for item in metadata_rows)
        )
        if imported_row is not None:
            serialized += str(imported_row[0]) + str(imported_row[1])
        self.assertTrue(imported_ids)
        self.assertNotIn(trusted_secret, serialized)
        self.assertNotIn(wrong_secret, serialized)

    def test_memory_packs_trusted_promotion_provenance(self) -> None:
        marker = "phase211-trusted-promotion-provenance"
        touched_rel = "src/phase211/provenance.py"
        touched_abs = self.workspace / touched_rel
        touched_abs.parent.mkdir(parents=True, exist_ok=True)
        touched_abs.write_text("PHASE211='provenance'\n", encoding="utf-8")
        fixture = self._create_trusted_import_fixture(marker=marker, touched_files=[touched_rel])
        imported_sc = fixture["imported"]["structuredContent"]
        pack_id = str(imported_sc["pack_id"])
        row_id = str(self._pack_rows(pack_id)[0][0])
        imported_memory_id = str(imported_sc["imported_rows"][0]["memory_id"])
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        promoted_sc = promoted["structuredContent"]
        promoted_memory_id = str(promoted_sc["promoted_rows"][0]["promoted_memory_id"])
        promotion_id = str(promoted_sc["promotion_id"])

        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            imported_row = conn.execute("SELECT * FROM memories WHERE id = ?", (imported_memory_id,)).fetchone()
            promoted_row = conn.execute("SELECT * FROM memories WHERE id = ?", (promoted_memory_id,)).fetchone()
            promoted_topics = conn.execute(
                "SELECT topic, source FROM memory_topics WHERE memory_id = ? ORDER BY topic ASC",
                (promoted_memory_id,),
            ).fetchall()
            imported_topics = conn.execute(
                "SELECT topic FROM memory_topics WHERE memory_id = ? ORDER BY topic ASC",
                (imported_memory_id,),
            ).fetchall()
            promoted_files = conn.execute(
                "SELECT memory_table, path, file_sha FROM memory_files WHERE memory_id = ? ORDER BY path ASC",
                (promoted_memory_id,),
            ).fetchall()
            imported_files = conn.execute(
                "SELECT memory_table, path, file_sha FROM memory_files WHERE memory_id = ? ORDER BY path ASC",
                (imported_memory_id,),
            ).fetchall()
            mapping = conn.execute(
                "SELECT promotion_id, original_import_freshness FROM promoted_pack_rows WHERE pack_id = ? AND row_id_in_pack = ?",
                (pack_id, row_id),
            ).fetchone()
            audit = conn.execute(
                "SELECT promotion_id, row_count FROM promotion_audit WHERE promotion_id = ?",
                (promotion_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(imported_row)
        self.assertIsNotNone(promoted_row)
        self.assertIsNotNone(mapping)
        self.assertIsNotNone(audit)
        assert imported_row is not None
        assert promoted_row is not None
        assert mapping is not None
        self.assertEqual(str(promoted_row["namespace"]), "local")
        self.assertEqual(str(promoted_row["origin"]), "promoted")
        self.assertEqual(str(promoted_row["kind"]), str(imported_row["kind"]))
        self.assertEqual(str(promoted_row["text"]), str(imported_row["text"]))
        self.assertEqual(str(promoted_row["title"] or ""), str(imported_row["title"] or ""))
        self.assertEqual(str(promoted_row["git_sha"] or ""), str(imported_row["git_sha"] or ""))
        self.assertEqual(str(promoted_row["git_branch"] or ""), str(imported_row["git_branch"] or ""))
        self.assertEqual(int(promoted_row["git_dirty"] or 0), int(imported_row["git_dirty"] or 0))
        self.assertEqual(str(promoted_row["import_freshness"] or ""), str(imported_row["import_freshness"] or ""))
        self.assertEqual([str(item["topic"]) for item in promoted_topics], [str(item["topic"]) for item in imported_topics])
        self.assertTrue(all(str(item["source"]) == "promotion" for item in promoted_topics))
        self.assertEqual(
            [(str(item["path"]), str(item["file_sha"] or "")) for item in promoted_files],
            [(str(item["path"]), str(item["file_sha"] or "")) for item in imported_files],
        )
        self.assertEqual(str(mapping["promotion_id"] or ""), promotion_id)

        promoted_meta = json.loads(str(promoted_row["metadata_json"] or "{}"))
        pack_meta = promoted_meta.get("pack_promotion", {}) if isinstance(promoted_meta, dict) else {}
        self.assertEqual(str(pack_meta.get("source_trust_level", "")), "trusted")
        self.assertEqual(str(pack_meta.get("promoted_from_pack_id", "")), pack_id)
        self.assertEqual(str(pack_meta.get("promoted_from_row_id_in_pack", "")), row_id)
        self.assertEqual(str(pack_meta.get("promoted_from_imported_memory_id", "")), imported_memory_id)
        self.assertEqual(str(pack_meta.get("promotion_id", "")), promotion_id)
        self.assertEqual(str(pack_meta.get("source_signer_id", "")), str(fixture["signer_id"]))
        self.assertEqual(
            str(pack_meta.get("source_secret_fingerprint", "")),
            server._secret_fingerprint(str(fixture["secret"])),
        )

    def test_promoted_memory_preserves_import_time_freshness(self) -> None:
        marker = "phase211-import-freshness-preserve"
        touched_rel = "src/phase211/freshness.py"
        touched_abs = self.workspace / touched_rel
        touched_abs.parent.mkdir(parents=True, exist_ok=True)
        touched_abs.write_text("VALUE='before-import'\n", encoding="utf-8")
        fixture = self._create_trusted_import_fixture(marker=marker, touched_files=[touched_rel])
        imported_sc = fixture["imported"]["structuredContent"]
        pack_id = str(imported_sc["pack_id"])
        row_id = str(self._pack_rows(pack_id)[0][0])
        imported_memory_id = str(imported_sc["imported_rows"][0]["memory_id"])

        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            imported_row = conn.execute(
                "SELECT import_freshness FROM memories WHERE id = ?",
                (imported_memory_id,),
            ).fetchone()
            imported_file = conn.execute(
                "SELECT path, file_sha FROM memory_files WHERE memory_id = ? ORDER BY path ASC LIMIT 1",
                (imported_memory_id,),
            ).fetchone()
        finally:
            conn.close()
        assert imported_row is not None
        imported_freshness = str(imported_row["import_freshness"] or "")
        imported_file_sha = str((imported_file["file_sha"] if imported_file is not None else "") or "")

        touched_abs.write_text("VALUE='after-import-mutated'\n", encoding="utf-8")
        promoted = self._pack_promote(pack_id=pack_id, row_ids=[row_id], confirm_promote=True)
        promoted_memory_id = str(promoted["structuredContent"]["promoted_rows"][0]["promoted_memory_id"])

        conn = sqlite3.connect(str(self.sqlite_file))
        conn.row_factory = sqlite3.Row
        try:
            promoted_row = conn.execute(
                "SELECT import_freshness FROM memories WHERE id = ?",
                (promoted_memory_id,),
            ).fetchone()
            promoted_file = conn.execute(
                "SELECT path, file_sha FROM memory_files WHERE memory_id = ? ORDER BY path ASC LIMIT 1",
                (promoted_memory_id,),
            ).fetchone()
        finally:
            conn.close()
        assert promoted_row is not None
        self.assertEqual(str(promoted_row["import_freshness"] or ""), imported_freshness)
        self.assertEqual(str((promoted_file["file_sha"] if promoted_file is not None else "") or ""), imported_file_sha)

    def test_trusted_import_available_stability(self) -> None:
        marker = "phase211-trusted-available-stability"
        fixture = self._create_signed_pack_fixture(marker=marker)
        pack_path = str(fixture["pack_path"])
        secret = str(fixture["secret"])
        signer_id = str(fixture["signer_id"])

        first = self._pack_inspect(pack_path=pack_path, verification_secret=secret)["structuredContent"]
        second = self._pack_inspect(pack_path=pack_path, verification_secret=secret)["structuredContent"]
        self.assertEqual(str((first.get("signature") or {}).get("trust_classification", "")), "trusted_signer")
        self.assertEqual(str((second.get("signature") or {}).get("trust_classification", "")), "trusted_signer")
        self.assertTrue(bool(first.get("trusted_import_available")))
        self.assertTrue(bool(second.get("trusted_import_available")))
        self.assertEqual(first.get("signature"), second.get("signature"))

        self._signer_disable(signer_id=signer_id)
        disabled = self._pack_inspect(pack_path=pack_path, verification_secret=secret)["structuredContent"]
        self.assertEqual(str((disabled.get("signature") or {}).get("trust_classification", "")), "disabled_signer")
        self.assertFalse(bool(disabled.get("trusted_import_available")))

        self._signer_enable(signer_id=signer_id)
        reenabled = self._pack_inspect(pack_path=pack_path, verification_secret=secret)["structuredContent"]
        self.assertEqual(str((reenabled.get("signature") or {}).get("trust_classification", "")), "trusted_signer")
        self.assertTrue(bool(reenabled.get("trusted_import_available")))

        conn = sqlite3.connect(str(self.sqlite_file))
        try:
            conn.execute(
                "UPDATE trusted_signers SET trust_level = 'blocked', updated_at = ? WHERE signer_id = ?",
                (server.now_iso(), signer_id),
            )
            conn.commit()
        finally:
            conn.close()
        blocked = self._pack_inspect(pack_path=pack_path, verification_secret=secret)["structuredContent"]
        self.assertEqual(str((blocked.get("signature") or {}).get("trust_classification", "")), "blocked_signer")
        self.assertFalse(bool(blocked.get("trusted_import_available")))

        wrong = self._pack_inspect(
            pack_path=pack_path,
            verification_secret="phase211-stability-wrong-secret-0123456789012",
        )["structuredContent"]
        self.assertEqual(str((wrong.get("signature") or {}).get("trust_classification", "")), "invalid_signature")
        self.assertFalse(bool(wrong.get("trusted_import_available")))

        unknown_secret = "phase211-stability-unknown-secret-01234567890123"
        unknown_row = self.record(f"{marker} unknown signer source", kind="context_block")
        unknown_topic = f"{marker}-unknown-topic"
        add_unknown = server.topic_add({"memory_id": str(unknown_row["id"]), "topic": unknown_topic, "source": "operator"})
        self.assertFalse(add_unknown["isError"], add_unknown)
        unknown_pack, _ = self._create_signed_exported_pack(
            pack_name=f"{marker}_unknown",
            output_dir=self.root / f"{marker}_unknown",
            signer_id=f"{marker}.unknown.signer",
            signing_secret=unknown_secret,
            topics=[unknown_topic],
            kinds=["context_block"],
        )
        unknown = self._pack_inspect(
            pack_path=str(unknown_pack),
            verification_secret=unknown_secret,
        )["structuredContent"]
        self.assertEqual(str((unknown.get("signature") or {}).get("trust_classification", "")), "unknown_signer")
        self.assertFalse(bool(unknown.get("trusted_import_available")))

    def test_memory_packs_mixed_trust_list_review_preview_promote(self) -> None:
        trusted_fixture = self._create_trusted_import_fixture(marker="phase211-mixed-list-trusted")
        trusted_sc = trusted_fixture["imported"]["structuredContent"]
        trusted_pack_id = str(trusted_sc["pack_id"])
        trusted_row_id = str(self._pack_rows(trusted_pack_id)[0][0])

        quarantine_fixture = self._create_signed_pack_fixture(marker="phase211-mixed-list-quarantine")
        quarantine_import = self._pack_import(
            pack_path=str(quarantine_fixture["pack_path"]),
            allow_unsigned_quarantine=True,
        )
        quarantine_sc = quarantine_import["structuredContent"]
        quarantine_pack_id = str(quarantine_sc["pack_id"])
        quarantine_row_id = str(self._pack_rows(quarantine_pack_id)[0][0])

        listed_all = self._pack_list_imports(limit=20)
        listed_trusted = self._pack_list_imports(limit=20, trust_level="trusted")
        listed_quarantine = self._pack_list_imports(limit=20, trust_level="quarantine")
        all_trust_levels = {str(item.get("trust_level", "")) for item in listed_all["structuredContent"]["packs"]}
        trusted_pack_ids = {str(item.get("pack_id", "")) for item in listed_trusted["structuredContent"]["packs"]}
        quarantine_pack_ids = {str(item.get("pack_id", "")) for item in listed_quarantine["structuredContent"]["packs"]}
        self.assertIn("trusted", all_trust_levels)
        self.assertIn("quarantine", all_trust_levels)
        self.assertIn(trusted_pack_id, trusted_pack_ids)
        self.assertIn(quarantine_pack_id, quarantine_pack_ids)

        trusted_review = self._pack_review_import(pack_id=trusted_pack_id, include_samples=True, sample_limit=5)
        quarantine_review = self._pack_review_import(pack_id=quarantine_pack_id, include_samples=True, sample_limit=5)
        self.assertTrue(
            all(str(item.get("namespace", "")).startswith("pack:trusted:") for item in trusted_review["structuredContent"]["samples"])
        )
        self.assertTrue(
            all(str(item.get("namespace", "")).startswith("pack:quarantine:") for item in quarantine_review["structuredContent"]["samples"])
        )

        trusted_preview = self._pack_promote_preview(pack_id=trusted_pack_id, row_ids=[trusted_row_id], include_samples=False)
        quarantine_preview = self._pack_promote_preview(
            pack_id=quarantine_pack_id,
            row_ids=[quarantine_row_id],
            include_samples=False,
        )
        self.assertFalse(trusted_preview["isError"], trusted_preview)
        self.assertFalse(quarantine_preview["isError"], quarantine_preview)

        trusted_preview_warnings = (trusted_preview["structuredContent"] or {}).get("warnings", [])
        trusted_preview_codes = {str(item.get("code", "")) for item in trusted_preview_warnings if isinstance(item, dict)}
        self.assertIn("trusted_import_source", trusted_preview_codes)
        self.assertNotIn("promoting_from_trusted_import", trusted_preview_codes)
        self.assertNotIn("promoted_from_trusted_import", trusted_preview_codes)
        self.assertTrue(
            any(
                isinstance(item, dict)
                and str(item.get("code", "")) == "trusted_import_source"
                and str(item.get("phase", "")) == "preview"
                for item in trusted_preview_warnings
            )
        )

        trusted_promote = self._pack_promote(
            pack_id=trusted_pack_id,
            row_ids=[trusted_row_id],
            confirm_promote=True,
        )
        promoted_memory_id = str(trusted_promote["structuredContent"]["promoted_rows"][0]["promoted_memory_id"])
        promote_warnings = (trusted_promote["structuredContent"] or {}).get("warnings", [])
        promote_codes = {str(item.get("code", "")) for item in promote_warnings if isinstance(item, dict)}
        self.assertIn("trusted_import_source", promote_codes)
        self.assertNotIn("promoting_from_trusted_import", promote_codes)
        self.assertNotIn("promoted_from_trusted_import", promote_codes)
        self.assertTrue(
            any(
                isinstance(item, dict)
                and str(item.get("code", "")) == "trusted_import_source"
                and str(item.get("phase", "")) == "promotion"
                for item in promote_warnings
            )
        )

        reviewed_after = self._pack_review_import(pack_id=trusted_pack_id, include_samples=True, sample_limit=10)
        promoted_rows = [item for item in reviewed_after["structuredContent"]["samples"] if str(item.get("row_id_in_pack", "")) == trusted_row_id]
        self.assertTrue(promoted_rows)
        self.assertEqual(str(promoted_rows[0].get("promoted_to_memory_id", "")), promoted_memory_id)

    def test_memory_packs_trusted_import_policy_docs_consistency(self) -> None:
        readme = (Path(__file__).resolve().parent / "README.md").read_text(encoding="utf-8")
        tool_reference = (Path(__file__).resolve().parent / "docs" / "tool_reference.md").read_text(encoding="utf-8")
        changelog = (Path(__file__).resolve().parent / "CHANGELOG.md").read_text(encoding="utf-8")
        combined = f"{readme}\n{tool_reference}"
        self.assertIn("Trusted Import Policy", combined)
        self.assertIn("Trusted import is NOT local adoption", combined)
        self.assertIn("Trusted import is NOT automatic promotion", combined)
        self.assertIn("Trusted import is NOT default retrieval", combined)
        self.assertIn("pack:trusted:<pack_id>", combined)
        self.assertIn("include_imported=true", combined)
        self.assertIn("include_quarantine=true", combined)
        self.assertIn("manual promotion", combined.lower())
        self.assertIn("local hmac", combined.lower())
        self.assertIn("not public-key", combined.lower())
        self.assertIn("not non-repudiation", combined.lower())
        self.assertIn("persistent secret store", combined.lower())
        self.assertIn("revocation", combined.lower())
        self.assertIn("## 0.21.1", changelog)
        self.assertIn("not a new feature phase", changelog.lower())

    def test_memory_packs_no_trusted_import_for_unsigned_or_unverified(self) -> None:
        marker = "phase211-no-trusted-for-unsigned-or-unverified"
        row = self.record(f"{marker} unsigned source", kind="context_block")
        topic = f"{marker}-topic"
        add = server.topic_add({"memory_id": str(row["id"]), "topic": topic, "source": "operator"})
        self.assertFalse(add["isError"], add)
        unsigned_pack = self._create_exported_pack(
            pack_name=f"{marker}_unsigned",
            output_dir=self.root / f"{marker}_unsigned",
            topics=[topic],
            kinds=["context_block"],
        )
        before = self._table_count("imported_packs")
        unsigned_fail = self._pack_import_error(
            pack_path=str(unsigned_pack),
            allow_trusted_import=True,
            verification_secret="phase211-unsigned-trusted-attempt-secret-012345678901",
        )
        self.assertEqual(self._pack_error_code(unsigned_fail), "trusted_import_requires_verified_trusted_signer")

        unknown_secret = "phase211-no-trusted-unknown-secret-01234567890123"
        unknown_row = self.record(f"{marker} unknown signer source", kind="context_block")
        unknown_topic = f"{marker}-unknown-topic"
        add_unknown = server.topic_add({"memory_id": str(unknown_row["id"]), "topic": unknown_topic, "source": "operator"})
        self.assertFalse(add_unknown["isError"], add_unknown)
        unknown_pack, _ = self._create_signed_exported_pack(
            pack_name=f"{marker}_unknown",
            output_dir=self.root / f"{marker}_unknown",
            signer_id=f"{marker}.unknown.signer",
            signing_secret=unknown_secret,
            topics=[unknown_topic],
            kinds=["context_block"],
        )
        unknown_fail = self._pack_import_error(
            pack_path=str(unknown_pack),
            allow_trusted_import=True,
            verification_secret=unknown_secret,
        )
        self.assertEqual(self._pack_error_code(unknown_fail), "trusted_import_requires_verified_trusted_signer")
        no_secret_fail = self._pack_import_error(
            pack_path=str(unknown_pack),
            allow_trusted_import=True,
        )
        self.assertEqual(self._pack_error_code(no_secret_fail), "trusted_import_requires_verification_secret")
        self.assertEqual(before, self._table_count("imported_packs"))

    def test_memory_packs_v1_readiness_report_gate(self) -> None:
        report_root = Path(__file__).resolve().parent / "_test_results" / "memory_packs_trusted_import_stabilization"
        if not report_root.exists():
            self.skipTest("synthetic readiness report not present in this checkout")
        run_dirs = sorted(path for path in report_root.iterdir() if path.is_dir() and path.name.startswith("phase211_run_"))
        if not run_dirs:
            self.skipTest("no phase211 readiness report directories found")
        latest = run_dirs[-1]
        report_path = latest / "memory_packs_trusted_import_stabilization_report.json"
        if not report_path.exists():
            self.skipTest("latest readiness report JSON not found")
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        checks = payload.get("checks", [])
        self.assertIsInstance(checks, list)
        status_rows = [item for item in checks if isinstance(item, dict) and str(item.get("name", "")) == "memory_packs_v1_status"]
        self.assertTrue(status_rows)
        status_row = status_rows[0]
        all_other_passed = all(
            bool(item.get("passed"))
            for item in checks
            if isinstance(item, dict) and str(item.get("name", "")) != "memory_packs_v1_status"
        )
        expected_ready = "ready" if all_other_passed else "not_ready"
        details_text = str(status_row.get("details", ""))
        self.assertIn(expected_ready, details_text)
        if all_other_passed:
            self.assertTrue(bool(status_row.get("passed")))
        else:
            self.assertFalse(bool(status_row.get("passed")))

        readme = (Path(__file__).resolve().parent / "README.md").read_text(encoding="utf-8")
        claim = "Complete for practical local export/import workflows"
        if all_other_passed:
            self.assertIn(claim, readme)
        else:
            self.assertNotIn(claim, readme)

    def test_memory_packs_sync_parity_if_siblings_present(self) -> None:
        base = Path(__file__).resolve().parent
        workspace_root = base.parent.parent.parent.parent
        copies = {
            "agentic": base,
            "mnemo": workspace_root / "mnemo",
            "pub_mnemo": workspace_root / "pub_mnemo",
        }
        missing = [name for name, path in copies.items() if not path.exists()]
        if missing:
            self.skipTest(f"sibling copy not present: {', '.join(missing)}")

        files = [
            "server.py",
            "test_server.py",
            "pyproject.toml",
            "README.md",
            "CHANGELOG.md",
            "docs/storage.md",
            "docs/tool_reference.md",
            "git_context.py",
            "smoke_test.py",
        ]
        for rel in files:
            digests: dict[str, str] = {}
            for label, root in copies.items():
                file_path = root / rel
                self.assertTrue(file_path.exists(), f"missing file for parity check: {label}:{rel}")
                digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
                digests[label] = digest
            if len(set(digests.values())) != 1:
                self.skipTest(f"sync parity mismatch for {rel}: {digests}")

    def test_memory_packs_moderate_scale_characterization(self) -> None:
        marker = "phase211_moderate_scale"
        topic = f"{marker}-topic"
        kinds = ["context_block", "hippocampus_entry"]
        for idx in range(500):
            kind = kinds[idx % len(kinds)]
            recorded = self.record(
                f"{marker} row {idx}",
                kind=kind,
                title=f"{marker} title {idx}",
            )
            topic_result = server.topic_add(
                {"memory_id": str(recorded["id"]), "topic": topic, "source": "operator"}
            )
            self.assertFalse(topic_result["isError"], topic_result)

        exported = self._pack_export(
            pack_name=f"{marker}_pack",
            output_dir=str(self.root / f"{marker}_pack"),
            topics=[topic],
            kinds=["context_block", "hippocampus_entry"],
            limit=600,
            allow_unsigned=True,
        )
        pack_path = Path(str(exported["structuredContent"]["output_path"]))
        inspect = self._pack_inspect(pack_path=str(pack_path))
        self.assertEqual(str(inspect["structuredContent"]["status"]), "valid")

        imported = self._pack_import(pack_path=str(pack_path), allow_unsigned_quarantine=True)
        imported_sc = imported["structuredContent"]
        self.assertEqual(int(imported_sc["imported"]["memory_count"]), 500)
        pack_id = str(imported_sc["pack_id"])
        review = self._pack_review_import(pack_id=pack_id, include_samples=False, limit=600)
        self.assertEqual(int(review["structuredContent"]["selection"]["total_pack_rows"]), 500)


class MemoryPacksLargeSyntheticHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._tmp = tempfile.TemporaryDirectory()
        cls._tmp_path = Path(cls._tmp.name)
        cls._runner_path = (
            Path(__file__).resolve().parent
            / "_test_results"
            / "memory_packs_large_synthetic"
            / "run_check.py"
        )
        if not cls._runner_path.exists():
            raise unittest.SkipTest("large synthetic runner script is not present")

        cls._quick_run_dir = cls._tmp_path / "quick_run"
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(
            [
                sys.executable,
                str(cls._runner_path),
                "--profile",
                "quick",
                "--seed",
                "424242",
                "--work-dir",
                str(cls._quick_run_dir),
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        cls._quick_proc = proc
        if proc.returncode != 0:
            raise AssertionError(
                "quick synthetic runner failed\nstdout:\n{0}\nstderr:\n{1}".format(
                    proc.stdout, proc.stderr
                )
            )
        cls._report_json_path = cls._quick_run_dir / "memory_packs_large_synthetic_report.json"
        cls._report_md_path = cls._quick_run_dir / "memory_packs_large_synthetic_report.md"
        if not cls._report_json_path.exists():
            raise AssertionError(f"expected synthetic report missing: {cls._report_json_path}")
        cls._report = json.loads(cls._report_json_path.read_text(encoding="utf-8"))
        cls._checks = {
            str(item.get("name", "")): bool(item.get("passed"))
            for item in cls._report.get("checks", [])
            if isinstance(item, dict)
        }

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._tmp.cleanup()
        finally:
            super().tearDownClass()

    def _assert_check_pass(self, name: str) -> None:
        self.assertIn(name, self._checks, f"missing check row: {name}")
        self.assertTrue(self._checks[name], f"synthetic check failed: {name}")

    def test_synthetic_memory_pack_runner_exists(self) -> None:
        self.assertTrue(self._runner_path.exists(), self._runner_path)

    def test_synthetic_runner_quick_profile_passes(self) -> None:
        summary = self._report.get("summary", {})
        self.assertEqual(str(summary.get("status", "")), "ready")
        self.assertEqual(int(summary.get("failed", 0)), 0)

    def test_synthetic_dataset_deterministic_for_seed(self) -> None:
        spec = importlib.util.spec_from_file_location("phase213_runner", str(self._runner_path))
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        first = module.build_dataset_plan("quick", 424242)
        second = module.build_dataset_plan("quick", 424242)
        self.assertEqual(first["cohort_counts"], second["cohort_counts"])
        self.assertEqual(first["cohort_signature"], second["cohort_signature"])
        self.assertEqual(first["dataset_signature"], second["dataset_signature"])

    def test_synthetic_export_preview_counts_match(self) -> None:
        self._assert_check_pass("synthetic_export_preview_counts_match")

    def test_synthetic_redaction_cohort_detects_expected_categories(self) -> None:
        self._assert_check_pass("synthetic_redaction_cohort_detects_expected_categories")

    def test_synthetic_export_import_roundtrip_counts(self) -> None:
        self._assert_check_pass("synthetic_export_import_roundtrip_counts")

    def test_synthetic_trusted_import_flow_quick(self) -> None:
        self._assert_check_pass("synthetic_trusted_import_flow_quick")

    def test_synthetic_quarantine_flow_quick(self) -> None:
        self._assert_check_pass("synthetic_quarantine_flow_quick")

    def test_synthetic_reports_do_not_contain_raw_secrets(self) -> None:
        payload_text = self._report_json_path.read_text(encoding="utf-8")
        self._assert_check_pass("synthetic_reports_do_not_contain_raw_secrets")
        self.assertNotIn("synthetic-trusted-signing-secret-0000424242-0123456789abcdef", payload_text)
        self.assertNotIn("synthetic-quarantine-signing-secret-0000424242-fedcba9876543210", payload_text)
        self.assertNotIn("synthetic-wrong-secret-0000424242-11112222333344445555", payload_text)

    def test_memory_pack_export_prompt_exists_and_is_deterministic(self) -> None:
        prompt_path = (
            Path(__file__).resolve().parent
            / ".github"
            / "prompts"
            / "mnemo.memory-pack-export.prompt.md"
        )
        self.assertTrue(prompt_path.exists(), prompt_path)
        text = prompt_path.read_text(encoding="utf-8")
        self.assertIn("pack_preview", text)
        self.assertIn("pack_redaction_preview", text)
        self.assertIn("pack_export", text)
        self.assertIn("mnemo.memory_group_discover", text)
        self.assertIn("Never use fuzzy query results as the final export selector", text)
        self.assertIn("mnemo.pack_preview", text)
        self.assertIn('"group_id": "<exact-group-id-from-catalog>"', text)
        self.assertNotIn('"<MEMORY_IDS>"', text)
        self.assertIn("local HMAC", text)

    def test_memory_pack_import_prompt_exists_and_is_safe(self) -> None:
        prompt_path = (
            Path(__file__).resolve().parent
            / ".github"
            / "prompts"
            / "mnemo.memory-pack-import.prompt.md"
        )
        self.assertTrue(prompt_path.exists(), prompt_path)
        text = prompt_path.read_text(encoding="utf-8")
        self.assertIn("pack_landing_list", text)
        self.assertIn("pack_inspect", text)
        self.assertIn("pack_import", text)
        self.assertIn("pack_review_import", text)
        self.assertIn("pack_promote_preview", text)
        self.assertIn("pack_promote", text)
        self.assertIn("Never auto-promote", text)
        self.assertIn("confirm_promote=true", text)
        self.assertIn("mnemo.pack_import", text)

    def test_no_large_profile_in_unit_tests_by_default(self) -> None:
        spec = importlib.util.spec_from_file_location("phase213_runner_default", str(self._runner_path))
        self.assertIsNotNone(spec)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.assertEqual(str(module.DEFAULT_PROFILE), "quick")
        self.assertNotIn("large", set(module.UNITTEST_SAFE_PROFILES))
        cmd_text = " ".join(self._quick_proc.args if isinstance(self._quick_proc.args, list) else [str(self._quick_proc.args)])
        self.assertIn("--profile quick", cmd_text)


class MemoryPacksLiveUxLabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._mnemo_root = Path(__file__).resolve().parent
        cls._workspace_root = None
        for candidate in [cls._mnemo_root.parent, cls._mnemo_root.parent.parent, cls._mnemo_root.parent.parent.parent, cls._mnemo_root.parent.parent.parent.parent]:
            if (candidate / "agentic" / "scripts").exists():
                cls._workspace_root = candidate
                break
        if cls._workspace_root is None:
            cls._workspace_root = cls._mnemo_root.parent.parent.parent.parent
        cls._agentic_root = cls._workspace_root / "agentic"
        cls._scripts_root = cls._agentic_root / "scripts"
        cls._seed_script = cls._scripts_root / "mnemo_seed_synthetic_live.py"
        cls._stats_script = cls._scripts_root / "mnemo_synthetic_stats.py"
        cls._smoke_script = cls._scripts_root / "mnemo_ux_lab_smoke.py"

    def _load_script_module(self, path: Path, module_name: str):
        script_dir = str(path.parent)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        self.assertIsNotNone(spec)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def _run_script(self, script: Path, *args: str, timeout_ms: int = 120000):
        return subprocess.run(
            [sys.executable, str(script), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_ms / 1000.0,
        )

    def test_runtime_version_surfaces_for_live_ux_lab(self) -> None:
        pyproject = (self._mnemo_root / "pyproject.toml").read_text(encoding="utf-8")
        readme = (self._mnemo_root / "README.md").read_text(encoding="utf-8")
        changelog = (self._mnemo_root / "CHANGELOG.md").read_text(encoding="utf-8")
        pyproject_match = re.search(r'^version = "([^"]+)"', pyproject, re.M)
        readme_match = re.search(r"Current version: \*\*([^\*]+)\*\*", readme)
        changelog_match = re.search(r"^##\s+([^\n]+)", changelog, re.M)
        self.assertIsNotNone(pyproject_match)
        self.assertIsNotNone(readme_match)
        self.assertIsNotNone(changelog_match)
        self.assertEqual(server.SERVER_VERSION, pyproject_match.group(1))
        self.assertEqual(server.SERVER_VERSION, readme_match.group(1))
        self.assertEqual(server.SERVER_VERSION, changelog_match.group(1))

    def test_live_synthetic_profile_sizes_are_small(self) -> None:
        module = self._load_script_module(self._seed_script, "mnemo_live_seed_sizes")
        self.assertEqual(module.PROFILE_ROW_TARGETS["quick"], 120)
        self.assertEqual(module.PROFILE_ROW_TARGETS["medium"], 400)
        self.assertEqual(module.PROFILE_ROW_TARGETS["large"], 1000)

    def test_live_synthetic_seeder_exists(self) -> None:
        self.assertTrue(self._seed_script.exists(), self._seed_script)

    def test_live_synthetic_seeder_dry_run_deterministic(self) -> None:
        if not self._seed_script.exists():
            self.skipTest("agentic seeder script not present")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sqlite_path = root / "mnemo.sqlite"
            workspace_root = root / "workspace"
            report_a = root / "report_a"
            report_b = root / "report_b"
            workspace_root.mkdir(parents=True)
            args = [
                sys.executable,
                str(self._seed_script),
                "--profile",
                "quick",
                "--seed",
                "424242",
                "--target-sqlite",
                str(sqlite_path),
                "--workspace-root",
                str(workspace_root),
                "--run-id",
                "dry-run-deterministic",
                "--report-dir",
                str(report_a),
                "--dry-run",
            ]
            first = subprocess.run(args, capture_output=True, text=True, check=False)
            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
            second_args = list(args)
            second_args[second_args.index(str(report_a))] = str(report_b)
            second = subprocess.run(second_args, capture_output=True, text=True, check=False)
            self.assertEqual(second.returncode, 0, second.stderr or second.stdout)
            first_manifest = json.loads((report_a / "dry-run-deterministic.json").read_text(encoding="utf-8"))
            second_manifest = json.loads((report_b / "dry-run-deterministic.json").read_text(encoding="utf-8"))
            self.assertEqual(first_manifest["deterministic_plan_checksum"], second_manifest["deterministic_plan_checksum"])
            self.assertEqual(first_manifest["counts_by_kind"], second_manifest["counts_by_kind"])
            self.assertEqual(first_manifest["export_cohort_counts"], second_manifest["export_cohort_counts"])
            self.assertIn("preflight", first_manifest)
            self.assertIn("requested_count", first_manifest)

    def test_live_synthetic_seeder_requires_allow_live_state_for_agentic_state(self) -> None:
        if not self._seed_script.exists():
            self.skipTest("agentic seeder script not present")
        proc = self._run_script(
            self._seed_script,
            "--profile",
            "quick",
            "--seed",
            "424242",
            "--target-sqlite",
            str(self._agentic_root / "state" / "mnemo" / "mnemo.sqlite"),
            "--workspace-root",
            str(self._agentic_root),
            "--run-id",
            "needs-live-allow",
            "--dry-run",
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--allow-live-state", proc.stderr + proc.stdout)

    def test_live_synthetic_seeder_temp_db_quick_profile(self) -> None:
        if not self._seed_script.exists():
            self.skipTest("agentic seeder script not present")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sqlite_path = root / "mnemo.sqlite"
            workspace_root = root / "workspace"
            report_dir = root / "reports"
            workspace_root.mkdir(parents=True)
            proc = self._run_script(
                self._seed_script,
                "--profile",
                "quick",
                "--seed",
                "424242",
                "--target-sqlite",
                str(sqlite_path),
                "--workspace-root",
                str(workspace_root),
                "--run-id",
                "temp-quick-seed",
                "--report-dir",
                str(report_dir),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
            manifest = json.loads((report_dir / "temp-quick-seed.json").read_text(encoding="utf-8"))
            self.assertEqual(int(manifest["inserted_memory_count"]), 120)
            self.assertLess(float(manifest["preflight"]["estimated_final_db_mb"]), 25.0)
            conn = sqlite3.connect(str(sqlite_path))
            try:
                row = conn.execute(
                    "SELECT COUNT(DISTINCT memory_id) FROM memory_topics WHERE topic = ?",
                    ("synthetic:run:temp-quick-seed",),
                ).fetchone()
                self.assertEqual(int(row[0]), 120)
            finally:
                conn.close()
            self.assertLess(sqlite_path.stat().st_size, 25 * 1024 * 1024)

    def test_live_synthetic_live_cap_blocks_oversized_seed(self) -> None:
        if not self._seed_script.exists():
            self.skipTest("agentic seeder script not present")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live_like = self._agentic_root / "state" / "mnemo" / "mnemo.sqlite"
            proc = self._run_script(
                self._seed_script,
                "--profile",
                "quick",
                "--count",
                "1500",
                "--max-live-count",
                "1000",
                "--target-sqlite",
                str(live_like),
                "--workspace-root",
                str(self._agentic_root),
                "--run-id",
                "too-many-live",
                "--allow-live-state",
                "--dry-run",
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("live_seed_count_exceeds_cap", proc.stderr + proc.stdout)

    def test_live_synthetic_dry_run_reports_size_estimate(self) -> None:
        if not self._seed_script.exists():
            self.skipTest("agentic seeder script not present")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sqlite_path = root / "mnemo.sqlite"
            workspace_root = root / "workspace"
            report_dir = root / "reports"
            workspace_root.mkdir(parents=True)
            proc = self._run_script(
                self._seed_script,
                "--profile",
                "quick",
                "--target-sqlite",
                str(sqlite_path),
                "--workspace-root",
                str(workspace_root),
                "--run-id",
                "size-dry-run",
                "--report-dir",
                str(report_dir),
                "--dry-run",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
            self.assertIn("PREFLIGHT", proc.stdout)
            manifest = json.loads((report_dir / "size-dry-run.json").read_text(encoding="utf-8"))
            preflight = manifest["preflight"]
            self.assertIn("current_db_mb", preflight)
            self.assertIn("estimated_final_db_mb", preflight)
            self.assertEqual(int(preflight["requested_count"]), 120)

    def test_synthetic_stats_reports_db_size_and_bloat_warnings(self) -> None:
        if not self._stats_script.exists():
            self.skipTest("agentic stats script not present")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sqlite_path = root / "mnemo.sqlite"
            # Reuse the seeder for a small temp corpus.
            workspace_root = root / "workspace"
            report_dir = root / "reports"
            workspace_root.mkdir(parents=True)
            seeded = self._run_script(
                self._seed_script,
                "--profile",
                "quick",
                "--target-sqlite",
                str(sqlite_path),
                "--workspace-root",
                str(workspace_root),
                "--run-id",
                "stats-small",
                "--report-dir",
                str(report_dir),
            )
            self.assertEqual(seeded.returncode, 0, seeded.stderr or seeded.stdout)
            stats_proc = self._run_script(
                self._stats_script,
                "--target-sqlite",
                str(sqlite_path),
                "--run-id",
                "stats-small",
                "--json",
            )
            self.assertEqual(stats_proc.returncode, 0, stats_proc.stderr or stats_proc.stdout)
            payload = json.loads(stats_proc.stdout)
            self.assertIn("db_size_mb", payload)
            self.assertIn("warnings", payload)
            self.assertIsInstance(payload["warnings"], list)
            self.assertIn("memory_group_discover_summary", payload)

    def test_cleanup_run_dry_run_only_targets_synthetic_rows(self) -> None:
        if not self._seed_script.exists():
            self.skipTest("agentic seeder script not present")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sqlite_path = root / "mnemo.sqlite"
            workspace_root = root / "workspace"
            report_dir = root / "reports"
            workspace_root.mkdir(parents=True)
            seeded = self._run_script(
                self._seed_script,
                "--profile",
                "quick",
                "--target-sqlite",
                str(sqlite_path),
                "--workspace-root",
                str(workspace_root),
                "--run-id",
                "cleanup-small",
                "--report-dir",
                str(report_dir),
            )
            self.assertEqual(seeded.returncode, 0, seeded.stderr or seeded.stdout)
            os.environ["MNEMO_STORE"] = "sqlite"
            os.environ["MNEMO_SQLITE_FILE"] = str(sqlite_path)
            os.environ["MNEMO_FILE"] = str(root / "memory.json")
            os.environ["MNEMO_WORKSPACE_ROOT"] = str(workspace_root)
            server._SQLITE_BOOTSTRAPPED.clear()
            local = server.record_memory({"text": "non synthetic local row", "kind": "context_block"})
            self.assertFalse(local["isError"], local)
            cleanup = self._run_script(
                self._seed_script,
                "--target-sqlite",
                str(sqlite_path),
                "--workspace-root",
                str(workspace_root),
                "--cleanup-run",
                "cleanup-small",
                "--report-dir",
                str(report_dir),
                "--dry-run",
            )
            self.assertEqual(cleanup.returncode, 0, cleanup.stderr or cleanup.stdout)
            payload = json.loads((report_dir / "cleanup-small.json").read_text(encoding="utf-8"))
            self.assertGreater(int(payload["cleanup_result"]["memories_targeted"]), 0)
            conn = sqlite3.connect(str(sqlite_path))
            try:
                synthetic_count = int(conn.execute("SELECT COUNT(*) FROM memory_topics WHERE topic = ?", ("synthetic:run:cleanup-small",)).fetchone()[0])
                total_memories = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            finally:
                conn.close()
            self.assertGreater(synthetic_count, 0)
            self.assertGreater(total_memories, synthetic_count)

    def test_no_version_or_changelog_change_for_ux_lab_sizing(self) -> None:
        pyproject = (self._mnemo_root / "pyproject.toml").read_text(encoding="utf-8")
        readme = (self._mnemo_root / "README.md").read_text(encoding="utf-8")
        changelog = (self._mnemo_root / "CHANGELOG.md").read_text(encoding="utf-8")
        pyproject_match = re.search(r'^version = "([^"]+)"', pyproject, re.M)
        readme_match = re.search(r"Current version: \*\*([^\*]+)\*\*", readme)
        changelog_match = re.search(r"^##\s+([^\n]+)", changelog, re.M)
        self.assertIsNotNone(pyproject_match)
        self.assertIsNotNone(readme_match)
        self.assertIsNotNone(changelog_match)
        self.assertEqual(server.SERVER_VERSION, pyproject_match.group(1))
        self.assertEqual(server.SERVER_VERSION, readme_match.group(1))
        self.assertEqual(server.SERVER_VERSION, changelog_match.group(1))

    def test_synthetic_stats_script_exists(self) -> None:
        self.assertTrue(self._stats_script.exists(), self._stats_script)

    def test_memory_list_select_prompt_is_stubbed_to_export_prompt(self) -> None:
        prompt_path = self._mnemo_root / ".github" / "prompts" / "mnemo.memory-list-select.prompt.md"
        self.assertTrue(prompt_path.exists(), prompt_path)
        text = prompt_path.read_text(encoding="utf-8")
        self.assertIn("/mnemo.memory-pack-export", text)
        self.assertIn("merged into", text)
        self.assertIn("use `/mnemo.memory-pack-export` with no argument", text)
        self.assertIn("use `/mnemo.memory-pack-export <topic-or-group>`", text)
        self.assertNotIn("mnemo.memory_group_discover", text)
        self.assertNotIn("mnemo.pack_export", text)

    def test_memory_pack_export_prompt_is_interactive_and_deterministic(self) -> None:
        prompt_path = self._mnemo_root / ".github" / "prompts" / "mnemo.memory-pack-export.prompt.md"
        self.assertTrue(prompt_path.exists(), prompt_path)
        text = prompt_path.read_text(encoding="utf-8")
        self.assertIn("If invoked with no input", text)
        self.assertIn("mnemo.memory_group_discover", text)
        self.assertIn('"output_mode": "catalog"', text)
        self.assertIn('"catalog_for": "export"', text)
        self.assertIn("Use `catalog.options` directly", text)
        self.assertIn("Do not reconstruct option values", text)
        self.assertIn("Do not invent group IDs", text)
        self.assertIn('must never say "No suitable list available" when `catalog.options` is non-empty', text)
        self.assertIn("vscode_askQuestions", text)
        self.assertIn("mnemo.pack_preview", text)
        self.assertIn("mnemo.pack_redaction_preview", text)
        self.assertIn("mnemo.pack_export", text)
        self.assertIn("exact topic", text)
        self.assertIn("exact group_id", text)
        self.assertIn("group label", text)
        self.assertIn("memory_ids", text)
        self.assertIn("Never use fuzzy query results as the final export selector", text)
        self.assertNotIn("- `mnemo.search`", text)
        self.assertIn("Do not call `mnemo.search`.", text)
        self.assertIn("Do not require a handoff object for normal use", text)
        self.assertIn("Do not transfer raw `memory_ids` through chat or placeholders.", text)
        self.assertNotIn('"<MEMORY_IDS>"', text)
        self.assertIn('"group_id": "<exact-group-id-from-catalog>"', text)
        self.assertIn('"scope": "core_plus_related"', text)
        self.assertIn("Run `mnemo.pack_preview` before `mnemo.pack_export`", text)
        self.assertIn("Run `mnemo.pack_redaction_preview` before `mnemo.pack_export`", text)
        self.assertIn("Require explicit approval before `mnemo.pack_export`", text)
        self.assertIn("Never call `mnemo.pack_export` without `pack_name`", text)
        self.assertIn("Never call `mnemo.pack_export` without either `allow_unsigned=true` or `sign_pack=true`", text)
        self.assertIn("allow_limited_export=true", text)
        self.assertIn("The output extension is `.mem`", text)
        self.assertIn("content/file_fingerprints.json", text)
        self.assertIn("paths and hashes, not file contents", text)
        self.assertIn("local HMAC", text)

    def test_memory_pack_import_prompt_is_interactive_and_safe(self) -> None:
        prompt_path = self._mnemo_root / ".github" / "prompts" / "mnemo.memory-pack-import.prompt.md"
        self.assertTrue(prompt_path.exists(), prompt_path)
        text = prompt_path.read_text(encoding="utf-8")
        self.assertIn("mnemo.pack_landing_list", text)
        self.assertIn("landing folder", text)
        self.assertIn(".mem", text)
        self.assertIn("mnemo.pack_inspect", text)
        self.assertIn("mnemo.pack_import", text)
        self.assertIn("mnemo.pack_review_import", text)
        self.assertIn("include_grouped_summary", text)
        self.assertIn("mnemo.pack_promote_preview", text)
        self.assertIn("mnemo.pack_promote", text)
        self.assertIn("Never auto-promote", text)
        self.assertIn("confirm_promote=true", text)
        self.assertIn("Trusted import is NOT local adoption", text)
        self.assertIn("Stop before promotion", text)

    def test_docs_present_export_as_primary_and_do_not_point_to_list_select(self) -> None:
        readme = (self._mnemo_root / "README.md").read_text(encoding="utf-8")
        tool_ref = (self._mnemo_root / "docs" / "tool_reference.md").read_text(encoding="utf-8")
        combined = f"{readme}\n{tool_ref}"
        self.assertNotIn("normal workflow: /mnemo.memory-list-select", combined)
        self.assertIn("/mnemo.memory-pack-export", combined)
        self.assertIn("/mnemo.memory-pack-import", combined)
        self.assertIn("pack_landing_list", combined)

    def test_prompt_files_include_nexus_pack_actions(self) -> None:
        prompt_names = [
            "mnemo.memory-list-select.prompt.md",
            "mnemo.memory-pack-export.prompt.md",
            "mnemo.memory-pack-import.prompt.md",
        ]
        combined = "\n".join(
            (self._mnemo_root / ".github" / "prompts" / name).read_text(encoding="utf-8")
            for name in prompt_names
        )
        for token in (
            "mnemo.memory_group_discover",
            "mnemo.memory_group_preview",
            "mnemo.pack_preview",
            "mnemo.pack_redaction_preview",
            "mnemo.pack_export",
            "mnemo.pack_landing_list",
            "mnemo.pack_inspect",
            "mnemo.pack_import",
            "mnemo.pack_review_import",
            "mnemo.pack_promote_preview",
            "mnemo.pack_promote",
        ):
            self.assertIn(token, combined)

    def test_ux_lab_smoke_script_exists(self) -> None:
        self.assertTrue(self._smoke_script.exists(), self._smoke_script)


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

    def _duplicate_mass_idf_values(self) -> list[float]:
        values: list[float] = ([6.402677] * 2297) + ([5.149914] * 1512)
        interior_values = [round(1.05 + (5.95 - 1.05) * idx / 139, 6) for idx in range(140)]
        for idx, value in enumerate(interior_values):
            repetitions = 12 + (1 if idx < 39 else 0)
            values.extend([value] * repetitions)
        return values

    def _duplicate_mass_idf_profile(self) -> dict[str, Any]:
        idf_map: dict[str, float] = {}
        for idx, value in enumerate(self._duplicate_mass_idf_values()):
            idf_map[f"corpus_term_{idx}"] = value
        idf_map.update(
            {
                "midaliasalpha": 3.9,
                "midaliasbeta": 4.2,
                "weakaliasalpha": 1.08,
                "weakaliasbeta": 1.12,
                "strongaliasomega": 6.24,
            }
        )
        return {"idf": idf_map}

    def _proposal_cluster(self, candidate_alias: str) -> dict[str, Any]:
        miss_events = [{"event_id": f"miss-{idx}", "query_text": candidate_alias} for idx in range(3)]
        return {
            "representative": candidate_alias,
            "miss_events": miss_events,
            "hints": [],
            "query_counts": {candidate_alias: len(miss_events)},
            "domain_counts": {"agentic": len(miss_events)},
        }

    def _run_alias_proposal_gate(
        self,
        candidate_alias: str,
        idf_profile: dict[str, Any],
        *,
        threshold: float | None = None,
    ) -> dict[str, Any]:
        cluster = self._proposal_cluster(candidate_alias)
        idf_selection = {"active": True, "profile": idf_profile, "scope": "project", "status": "ready"}
        patches = [
            mock.patch("server._load_recent_alias_source_events", return_value=([{"event_id": "seed"}], [])),
            mock.patch("server._cluster_miss_events", return_value=[cluster]),
            mock.patch("server._ensure_idf_profiles", return_value={"project": "ready"}),
            mock.patch("server._resolve_idf_profile_for_memory_or_query", return_value=idf_selection),
            mock.patch(
                "server._alias_candidate_memories",
                return_value=[(0.64, {"id": "mem-1", "text": "memory recall pipeline", "domain": "agentic"})],
            ),
            mock.patch("server._cluster_canonical", return_value="memory recall pipeline"),
            mock.patch("server._proposal_source_events", return_value=["miss-0", "miss-1", "miss-2"]),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            if threshold is None:
                return server._propose_aliases_maintenance({"domain": "agentic", "min_recurrence": 3}, dry_run=True)
            with mock.patch.object(server, "ALIAS_MIN_IDF_STRENGTH", threshold):
                return server._propose_aliases_maintenance({"domain": "agentic", "min_recurrence": 3}, dry_run=True)

    def _load_server_module_with_alias_threshold(self, raw_value: str):
        module_name = f"server_alias_threshold_{raw_value.replace('.', '_')}"
        os.environ["MNEMO_ALIAS_MIN_IDF_STRENGTH"] = raw_value
        spec = importlib.util.spec_from_file_location(module_name, str(Path(server.__file__).resolve()))
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

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

    def test_idf_unique_value_quantile_handles_duplicate_mass(self) -> None:
        values = self._duplicate_mass_idf_values()
        new_high = server._idf_unique_value_quantile(values, 0.75)
        new_low = server._idf_unique_value_quantile(values, 0.25)
        old_high = sorted(values)[int(round((len(values) - 1) * 0.75))]
        self.assertLess(new_high, max(values))
        self.assertGreater(new_low, min(values))
        self.assertEqual(old_high, max(values))
        self.assertLess(new_high, old_high)

    def test_alias_admission_admits_mid_idf_candidate(self) -> None:
        idf_profile = self._duplicate_mass_idf_profile()
        idf_terms, penalized_terms, idf_strength = server._alias_idf_evidence("midaliasalpha midaliasbeta", idf_profile)
        self.assertEqual(idf_terms, [])
        self.assertEqual(penalized_terms, [])
        self.assertGreater(idf_strength, server.ALIAS_MIN_IDF_STRENGTH)
        result = self._run_alias_proposal_gate("midaliasalpha midaliasbeta", idf_profile)
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["status"], "ok")
        self.assertEqual(len(result["structuredContent"]["proposals"]), 1)

    def test_alias_admission_drops_weak_candidate(self) -> None:
        idf_profile = self._duplicate_mass_idf_profile()
        idf_terms, penalized_terms, idf_strength = server._alias_idf_evidence("weakaliasalpha weakaliasbeta", idf_profile)
        self.assertEqual(idf_terms, [])
        self.assertGreaterEqual(len(penalized_terms), 2)
        self.assertLess(idf_strength, server.ALIAS_MIN_IDF_STRENGTH)
        result = self._run_alias_proposal_gate("weakaliasalpha weakaliasbeta", idf_profile)
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["status"], "no_proposals")
        self.assertEqual(result["structuredContent"]["proposals"], [])

    def test_alias_admission_degenerate_profile_returns_neutral(self) -> None:
        expected_tokens = []
        seen: set[str] = set()
        for token in server._normalize_for_signature("degenerate tokens"):
            if len(token) >= 2 and token not in seen:
                seen.add(token)
                expected_tokens.append(token)
        self.assertEqual(
            server._alias_idf_evidence("degenerate tokens", {"idf": {}}),
            ([], expected_tokens, 0.0),
        )
        self.assertEqual(
            server._alias_idf_evidence("degenerate tokens", {"idf": {"degenerate": 0.0, "tokens": 0.0}}),
            ([], expected_tokens, 0.0),
        )

    def test_alias_admission_idf_terms_remains_in_evidence(self) -> None:
        idf_profile = self._duplicate_mass_idf_profile()
        idf_terms, penalized_terms, idf_strength = server._alias_idf_evidence("strongaliasomega midaliasalpha", idf_profile)
        self.assertIn("strongaliasomega", idf_terms)
        self.assertEqual(penalized_terms, [])
        self.assertGreater(idf_strength, server.ALIAS_MIN_IDF_STRENGTH)
        result = self._run_alias_proposal_gate("strongaliasomega midaliasalpha", idf_profile)
        self.assertFalse(result["isError"], result)
        proposal = result["structuredContent"]["proposals"][0]
        self.assertIn("strongaliasomega", proposal["evidence"]["idf_terms"])

    def test_alias_proposal_pipeline_emits_previously_dropped_candidate(self) -> None:
        idf_profile = self._duplicate_mass_idf_profile()
        result = self._run_alias_proposal_gate("midaliasalpha midaliasbeta", idf_profile)
        self.assertFalse(result["isError"], result)
        proposals = result["structuredContent"]["proposals"]
        self.assertEqual(len(proposals), 1)
        self.assertGreater(float(proposals[0]["score"]), 0.0)
        self.assertEqual(proposals[0]["candidate_alias"], "midaliasalpha midaliasbeta")

    def test_alias_min_idf_strength_env_override(self) -> None:
        module_low = self._load_server_module_with_alias_threshold("0.10")
        self.assertEqual(module_low.ALIAS_MIN_IDF_STRENGTH, 0.10)
        module_high = self._load_server_module_with_alias_threshold("0.90")
        self.assertEqual(module_high.ALIAS_MIN_IDF_STRENGTH, 0.90)
        _idf_terms, _penalized_terms, idf_strength = module_high._alias_idf_evidence(
            "midaliasalpha midaliasbeta",
            self._duplicate_mass_idf_profile(),
        )
        self.assertLess(idf_strength, module_high.ALIAS_MIN_IDF_STRENGTH)
        result = self._run_alias_proposal_gate("midaliasalpha midaliasbeta", self._duplicate_mass_idf_profile(), threshold=0.90)
        self.assertEqual(result["structuredContent"]["proposals"], [])

    def test_idf_quantile_removal(self) -> None:
        self.assertFalse(hasattr(server, "_idf_quantile"))

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
