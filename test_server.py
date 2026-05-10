"""Basic unit and functional tests for Mnemo public release packaging."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402


class ScoringTests(unittest.TestCase):
    def test_tokenize_identifier(self) -> None:
        self.assertIn("authenticate", server.tokenize("authenticate_user"))
        self.assertIn("user", server.tokenize("authenticate_user"))

    def test_jaccard_empty(self) -> None:
        self.assertEqual(server.jaccard(set(), set()), 1.0)

    def test_score_memory_positive_match(self) -> None:
        memory = server.new_memory(
            memory_id="mem_test",
            kind="decision",
            text="Run validation commands before handoff.",
            source="",
            tags=["validation"],
            references=[],
            pinned=False,
        )
        self.assertGreater(server.score_memory(server.tokenize("validation handoff"), memory, now=datetime.now(timezone.utc)), 0.0)


class SalienceLoaderTests(unittest.TestCase):
    def test_loader_returns_tuple_when_unavailable(self) -> None:
        previous = os.environ.pop("AGENT_SALIENCE_HOME", None)
        try:
            module_name = "agent_salience"
            previous_module = sys.modules.pop(module_name, None)
            try:
                import salience_loader

                module, reason = salience_loader.load_optional_agent_salience()
                if module is None:
                    self.assertIsInstance(reason, str)
                    self.assertIn("agent_salience", reason)
                else:
                    self.assertTrue(hasattr(module, "signal_score"))
            finally:
                if previous_module is not None:
                    sys.modules[module_name] = previous_module
        finally:
            if previous is not None:
                os.environ["AGENT_SALIENCE_HOME"] = previous


class FunctionalMemoryTests(unittest.TestCase):
    def test_record_and_search_with_temp_memory_file(self) -> None:
        old_file_env = os.environ.get("MNEMO_FILE")
        old_query_env = os.environ.get("MNEMO_LOG_QUERIES")
        old_event_env = os.environ.get("MNEMO_LOG_EVENTS")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["MNEMO_FILE"] = str(Path(tmp) / "memory.json")
                os.environ["MNEMO_LOG_QUERIES"] = "0"
                os.environ["MNEMO_LOG_EVENTS"] = "0"
                recorded = server.record_memory(
                    {
                        "kind": "note",
                        "text": "Public release smoke memory.",
                        "tags": ["release"],
                    }
                )
                self.assertFalse(recorded["isError"])
                searched = server.search_memories({"query": "release smoke", "limit": 3})
                self.assertFalse(searched["isError"])
                self.assertGreaterEqual(len(searched["structuredContent"]["matches"]), 1)
        finally:
            if old_file_env is None:
                os.environ.pop("MNEMO_FILE", None)
            else:
                os.environ["MNEMO_FILE"] = old_file_env
            if old_query_env is None:
                os.environ.pop("MNEMO_LOG_QUERIES", None)
            else:
                os.environ["MNEMO_LOG_QUERIES"] = old_query_env
            if old_event_env is None:
                os.environ.pop("MNEMO_LOG_EVENTS", None)
            else:
                os.environ["MNEMO_LOG_EVENTS"] = old_event_env


if __name__ == "__main__":
    unittest.main()
