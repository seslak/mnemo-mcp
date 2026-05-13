#!/usr/bin/env python3
"""Mnemo: dependency-free local MCP memory server.

Transport: newline-delimited JSON-RPC on stdin/stdout.
Storage: SQLite primary store (default) with optional JSON compatibility mode.

Environment variables:
- MNEMO_STORE: sqlite|json. Defaults to sqlite.
- MNEMO_FILE: compatibility/import/export path for memory.json.
- MNEMO_SQLITE_FILE: sqlite db path. Defaults to <workspace>/state/mnemo/mnemo.sqlite.
- MNEMO_MAX_MEMORIES: total memory cap including retired entries. Defaults to 5000.
- MNEMO_LOG_QUERIES: set to 0 to disable query event logging. Defaults to 1.
- MNEMO_WORKSPACE_ROOT: root for lookup_symbol. Defaults to the parent of
  the current working directory.
- MNEMO_SYMBOL_TTL_SECONDS: symbol-index walk TTL. Defaults to 5.
- MNEMO_DECAY: set to 0 to disable time-decay scoring. Defaults to 1.
- MNEMO_LOG_EVENTS: set to 0 to disable lifecycle event logging. Defaults to 1.
- MNEMO_LOG_ARCHIVE: set to 0 to disable permanent log archives. Defaults to 1.
- MNEMO_CONSOLIDATE_THRESHOLD: near-duplicate consolidation threshold. Defaults to 0.7.
- MNEMO_MAX_SEARCH_RESULTS: server-side cap for memory_search results. Defaults to 20.
- MNEMO_MAX_RECENT_RESULTS: server-side cap for memory_recent results. Defaults to 50.
- MNEMO_MAX_FILES_SCANNED: max files scanned by lookup_symbol. Defaults to 5000.
- MNEMO_MAX_TOTAL_BYTES: max total bytes scanned by lookup_symbol. Defaults to 52428800.
- MNEMO_MAX_FILE_BYTES: max single file bytes read by lookup_symbol. Defaults to 1048576.
- MNEMO_MAX_CHARS_PER_ITEM: per-item preview cap for recall/search bundles. Defaults to 1200.
- MNEMO_MAX_TOTAL_CHARS: total preview cap for recall/search bundles. Defaults to 12000.
- AGENT_SALIENCE_HOME: optional path to local agent-salience checkout for diagnostics when not installed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from salience_loader import load_optional_agent_salience


PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "mnemo"
SERVER_TITLE = "Mnemo Project Memory"
SERVER_VERSION = "0.11.0"
DEFAULT_MEMORY_FILE = Path(__file__).with_name("memory.json")
TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]+")
CAMEL_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|[0-9]+")
TOKEN_CHARS_PER_TOKEN = 3.7
MEMORY_KINDS = (
    "decision",
    "invariant",
    "failed_approach",
    "test_result",
    "command",
    "path",
    "note",
    "interaction_log",
    "context_block",
    "hippocampus_entry",
    "agent_feedback",
)
ORDERED_KINDS = (
    "invariant",
    "decision",
    "hippocampus_entry",
    "agent_feedback",
    "context_block",
    "interaction_log",
    "failed_approach",
    "test_result",
    "command",
    "path",
    "note",
)
HALF_LIVES_DAYS: dict[str, float] = {
    "invariant": math.inf,
    "decision": 180.0,
    "command": 90.0,
    "path": 90.0,
    "failed_approach": 60.0,
    "test_result": 30.0,
    "note": 30.0,
    "interaction_log": 14.0,
    "context_block": 120.0,
    "hippocampus_entry": 365.0,
    "agent_feedback": 180.0,
}
AUTHORITY_VALUES = ("low", "medium", "high", "pinned")
RETENTION_VALUES = ("ephemeral", "compressible", "durable", "pinned")
CONFIDENCE_VALUES = ("low", "medium", "high")
PHASES = ("exploration", "implementation", "debugging", "none")
PHASE_KEYWORDS: dict[str, set[str]] = {
    "debugging": {
        "error",
        "fail",
        "failing",
        "broken",
        "bug",
        "regression",
        "crash",
        "exception",
        "trace",
        "stack",
        "traceback",
        "panic",
    },
    "implementation": {
        "implement",
        "add",
        "create",
        "build",
        "wire",
        "extend",
        "introduce",
        "refactor",
        "rename",
        "migrate",
    },
    "exploration": {"what", "where", "how", "why", "find", "show", "list", "explore", "understand", "explain"},
}
PHASE_KIND_BIAS: dict[str, dict[str, float]] = {
    "exploration": {"path": 1.5, "note": 1.3, "invariant": 1.3},
    "implementation": {"decision": 1.5, "invariant": 1.5, "failed_approach": 1.3},
    "debugging": {"failed_approach": 1.5, "test_result": 1.5, "command": 1.3},
}
LOCK_TIMEOUT_SECONDS = 5.0
QUERY_LOG_MAX_BYTES = 10 * 1024 * 1024
EVENT_LOG_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_CHARS_PER_ITEM = 1200
DEFAULT_MAX_TOTAL_CHARS = 12000
SKIP_DIRS = {
    "node_modules",
    ".git",
    "__pycache__",
    "dist",
    "build",
    ".venv",
    "venv",
    "target",
}
_SHOULD_EXIT = False
_SYMBOL_CACHE: dict[str, tuple[float, str, dict[str, Any]]] = {}
_SQLITE_BOOTSTRAPPED: set[str] = set()
_SQLITE_FTS_CANDIDATE_LIMIT = 500
SALIENCE_UNAVAILABLE_MESSAGE = (
    "Configure AGENT_SALIENCE_HOME or install agent-salience to use salience diagnostics."
)
_NULLABLE_NOTE = "Optional; omit this field instead of sending null."
SUPPORTED_SCHEMA_KEYS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "description",
}
FORBIDDEN_SCHEMA_KEYS = {
    "minimum",
    "maximum",
    "default",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "pattern",
    "anyOf",
    "oneOf",
    "allOf",
    "not",
    "const",
    "format",
    "examples",
    "nullable",
    "$ref",
    "$schema",
}


class LockTimeout(RuntimeError):
    """Raised when the memory write lock cannot be acquired in time."""


def _append_description_note(schema: dict[str, Any], note: str) -> None:
    existing = str(schema.get("description", "")).strip()
    if note in existing:
        return
    if existing:
        schema["description"] = f"{existing} {note}"
    else:
        schema["description"] = note


def _coerce_copilot_type(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    non_null = [item for item in value if item != "null"]
    if len(non_null) == 1:
        return non_null[0]
    if len(non_null) > 1:
        # Copilot client can reject multi-type arrays; fall back to string as safest generic scalar.
        return "string"
    return "string"


def make_copilot_safe_schema(schema: object) -> object:
    """Return a Copilot-safe inputSchema subset copy.

    Keeps only:
    type, properties, required, additionalProperties, items, enum, description.
    """

    def _sanitize(value: object, *, in_schema: bool, parent_key: str | None = None) -> object:
        if isinstance(value, dict):
            if not in_schema:
                return {
                    key: _sanitize(item, in_schema=True, parent_key=key)
                    for key, item in value.items()
                    if isinstance(key, str)
                }

            out: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    continue
                if key in FORBIDDEN_SCHEMA_KEYS:
                    continue
                if key not in SUPPORTED_SCHEMA_KEYS:
                    continue
                if key == "properties" and isinstance(item, dict):
                    out[key] = {
                        str(prop_name): _sanitize(prop_schema, in_schema=True, parent_key="properties")
                        for prop_name, prop_schema in item.items()
                    }
                    continue
                if key == "type":
                    out[key] = _coerce_copilot_type(item)
                    if isinstance(item, list) and "null" in item:
                        _append_description_note(out, _NULLABLE_NOTE)
                    continue
                if key == "enum" and isinstance(item, list):
                    out[key] = [entry for entry in item if entry is not None]
                    if len(out[key]) != len(item):
                        _append_description_note(out, _NULLABLE_NOTE)
                    continue
                if key == "required" and isinstance(item, list):
                    out[key] = [str(entry) for entry in item if str(entry).strip()]
                    continue
                if key == "items":
                    out[key] = _sanitize(item, in_schema=True, parent_key="items")
                    continue
                out[key] = _sanitize(item, in_schema=True, parent_key=key)
            return out
        if isinstance(value, list):
            if parent_key == "required":
                return [str(entry) for entry in value if str(entry).strip()]
            if parent_key == "enum":
                return [entry for entry in value if entry is not None]
            return [_sanitize(item, in_schema=False, parent_key=parent_key) for item in value]
        return value

    sanitized = _sanitize(schema, in_schema=True)
    return sanitized if isinstance(sanitized, dict) else {}


def _simplify_copilot_nullable_schema(schema: Any) -> Any:
    """Simplify nullable schema unions for Copilot-facing inputSchema."""
    if isinstance(schema, dict):
        out: dict[str, Any] = {key: _simplify_copilot_nullable_schema(value) for key, value in schema.items()}
        schema_type = out.get("type")
        if isinstance(schema_type, list):
            non_null = [item for item in schema_type if item != "null"]
            if non_null:
                out["type"] = non_null[0]
                _append_description_note(out, _NULLABLE_NOTE)
            elif schema_type:
                out["type"] = schema_type[0]
        enum_values = out.get("enum")
        if isinstance(enum_values, list) and any(item is None for item in enum_values):
            out["enum"] = [item for item in enum_values if item is not None]
            _append_description_note(out, _NULLABLE_NOTE)
        return out
    if isinstance(schema, list):
        return [_simplify_copilot_nullable_schema(item) for item in schema]
    return schema


def copilot_safe_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    stripped = make_copilot_safe_schema(schema)
    simplified = _simplify_copilot_nullable_schema(stripped)
    return simplified if isinstance(simplified, dict) else {}


GATEWAY_TOOL_NAME = "mnemo"


def mcp_profile() -> str:
    """Return a legacy profile value for diagnostics only.

    Since 0.11.0, Mnemo exposes a single public gateway tool regardless of
    profile. The environment variable is accepted harmlessly for older
    launch configurations, but it no longer changes tools/list output.
    """
    value = str(os.environ.get("MNEMO_MCP_PROFILE", "gateway")).strip().lower()
    return value if value else "gateway"


def exposed_tools(profile: str | None = None) -> list[dict[str, Any]]:
    del profile
    return list(TOOLS)


def copilot_safe_tools(profile: str | None = None) -> list[dict[str, Any]]:
    base_tools = exposed_tools(profile)
    tools: list[dict[str, Any]] = []
    for tool in base_tools:
        entry = dict(tool)
        raw_schema = tool.get("inputSchema", {})
        entry["inputSchema"] = copilot_safe_input_schema(raw_schema if isinstance(raw_schema, dict) else {})
        tools.append(entry)
    return tools


def store_backend() -> str:
    value = str(os.environ.get("MNEMO_STORE", "sqlite")).strip().lower()
    return value if value in {"sqlite", "json"} else "sqlite"


def workspace_root() -> Path:
    configured = os.environ.get("MNEMO_WORKSPACE_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.cwd()


def state_dir() -> Path:
    configured_memory = os.environ.get("MNEMO_FILE", "").strip()
    if configured_memory:
        return Path(configured_memory).expanduser().parent
    return workspace_root() / "state" / "mnemo"


def memory_path() -> Path:
    configured = os.environ.get("MNEMO_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return state_dir() / "memory.json"


def sqlite_path() -> Path:
    configured = os.environ.get("MNEMO_SQLITE_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return state_dir() / "mnemo.sqlite"


def max_chars_per_item() -> int:
    return positive_int_env("MNEMO_MAX_CHARS_PER_ITEM", DEFAULT_MAX_CHARS_PER_ITEM)


def max_total_chars() -> int:
    return positive_int_env("MNEMO_MAX_TOTAL_CHARS", DEFAULT_MAX_TOTAL_CHARS)


def max_memories() -> int:
    raw = os.environ.get("MNEMO_MAX_MEMORIES", "5000").strip() or "5000"
    value = int(raw)
    if value < 1:
        raise ValueError("MNEMO_MAX_MEMORIES must be at least 1")
    return value


def symbol_ttl_seconds() -> float:
    raw = os.environ.get("MNEMO_SYMBOL_TTL_SECONDS", "5").strip() or "5"
    return float(raw)


def decay_enabled() -> bool:
    return os.environ.get("MNEMO_DECAY", "1").strip() != "0"


def event_logging_enabled() -> bool:
    return os.environ.get("MNEMO_LOG_EVENTS", "1").strip() != "0"


def log_archive_enabled() -> bool:
    return os.environ.get("MNEMO_LOG_ARCHIVE", "1").strip() != "0"


def consolidate_threshold(raw: Any = None) -> float:
    value = raw
    if value is None:
        value = os.environ.get("MNEMO_CONSOLIDATE_THRESHOLD", "0.7")
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        threshold = 0.7
    return max(0.5, min(threshold, 1.0))


def positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip() or str(default)
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, value)


def max_search_results() -> int:
    return positive_int_env("MNEMO_MAX_SEARCH_RESULTS", 20)


def max_recent_results() -> int:
    return positive_int_env("MNEMO_MAX_RECENT_RESULTS", 50)


def max_files_scanned() -> int:
    return positive_int_env("MNEMO_MAX_FILES_SCANNED", 5000)


def max_total_bytes() -> int:
    return positive_int_env("MNEMO_MAX_TOTAL_BYTES", 50 * 1024 * 1024)


def max_file_bytes() -> int:
    return positive_int_env("MNEMO_MAX_FILE_BYTES", 1024 * 1024)


def estimate_tokens(text: str) -> int:
    """Rough token estimate (chars / 3.7) for budget enforcement.

    Conservative-ish for English + code mix. Replace with thrift's
    estimator if available; the agent-facing contract is unchanged.
    """
    if not text:
        return 0
    return math.ceil(len(text) / TOKEN_CHARS_PER_TOKEN)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_strict_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{field} must be a boolean")


def strip_suffix(token: str) -> str:
    current = token
    for _ in range(2):
        stripped = current
        for suffix in ("tion", "ment", "ing", "ed", "ly", "s", "e"):
            min_length = 4 if suffix == "e" else len(suffix) + 2
            if current.endswith(suffix) and len(current) > min_length:
                stripped = current[: -len(suffix)]
                break
        if stripped == current:
            break
        current = stripped
    return current


def token_variants(token: str) -> set[str]:
    out: set[str] = set()
    raw = token.lower()
    if raw:
        out.add(raw)
        out.add(strip_suffix(raw))
    for piece in re.split(r"[._/:-]+", token):
        if not piece:
            continue
        lower_piece = piece.lower()
        out.add(lower_piece)
        out.add(strip_suffix(lower_piece))
        for part in CAMEL_RE.findall(piece):
            lower = part.lower()
            out.add(lower)
            out.add(strip_suffix(lower))
    return {item for item in out if item}


def tokenize(text: str) -> set[str]:
    tokens: set[str] = set()
    for match in TOKEN_RE.finditer(text):
        tokens.update(token_variants(match.group(0)))
    return tokens


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def validate_kind(kind: str) -> str:
    kind = kind.strip().lower()
    if kind not in MEMORY_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(MEMORY_KINDS)}")
    return kind


def normalize_memory_kind(kind: Any, default: str = "note") -> str:
    value = str(kind or default).strip().lower()
    return value if value in MEMORY_KINDS else default


def normalize_tags(raw_tags: Any) -> list[str]:
    if raw_tags is None:
        return []
    if not isinstance(raw_tags, list):
        raise ValueError("tags must be an array of strings")
    return [str(tag).strip().lower() for tag in raw_tags if str(tag).strip()]


def normalize_references(raw_references: Any) -> list[str]:
    if raw_references is None:
        return []
    if not isinstance(raw_references, list):
        raise ValueError("references must be an array of strings")
    if not all(isinstance(reference, str) for reference in raw_references):
        raise ValueError("references must be an array of strings")
    return list(raw_references)


def normalize_linked_ids(raw_linked_ids: Any) -> list[str]:
    if raw_linked_ids is None:
        return []
    if not isinstance(raw_linked_ids, list):
        raise ValueError("linked_ids must be an array of strings")
    if not all(isinstance(linked_id, str) for linked_id in raw_linked_ids):
        raise ValueError("linked_ids must be an array of strings")
    deduped: list[str] = []
    seen: set[str] = set()
    for linked_id in raw_linked_ids:
        value = linked_id.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def normalize_optional_string(raw: Any) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    return value if value else None


def normalize_choice(
    raw: Any,
    field: str,
    allowed: tuple[str, ...],
    default: str | None = None,
    *,
    strict: bool = True,
) -> str | None:
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if not value:
        return default
    if value not in allowed:
        if strict:
            raise ValueError(f"{field} must be one of: {', '.join(allowed)}")
        return default
    return value


def normalize_metadata(raw_metadata: Any) -> dict[str, Any]:
    if raw_metadata is None:
        return {}
    if not isinstance(raw_metadata, dict):
        raise ValueError("metadata must be an object")
    return {str(key): value for key, value in raw_metadata.items()}


def new_memory(
    memory_id: str,
    kind: str,
    text: str,
    source: str,
    tags: list[str],
    references: list[str] | None = None,
    pinned: bool = False,
    *,
    agent_id: str | None = None,
    role: str | None = None,
    scope: str | None = None,
    domain: str | None = None,
    authority: str | None = None,
    retention: str | None = None,
    confidence: str | None = None,
    linked_ids: list[str] | None = None,
    parent_id: str | None = None,
    source_run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_references = normalize_references(references or [])
    normalized_linked = normalize_linked_ids(linked_ids if linked_ids is not None else normalized_references)
    if not normalized_references:
        normalized_references = list(normalized_linked)
    elif not normalized_linked:
        normalized_linked = list(normalized_references)
    return {
        "id": memory_id,
        "kind": kind,
        "text": text,
        "source": source,
        "tags": tags,
        "pinned": pinned,
        "references": normalized_references,
        "linked_ids": normalized_linked,
        "agent_id": agent_id,
        "role": role,
        "scope": scope,
        "domain": domain,
        "authority": authority,
        "retention": retention,
        "confidence": confidence,
        "parent_id": parent_id,
        "source_run_id": source_run_id,
        "metadata": metadata or {},
        "created_at": now_iso(),
        "updated_at": None,
        "deleted_at": None,
        "deletion_reason": None,
        "superseded_by": None,
    }


def merge_link_fields(memory: dict[str, Any]) -> None:
    references = normalize_references(memory.get("references", []))
    linked_ids = normalize_linked_ids(memory.get("linked_ids", references))
    merged: list[str] = []
    seen: set[str] = set()
    for value in list(references) + list(linked_ids):
        if value in seen:
            continue
        seen.add(value)
        merged.append(value)
    memory["references"] = merged
    memory["linked_ids"] = list(merged)


def apply_structured_fields(memory: dict[str, Any], args: dict[str, Any]) -> None:
    memory["agent_id"] = normalize_optional_string(args.get("agent_id"))
    memory["role"] = normalize_optional_string(args.get("role"))
    memory["scope"] = normalize_optional_string(args.get("scope"))
    memory["domain"] = normalize_optional_string(args.get("domain"))
    memory["authority"] = normalize_choice(
        args.get("authority"),
        "authority",
        AUTHORITY_VALUES,
        default=memory.get("authority"),
    )
    memory["retention"] = normalize_choice(
        args.get("retention"),
        "retention",
        RETENTION_VALUES,
        default=memory.get("retention"),
    )
    memory["confidence"] = normalize_choice(
        args.get("confidence"),
        "confidence",
        CONFIDENCE_VALUES,
        default=memory.get("confidence"),
    )
    memory["parent_id"] = normalize_optional_string(args.get("parent_id"))
    memory["source_run_id"] = normalize_optional_string(args.get("source_run_id"))
    memory["metadata"] = normalize_metadata(args.get("metadata"))


def migrate_memory(memory: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(memory)
    text = str(migrated.get("text", ""))
    migrated["id"] = str(migrated.get("id") or make_id(text))
    migrated["kind"] = normalize_memory_kind(migrated.get("kind"), "note")
    migrated["text"] = text
    migrated["source"] = str(migrated.get("source", ""))
    tags = migrated.get("tags", [])
    migrated["tags"] = tags if isinstance(tags, list) else []
    migrated["pinned"] = bool(migrated.get("pinned", False))
    references = migrated.get("references", [])
    linked_ids = migrated.get("linked_ids", references)
    migrated["references"] = (
        [reference for reference in references if isinstance(reference, str)] if isinstance(references, list) else []
    )
    migrated["linked_ids"] = (
        [linked_id for linked_id in linked_ids if isinstance(linked_id, str)] if isinstance(linked_ids, list) else []
    )
    migrated["agent_id"] = normalize_optional_string(migrated.get("agent_id"))
    migrated["role"] = normalize_optional_string(migrated.get("role"))
    migrated["scope"] = normalize_optional_string(migrated.get("scope"))
    migrated["domain"] = normalize_optional_string(migrated.get("domain"))
    migrated["authority"] = normalize_choice(
        migrated.get("authority"),
        "authority",
        AUTHORITY_VALUES,
        default=None,
        strict=False,
    )
    migrated["retention"] = normalize_choice(
        migrated.get("retention"),
        "retention",
        RETENTION_VALUES,
        default=None,
        strict=False,
    )
    migrated["confidence"] = normalize_choice(
        migrated.get("confidence"),
        "confidence",
        CONFIDENCE_VALUES,
        default=None,
        strict=False,
    )
    migrated["parent_id"] = normalize_optional_string(migrated.get("parent_id"))
    migrated["source_run_id"] = normalize_optional_string(migrated.get("source_run_id"))
    migrated["metadata"] = normalize_metadata(migrated.get("metadata"))
    migrated["created_at"] = str(migrated.get("created_at") or now_iso())
    migrated.setdefault("updated_at", None)
    migrated.setdefault("deleted_at", None)
    migrated.setdefault("deletion_reason", None)
    migrated.setdefault("superseded_by", None)
    merge_link_fields(migrated)
    return migrated


def memory_content_hash(memory: dict[str, Any]) -> str:
    metadata = normalize_metadata(memory.get("metadata"))
    title = normalize_optional_string(metadata.get("title")) or ""
    normalized = {
        "kind": normalize_memory_kind(memory.get("kind"), "note"),
        "text": normalize_text(str(memory.get("text", ""))),
        "title": normalize_text(title),
        "agent_id": normalize_optional_string(memory.get("agent_id")) or "",
        "role": normalize_optional_string(memory.get("role")) or "",
        "domain": normalize_optional_string(memory.get("domain")) or "",
        "scope": normalize_optional_string(memory.get("scope")) or "",
    }
    return hashlib.sha1(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _sqlite_connect() -> sqlite3.Connection:
    path = sqlite_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def _sqlite_session() -> Any:
    conn = _sqlite_connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _sqlite_set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _sqlite_get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    value = row[0]
    return str(value) if value is not None else None


def _sqlite_fts_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(id UNINDEXED, text, title, tags)"
        )
        return True
    except sqlite3.OperationalError:
        return False


def _sqlite_has_fts_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
    ).fetchone()
    return row is not None


def _sqlite_tags_text(tags_json: Any) -> str:
    if tags_json is None:
        return ""
    if isinstance(tags_json, str):
        try:
            parsed = json.loads(tags_json)
        except Exception:
            parsed = None
    else:
        parsed = tags_json
    if not isinstance(parsed, list):
        return ""
    return " ".join(str(item).strip() for item in parsed if str(item).strip())


def _sqlite_sync_fts_for_memory_row(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    if not _sqlite_has_fts_table(conn) and not _sqlite_fts_available(conn):
        return
    conn.execute(
        "INSERT OR REPLACE INTO memories_fts(id, text, title, tags) VALUES(?, ?, ?, ?)",
        (
            str(row.get("id", "")),
            str(row.get("text", "")),
            str(row.get("title") or ""),
            _sqlite_tags_text(row.get("tags_json")),
        ),
    )


def _sqlite_rebuild_fts_index(conn: sqlite3.Connection) -> bool:
    if not _sqlite_has_fts_table(conn) and not _sqlite_fts_available(conn):
        return False
    conn.execute("DELETE FROM memories_fts")
    rows = conn.execute("SELECT id, text, title, tags_json FROM memories").fetchall()
    for row in rows:
        conn.execute(
            "INSERT OR REPLACE INTO memories_fts(id, text, title, tags) VALUES(?, ?, ?, ?)",
            (
                str(row["id"] or ""),
                str(row["text"] or ""),
                str(row["title"] or ""),
                _sqlite_tags_text(row["tags_json"]),
            ),
        )
    _sqlite_set_meta(conn, "fts_index_built_at", now_iso())
    return True


def _sqlite_ensure_schema(conn: sqlite3.Connection) -> None:
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
            metadata_json TEXT,
            pinned INTEGER DEFAULT 0,
            deleted INTEGER DEFAULT 0,
            superseded_by TEXT,
            created_at TEXT,
            updated_at TEXT,
            token_estimate INTEGER,
            content_hash TEXT
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind)",
        "CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_memories_updated_at ON memories(updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_memories_domain ON memories(domain)",
        "CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope)",
        "CREATE INDEX IF NOT EXISTS idx_memories_agent_id ON memories(agent_id)",
        "CREATE INDEX IF NOT EXISTS idx_memories_role ON memories(role)",
        "CREATE INDEX IF NOT EXISTS idx_memories_authority ON memories(authority)",
        "CREATE INDEX IF NOT EXISTS idx_memories_retention ON memories(retention)",
        "CREATE INDEX IF NOT EXISTS idx_memories_source_run_id ON memories(source_run_id)",
        "CREATE INDEX IF NOT EXISTS idx_memories_deleted ON memories(deleted)",
        "CREATE INDEX IF NOT EXISTS idx_memories_content_hash ON memories(content_hash)",
        "CREATE INDEX IF NOT EXISTS idx_links_source_id ON links(source_id)",
        "CREATE INDEX IF NOT EXISTS idx_links_target_id ON links(target_id)",
        "CREATE INDEX IF NOT EXISTS idx_events_memory_id ON events(memory_id)",
        "CREATE INDEX IF NOT EXISTS idx_events_event_type ON events(event_type)",
        "CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at)",
    ):
        conn.execute(statement)
    _sqlite_set_meta(conn, "schema_version", "1")
    if _sqlite_get_meta(conn, "created_at") is None:
        _sqlite_set_meta(conn, "created_at", now_iso())


def _memory_to_sqlite_row(memory: dict[str, Any]) -> dict[str, Any]:
    migrated = migrate_memory(memory)
    metadata = normalize_metadata(migrated.get("metadata"))
    title = normalize_optional_string(metadata.get("title"))
    text_value = str(migrated.get("text", ""))
    return {
        "id": str(migrated.get("id", "")),
        "kind": str(migrated.get("kind", "note")),
        "text": text_value,
        "title": title,
        "preview": memory_preview(migrated, max_chars=240),
        "source": str(migrated.get("source", "")),
        "tags_json": json.dumps(normalize_tags(migrated.get("tags", [])), ensure_ascii=False),
        "linked_ids_json": json.dumps(
            normalize_linked_ids(migrated.get("linked_ids", migrated.get("references", []))),
            ensure_ascii=False,
        ),
        "agent_id": normalize_optional_string(migrated.get("agent_id")),
        "role": normalize_optional_string(migrated.get("role")),
        "scope": normalize_optional_string(migrated.get("scope")),
        "domain": normalize_optional_string(migrated.get("domain")),
        "authority": normalize_choice(
            migrated.get("authority"),
            "authority",
            AUTHORITY_VALUES,
            default=None,
            strict=False,
        ),
        "retention": normalize_choice(
            migrated.get("retention"),
            "retention",
            RETENTION_VALUES,
            default=None,
            strict=False,
        ),
        "confidence": normalize_choice(
            migrated.get("confidence"),
            "confidence",
            CONFIDENCE_VALUES,
            default=None,
            strict=False,
        ),
        "parent_id": normalize_optional_string(migrated.get("parent_id")),
        "source_run_id": normalize_optional_string(migrated.get("source_run_id")),
        "metadata_json": json.dumps(metadata, ensure_ascii=False),
        "pinned": 1 if bool(migrated.get("pinned")) else 0,
        "deleted": 1 if bool(migrated.get("deleted_at")) else 0,
        "superseded_by": normalize_optional_string(migrated.get("superseded_by")),
        "created_at": str(migrated.get("created_at") or now_iso()),
        "updated_at": normalize_optional_string(migrated.get("updated_at")),
        "token_estimate": int(estimate_tokens(text_value)),
        "content_hash": memory_content_hash(migrated),
        "deletion_reason": normalize_optional_string(migrated.get("deletion_reason")),
    }


def _sqlite_row_to_memory(row: sqlite3.Row) -> dict[str, Any]:
    tags = json.loads(str(row["tags_json"] or "[]"))
    linked_ids = json.loads(str(row["linked_ids_json"] or "[]"))
    metadata = json.loads(str(row["metadata_json"] or "{}"))
    deleted_at = None
    deletion_reason = None
    if int(row["deleted"] or 0):
        deletion_reason = normalize_optional_string(metadata.get("_deletion_reason"))
        deleted_at = normalize_optional_string(metadata.get("_deleted_at")) or normalize_optional_string(row["updated_at"]) or now_iso()
    memory = {
        "id": str(row["id"]),
        "kind": str(row["kind"]),
        "text": str(row["text"]),
        "source": str(row["source"] or ""),
        "tags": tags if isinstance(tags, list) else [],
        "pinned": bool(int(row["pinned"] or 0)),
        "references": linked_ids if isinstance(linked_ids, list) else [],
        "linked_ids": linked_ids if isinstance(linked_ids, list) else [],
        "agent_id": row["agent_id"],
        "role": row["role"],
        "scope": row["scope"],
        "domain": row["domain"],
        "authority": row["authority"],
        "retention": row["retention"],
        "confidence": row["confidence"],
        "parent_id": row["parent_id"],
        "source_run_id": row["source_run_id"],
        "metadata": metadata if isinstance(metadata, dict) else {},
        "created_at": str(row["created_at"] or now_iso()),
        "updated_at": row["updated_at"],
        "deleted_at": deleted_at,
        "deletion_reason": deletion_reason,
        "superseded_by": row["superseded_by"],
    }
    return migrate_memory(memory)


def _sqlite_insert_event(
    conn: sqlite3.Connection,
    memory_id: str | None,
    event_type: str,
    data: dict[str, Any],
    created_at: str | None = None,
    event_id: str | None = None,
) -> None:
    created = created_at or now_iso()
    payload = data if isinstance(data, dict) else {"value": data}
    if not event_id:
        digest = hashlib.sha1(
            f"{created}:{event_type}:{memory_id or ''}:{json.dumps(payload, sort_keys=True, ensure_ascii=False)}".encode(
                "utf-8"
            )
        ).hexdigest()[:16]
        event_id = f"evt_{digest}"
    conn.execute(
        "INSERT OR IGNORE INTO events(id, memory_id, event_type, data_json, created_at) VALUES(?, ?, ?, ?, ?)",
        (
            event_id,
            memory_id or None,
            event_type,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            created,
        ),
    )


def _sqlite_sync_links_for_memory(conn: sqlite3.Connection, memory: dict[str, Any]) -> None:
    source_id = str(memory.get("id", "")).strip()
    if not source_id:
        return
    linked_ids = normalize_linked_ids(memory.get("linked_ids", memory.get("references", [])))
    metadata = normalize_metadata(memory.get("metadata"))
    relations_raw = metadata.get("link_relations", {})
    relations = relations_raw if isinstance(relations_raw, dict) else {}
    conn.execute("DELETE FROM links WHERE source_id = ?", (source_id,))
    for target_id in linked_ids:
        relation = normalize_optional_string(relations.get(target_id)) or ""
        conn.execute(
            "INSERT OR REPLACE INTO links(source_id, target_id, relation, created_at) VALUES(?, ?, ?, ?)",
            (source_id, target_id, relation, now_iso()),
        )


def _sqlite_upsert_memory(conn: sqlite3.Connection, memory: dict[str, Any]) -> None:
    row = _memory_to_sqlite_row(memory)
    metadata = normalize_metadata(memory.get("metadata"))
    if row["deleted"]:
        metadata["_deleted_at"] = memory.get("deleted_at")
        metadata["_deletion_reason"] = memory.get("deletion_reason")
        row["metadata_json"] = json.dumps(metadata, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO memories(
            id, kind, text, title, preview, source, tags_json, linked_ids_json,
            agent_id, role, scope, domain, authority, retention, confidence,
            parent_id, source_run_id, metadata_json, pinned, deleted, superseded_by,
            created_at, updated_at, token_estimate, content_hash
        ) VALUES(
            :id, :kind, :text, :title, :preview, :source, :tags_json, :linked_ids_json,
            :agent_id, :role, :scope, :domain, :authority, :retention, :confidence,
            :parent_id, :source_run_id, :metadata_json, :pinned, :deleted, :superseded_by,
            :created_at, :updated_at, :token_estimate, :content_hash
        )
        ON CONFLICT(id) DO UPDATE SET
            kind=excluded.kind,
            text=excluded.text,
            title=excluded.title,
            preview=excluded.preview,
            source=excluded.source,
            tags_json=excluded.tags_json,
            linked_ids_json=excluded.linked_ids_json,
            agent_id=excluded.agent_id,
            role=excluded.role,
            scope=excluded.scope,
            domain=excluded.domain,
            authority=excluded.authority,
            retention=excluded.retention,
            confidence=excluded.confidence,
            parent_id=excluded.parent_id,
            source_run_id=excluded.source_run_id,
            metadata_json=excluded.metadata_json,
            pinned=excluded.pinned,
            deleted=excluded.deleted,
            superseded_by=excluded.superseded_by,
            created_at=excluded.created_at,
            updated_at=excluded.updated_at,
            token_estimate=excluded.token_estimate,
            content_hash=excluded.content_hash
        """,
        row,
    )
    _sqlite_sync_links_for_memory(conn, memory)
    _sqlite_sync_fts_for_memory_row(conn, row)


def _sqlite_load_store(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT * FROM memories ORDER BY created_at ASC, id ASC").fetchall()
    memories = [_sqlite_row_to_memory(row) for row in rows]
    return {"version": 1, "memories": memories}


def _sqlite_import_memory_rows(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> tuple[int, int, list[str]]:
    imported = 0
    skipped = 0
    errors: list[str] = []
    existing_ids = {str(row[0]) for row in conn.execute("SELECT id FROM memories").fetchall()}
    existing_hashes = {str(row[0]) for row in conn.execute("SELECT content_hash FROM memories WHERE content_hash IS NOT NULL").fetchall()}
    for row in rows:
        try:
            memory = migrate_memory(row)
            memory_id = str(memory.get("id", "")).strip()
            digest = memory_content_hash(memory)
            if not memory_id:
                skipped += 1
                continue
            if memory_id in existing_ids or digest in existing_hashes:
                skipped += 1
                continue
            imported += 1
            if dry_run:
                continue
            _sqlite_upsert_memory(conn, memory)
            existing_ids.add(memory_id)
            existing_hashes.add(digest)
        except Exception as exc:
            errors.append(str(exc))
    return imported, skipped, errors


def _read_json_memories(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return []
    if isinstance(payload, dict):
        memories = payload.get("memories", [])
        if isinstance(memories, list):
            return [memory for memory in memories if isinstance(memory, dict)]
    if isinstance(payload, list):
        return [memory for memory in payload if isinstance(memory, dict)]
    return []


def _read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        return []
    return rows


def _sqlite_ingest_legacy_events_and_queries(conn: sqlite3.Connection) -> None:
    legacy_event_paths = [
        (events_log_path().with_name("events.1.jsonl"), False),
        (events_log_path(), False),
        (events_archive_path(), True),
    ]
    for path, is_archive in legacy_event_paths:
        for row in _read_jsonl_rows(path):
            event_type = str(row.get("event", "")).strip() or "event"
            created_at = str(row.get("ts", "")).strip() or now_iso()
            memory_id = normalize_optional_string(row.get("id"))
            details = row.get("details")
            data = details if isinstance(details, dict) else {"details": details}
            data["_legacy_archive"] = bool(is_archive)
            _sqlite_insert_event(conn, memory_id, event_type, data, created_at)

    legacy_query_paths = [
        (query_log_path().with_name("queries.1.jsonl"), False),
        (query_log_path(), False),
        (query_archive_path(), True),
    ]
    for path, is_archive in legacy_query_paths:
        for row in _read_jsonl_rows(path):
            created_at = str(row.get("ts", "")).strip() or now_iso()
            payload = dict(row)
            payload["_legacy_archive"] = bool(is_archive)
            _sqlite_insert_event(conn, None, "query", payload, created_at)


def _sqlite_bootstrap_if_needed(conn: sqlite3.Connection) -> None:
    db_key = str(sqlite_path().resolve())
    if db_key in _SQLITE_BOOTSTRAPPED:
        return
    count_row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
    memory_count = int(count_row[0]) if count_row else 0
    if memory_count == 0:
        mem_path = memory_path()
        source_rows = _read_json_memories(mem_path)
        if not source_rows:
            source_rows = _read_json_memories(mem_path.with_name("memory.example.json"))
        archive_rows = _read_jsonl_rows(archived_path())
        merged_rows: list[dict[str, Any]] = list(source_rows)
        for row in archive_rows:
            if isinstance(row, dict):
                metadata = normalize_metadata(row.get("metadata"))
                metadata["legacy_archive"] = True
                row["metadata"] = metadata
                merged_rows.append(row)
        _sqlite_import_memory_rows(conn, merged_rows, dry_run=False)
        _sqlite_ingest_legacy_events_and_queries(conn)
        _sqlite_set_meta(conn, "last_import_at", now_iso())
    _sqlite_set_meta(conn, "legacy_bootstrap_done", now_iso())
    fts_available = _sqlite_fts_available(conn)
    _sqlite_set_meta(conn, "fts_available", "1" if fts_available else "0")
    if fts_available and _sqlite_get_meta(conn, "fts_index_built_at") is None:
        _sqlite_rebuild_fts_index(conn)
    _SQLITE_BOOTSTRAPPED.add(db_key)


def _json_load_store() -> dict[str, Any]:
    path = memory_path()
    if not path.exists():
        bootstrap_store_from_example(path)
    if not path.exists():
        return {"version": 1, "memories": []}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {"version": 1, "memories": []}
    data.setdefault("version", 1)
    raw_memories = data.get("memories", [])
    if not isinstance(raw_memories, list):
        raw_memories = []
    data["memories"] = [migrate_memory(m) for m in raw_memories if isinstance(m, dict)]
    return data


def load_store() -> dict[str, Any]:
    if store_backend() == "json":
        return _json_load_store()
    with _sqlite_session() as conn:
        _sqlite_ensure_schema(conn)
        _sqlite_bootstrap_if_needed(conn)
        return _sqlite_load_store(conn)


def bootstrap_store_from_example(path: Path) -> None:
    example = path.with_name("memory.example.json")
    if path.exists() or not example.exists():
        return
    with MemoryFileLock(path):
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with example.open("r", encoding="utf-8") as src, tmp.open("w", encoding="utf-8") as dst:
                dst.write(src.read())
            if path.exists():
                return
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()


def save_store(data: dict[str, Any]) -> None:
    data["version"] = 1
    data["memories"] = [migrate_memory(m) for m in data.get("memories", []) if isinstance(m, dict)]
    if store_backend() == "json":
        path = memory_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
        return

    with _sqlite_session() as conn:
        _sqlite_ensure_schema(conn)
        _sqlite_bootstrap_if_needed(conn)
        current_ids = {str(row[0]) for row in conn.execute("SELECT id FROM memories").fetchall()}
        next_memories = [migrate_memory(m) for m in data.get("memories", []) if isinstance(m, dict)]
        next_ids = {str(memory.get("id")) for memory in next_memories}
        delete_ids = sorted(current_ids - next_ids)
        for memory_id in delete_ids:
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            conn.execute("DELETE FROM links WHERE source_id = ? OR target_id = ?", (memory_id, memory_id))
            if _sqlite_has_fts_table(conn):
                conn.execute("DELETE FROM memories_fts WHERE id = ?", (memory_id,))
        for memory in next_memories:
            _sqlite_upsert_memory(conn, memory)


def store_lock_path() -> Path:
    return sqlite_path() if store_backend() == "sqlite" else memory_path()


def make_id(text: str) -> str:
    digest = hashlib.sha1(f"{time.time_ns()}:{text}".encode("utf-8")).hexdigest()[:10]
    return f"mem_{int(time.time())}_{digest}"


class MemoryFileLock:
    """Advisory write lock for the memory store.

    The lock is a sidecar file next to memory.json. A byte range is locked
    with msvcrt.locking on Windows and fcntl.flock on POSIX. The sidecar keeps
    the actual memory file replaceable, so saves can remain atomic while reads
    stay lock-free. Writers wait up to five seconds with backoff.
    """

    def __init__(self, target: Path) -> None:
        self._lock_path = target.with_suffix(target.suffix + ".lock")
        self._handle: Any = None

    def __enter__(self) -> MemoryFileLock:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._lock_path.open("a+b")
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        delay = 0.025
        while True:
            try:
                self._acquire_nonblocking()
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise LockTimeout("memory file is busy; could not acquire write lock")
                time.sleep(delay)
                delay = min(delay * 1.7, 0.25)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._handle is None:
            return
        try:
            self._release()
        finally:
            self._handle.close()
            self._handle = None

    def _acquire_nonblocking(self) -> None:
        assert self._handle is not None
        if os.name == "nt":
            import msvcrt

            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release(self) -> None:
        assert self._handle is not None
        if os.name == "nt":
            import msvcrt

            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)


def text_result(text: str, structured: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "isError": False,
    }
    if structured is not None:
        result["structuredContent"] = structured
    return result


def tool_error(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "isError": True,
    }


def salience_unavailable_result(reason: str | None = None) -> dict[str, Any]:
    structured = {
        "error": "agent_salience_unavailable",
        "message": SALIENCE_UNAVAILABLE_MESSAGE,
    }
    if reason:
        structured["reason"] = reason
    return {
        "content": [{"type": "text", "text": f"Error: {SALIENCE_UNAVAILABLE_MESSAGE}"}],
        "isError": True,
        "structuredContent": structured,
    }


def salience_breakdown_payload(breakdown: Any) -> dict[str, Any]:
    if hasattr(breakdown, "to_dict"):
        payload = breakdown.to_dict()
        if isinstance(payload, dict):
            return payload
    return {
        "cosine": float(getattr(breakdown, "cosine", 0.0)),
        "jaccard": float(getattr(breakdown, "jaccard", 0.0)),
        "repetition": float(getattr(breakdown, "repetition", 0.0)),
        "recency": float(getattr(breakdown, "recency", 0.0)),
        "novelty": float(getattr(breakdown, "novelty", 0.0)),
        "drift": float(getattr(breakdown, "drift", 0.0)),
        "final": float(getattr(breakdown, "final", 0.0)),
        "weights": dict(getattr(breakdown, "weights", {})),
    }


def is_superseded(memory: dict[str, Any]) -> bool:
    return bool(memory.get("superseded_by"))


def is_legacy_supersede_retirement(memory: dict[str, Any]) -> bool:
    if not memory.get("deleted_at") or not memory.get("superseded_by"):
        return False
    reason = str(memory.get("deletion_reason") or "").strip().lower()
    return reason == "" or reason.startswith("superseded by ") or reason.startswith("consolidated into ")


def is_deleted(memory: dict[str, Any]) -> bool:
    return bool(memory.get("deleted_at")) and not is_legacy_supersede_retirement(memory)


def is_active(memory: dict[str, Any]) -> bool:
    return not is_deleted(memory) and not is_superseded(memory)


def visible_memory(memory: dict[str, Any], include_deleted: bool, include_superseded: bool) -> bool:
    if is_deleted(memory) and not include_deleted:
        return False
    if is_superseded(memory) and not include_superseded:
        return False
    return True


def active_count(store: dict[str, Any]) -> int:
    return sum(1 for memory in store.get("memories", []) if is_active(memory))


def total_count(store: dict[str, Any]) -> int:
    return len(store.get("memories", []))


def find_memory(store: dict[str, Any], memory_id: str) -> dict[str, Any] | None:
    for memory in store.get("memories", []):
        if memory.get("id") == memory_id:
            return memory
    return None


def decay_multiplier(memory: dict[str, Any], now: datetime | None = None) -> float:
    if not decay_enabled() or memory.get("pinned"):
        return 1.0
    kind = str(memory.get("kind", "note"))
    half_life = HALF_LIVES_DAYS.get(kind, HALF_LIVES_DAYS["note"])
    if math.isinf(half_life):
        return 1.0
    timestamp = memory.get("updated_at") or memory.get("created_at")
    if not timestamp:
        return 1.0
    try:
        relevant = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return 1.0
    if relevant.tzinfo is None:
        relevant = relevant.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (current.astimezone(timezone.utc) - relevant).total_seconds() / 86400)
    return 0.5 ** (age_days / half_life)


def infer_phase(query: str) -> str | None:
    tokens = {match.group(0).lower() for match in TOKEN_RE.finditer(query)}
    scores = {phase: len(tokens & keywords) for phase, keywords in PHASE_KEYWORDS.items()}
    if not any(scores.values()):
        return None
    for phase in ("debugging", "implementation", "exploration"):
        if scores[phase] == max(scores.values()):
            return phase
    return None


def resolve_phase(args: dict[str, Any], query: str) -> tuple[str | None, str | None]:
    if "phase" in args and args.get("phase") is not None:
        phase = str(args.get("phase", "")).strip().lower()
        if phase not in PHASES:
            raise ValueError(f"phase must be one of: {', '.join(PHASES)}")
        return phase, None if phase == "none" else phase
    phase = infer_phase(query)
    return phase, phase


def score_memory(
    query_tokens: set[str],
    memory: dict[str, Any],
    phase: str | None = None,
    now: datetime | None = None,
) -> float:
    """Return lexical relevance for a memory.

    Tokens come from TOKEN_RE plus identifier splits for camelCase,
    snake_case, dotted paths, slash paths, and colon or dash separated names.
    A tiny suffix stripper emits rough variants for common endings. Scoring is:

    weighted_overlap(text*1.0 + tags*1.5 + source*0.7 + kind*0.3)
    divided by sqrt(total_unique_memory_tokens). If any query token exactly
    equals a tag, add a flat 0.5 bonus. Pinned memories add a flat 0.3 bonus.
    The result is multiplied by time decay and optional phase kind bias.
    Recency is used only by callers as a tiebreak when scores are equal.
    """

    if not query_tokens:
        return 0.0

    field_tokens = {
        "text": tokenize(str(memory.get("text", ""))),
        "tags": tokenize(" ".join(str(tag) for tag in memory.get("tags", []))),
        "source": tokenize(str(memory.get("source", ""))),
        "kind": tokenize(str(memory.get("kind", ""))),
    }
    weights = {"text": 1.0, "tags": 1.5, "source": 0.7, "kind": 0.3}
    memory_tokens: set[str] = set()
    weighted_overlap = 0.0
    for field, tokens in field_tokens.items():
        memory_tokens.update(tokens)
        weighted_overlap += weights[field] * len(query_tokens & tokens)
    if not memory_tokens or weighted_overlap <= 0:
        return 0.0

    score = weighted_overlap / math.sqrt(len(memory_tokens))
    exact_tags = {str(tag).strip().lower() for tag in memory.get("tags", [])}
    if query_tokens & exact_tags:
        score += 0.5
    if memory.get("pinned"):
        score += 0.3
    score *= decay_multiplier(memory, now)
    if phase:
        score *= PHASE_KIND_BIAS.get(phase, {}).get(str(memory.get("kind", "")), 1.0)
    return score


def archived_path() -> Path:
    return memory_path().with_name("memory.archive.jsonl")


def archive_retired_entries(store: dict[str, Any]) -> int:
    memories = store.get("memories", [])
    retired = [
        (index, memory)
        for index, memory in enumerate(memories)
        if (is_deleted(memory) or is_superseded(memory)) and not memory.get("pinned")
    ]
    if not retired:
        return 0
    retired.sort(key=lambda item: str(item[1].get("created_at", "")))
    count = max(1, math.ceil(len(retired) * 0.10))
    selected = retired[:count]
    path = archived_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    archived_at = now_iso()
    with path.open("a", encoding="utf-8") as f:
        for _, memory in selected:
            row = dict(memory)
            row["archived_at"] = archived_at
            f.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")
            append_event_log("archive", str(memory.get("id", "")), {"archived_to": path.name})
    for index, _ in sorted(selected, key=lambda item: item[0], reverse=True):
        del memories[index]
    return len(selected)


def enforce_size_cap(store: dict[str, Any]) -> tuple[bool, str | None]:
    cap = max_memories()
    total = total_count(store)
    projected = total + 1
    if projected <= cap:
        return True, None
    archived = archive_retired_entries(store)
    if archived:
        save_store(store)
    total = total_count(store)
    projected = total + 1
    if projected > cap:
        return (
            False,
            f"memory cap {cap} reached; archive or supersede before writing",
        )
    return True, None


def duplicate_candidates(memories: list[dict[str, Any]], kind: str, skip_id: str | None) -> list[dict[str, Any]]:
    same_kind = [
        memory
        for memory in memories
        if memory.get("kind") == kind
        and memory.get("id") != skip_id
        and is_active(memory)
    ]
    return same_kind[-200:]


def memory_preview(memory: dict[str, Any], max_chars: int = 200) -> str:
    text = str(memory.get("text", ""))
    return text if len(text) <= max_chars else text[:max_chars]


def get_linked_memories(
    store: dict[str, Any],
    memory: dict[str, Any],
    include_deleted: bool = False,
    include_superseded: bool = False,
) -> list[dict[str, Any]]:
    linked_ids = normalize_linked_ids(memory.get("linked_ids", memory.get("references", [])))
    if not linked_ids:
        return []
    out: list[dict[str, Any]] = []
    for linked_id in linked_ids:
        linked = find_memory(store, linked_id)
        if linked is None:
            continue
        if not visible_memory(linked, include_deleted, include_superseded):
            continue
        out.append(linked)
    return out


def filter_memories(
    memories: list[dict[str, Any]],
    filters: dict[str, Any],
    *,
    include_deleted: bool = False,
    include_superseded: bool = False,
) -> list[dict[str, Any]]:
    kind_filter = normalize_optional_string(filters.get("kind"))
    role_filter = normalize_optional_string(filters.get("role"))
    agent_id_filter = normalize_optional_string(filters.get("agent_id"))
    domain_filter = normalize_optional_string(filters.get("domain"))
    scope_filter = normalize_optional_string(filters.get("scope"))
    authority_filter = normalize_choice(
        filters.get("authority"),
        "authority",
        AUTHORITY_VALUES,
        default=None,
        strict=False,
    )
    retention_filter = normalize_choice(
        filters.get("retention"),
        "retention",
        RETENTION_VALUES,
        default=None,
        strict=False,
    )
    source_run_id_filter = normalize_optional_string(filters.get("source_run_id"))
    pinned_filter = parse_bool(filters.get("pinned"), default=False) if "pinned" in filters else None

    out: list[dict[str, Any]] = []
    for memory in memories:
        if not visible_memory(memory, include_deleted, include_superseded):
            continue
        if kind_filter and str(memory.get("kind", "")) != kind_filter:
            continue
        if role_filter and str(memory.get("role", "")).strip() != role_filter:
            continue
        if agent_id_filter and str(memory.get("agent_id", "")).strip() != agent_id_filter:
            continue
        if domain_filter and str(memory.get("domain", "")).strip() != domain_filter:
            continue
        if scope_filter and str(memory.get("scope", "")).strip() != scope_filter:
            continue
        if authority_filter and str(memory.get("authority", "")).strip().lower() != authority_filter:
            continue
        if retention_filter and str(memory.get("retention", "")).strip().lower() != retention_filter:
            continue
        if source_run_id_filter and str(memory.get("source_run_id", "")).strip() != source_run_id_filter:
            continue
        if pinned_filter is not None and bool(memory.get("pinned")) != pinned_filter:
            continue
        out.append(memory)
    return out


def rank_memories_for_query(
    memories: list[dict[str, Any]],
    query_tokens: set[str],
    *,
    phase: str | None = None,
    query_text: str = "",
) -> list[tuple[float, dict[str, Any]]]:
    ranked: list[tuple[float, dict[str, Any]]] = []
    for memory in memories:
        score = score_memory(query_tokens, memory, phase)
        if score > 0 or not query_text:
            ranked.append((score, memory))
    ranked.sort(key=lambda item: (item[0], str(item[1].get("created_at", ""))), reverse=True)
    return ranked


def _sqlite_fts_match_expression(query_tokens: set[str]) -> str:
    tokens = sorted(token for token in query_tokens if token)
    if not tokens:
        return ""
    limited = tokens[:24]
    quoted = [f"\"{token.replace('\"', '\"\"')}\"" for token in limited]
    return " OR ".join(quoted)


def _sqlite_fts_candidate_memories(
    args: dict[str, Any],
    query: str,
    *,
    include_deleted: bool,
    include_superseded: bool,
    limit: int,
) -> list[dict[str, Any]]:
    query_tokens = tokenize(query)
    match_expression = _sqlite_fts_match_expression(query_tokens)
    if not match_expression:
        return []

    clauses = ["memories_fts MATCH ?"]
    params: list[Any] = [match_expression]
    if not include_deleted:
        clauses.append("m.deleted = 0")
    if not include_superseded:
        clauses.append("(m.superseded_by IS NULL OR m.superseded_by = '')")

    for field in ("kind", "role", "agent_id", "domain", "scope", "source_run_id"):
        value = normalize_optional_string(args.get(field))
        if value is not None:
            clauses.append(f"m.{field} = ?")
            params.append(value)
    authority_value = normalize_choice(
        args.get("authority"),
        "authority",
        AUTHORITY_VALUES,
        default=None,
        strict=False,
    )
    if authority_value is not None:
        clauses.append("m.authority = ?")
        params.append(authority_value)
    retention_value = normalize_choice(
        args.get("retention"),
        "retention",
        RETENTION_VALUES,
        default=None,
        strict=False,
    )
    if retention_value is not None:
        clauses.append("m.retention = ?")
        params.append(retention_value)
    if "pinned" in args:
        clauses.append("m.pinned = ?")
        params.append(1 if parse_bool(args.get("pinned"), default=False) else 0)

    candidate_limit = min(_SQLITE_FTS_CANDIDATE_LIMIT, max(limit * 8, 50))
    sql = (
        "SELECT m.* "
        "FROM memories_fts "
        "JOIN memories m ON m.id = memories_fts.id "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY COALESCE(m.updated_at, m.created_at) DESC, m.created_at DESC, m.id DESC "
        "LIMIT ?"
    )
    params.append(candidate_limit)
    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            _sqlite_bootstrap_if_needed(conn)
            if not _sqlite_has_fts_table(conn) and not _sqlite_fts_available(conn):
                return []
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [_sqlite_row_to_memory(row) for row in rows]
    except Exception:
        # Keep search deterministic and robust by falling back to lexical ranking.
        return []


def search_rank(args: dict[str, Any], phase: str | None = None) -> list[dict[str, Any]]:
    query = str(args.get("query", "")).strip()
    include_deleted = parse_bool(args.get("include_deleted"), default=False)
    include_superseded = parse_bool(args.get("include_superseded"), default=False)
    limit = int(args.get("limit", 5))
    limit = max(1, min(limit, 20, max_search_results()))
    if "kind" in args and args.get("kind") is not None:
        validate_kind(str(args.get("kind")))
    if "authority" in args and args.get("authority") is not None:
        normalize_choice(args.get("authority"), "authority", AUTHORITY_VALUES)
    if "retention" in args and args.get("retention") is not None:
        normalize_choice(args.get("retention"), "retention", RETENTION_VALUES)
    if phase is None:
        _, phase = resolve_phase(args, query)

    query_tokens = tokenize(query)
    candidates: list[dict[str, Any]] = []
    if store_backend() == "sqlite" and query and _sqlite_fts_flag():
        candidates = _sqlite_fts_candidate_memories(
            args,
            query,
            include_deleted=include_deleted,
            include_superseded=include_superseded,
            limit=limit,
        )
    if not candidates:
        store = load_store()
        candidates = filter_memories(
            [memory for memory in store.get("memories", []) if isinstance(memory, dict)],
            args,
            include_deleted=include_deleted,
            include_superseded=include_superseded,
        )
    ranked = rank_memories_for_query(candidates, query_tokens, phase=phase, query_text=query)
    return [memory_to_match(memory, score) for score, memory in ranked[:limit]]


def match_is_deleted(match: dict[str, Any]) -> bool:
    return bool(match.get("deleted_at")) and not (
        match.get("superseded_by")
        and (
            str(match.get("deletion_reason") or "").strip().lower() == ""
            or str(match.get("deletion_reason") or "").strip().lower().startswith("superseded by ")
            or str(match.get("deletion_reason") or "").strip().lower().startswith("consolidated into ")
        )
    )


def memory_to_match(memory: dict[str, Any], score: float) -> dict[str, Any]:
    max_chars = max_chars_per_item()
    text_value = str(memory.get("text", ""))
    clipped_text = text_value[:max_chars]
    return {
        "id": memory.get("id"),
        "kind": memory.get("kind"),
        "text": clipped_text,
        "text_full_available": len(text_value) > len(clipped_text),
        "title": (memory.get("metadata") or {}).get("title") if isinstance(memory.get("metadata"), dict) else None,
        "source": memory.get("source", ""),
        "tags": memory.get("tags", []),
        "pinned": bool(memory.get("pinned", False)),
        "references": memory.get("references", []),
        "linked_ids": memory.get("linked_ids", memory.get("references", [])),
        "agent_id": memory.get("agent_id"),
        "role": memory.get("role"),
        "scope": memory.get("scope"),
        "domain": memory.get("domain"),
        "authority": memory.get("authority"),
        "retention": memory.get("retention"),
        "confidence": memory.get("confidence"),
        "parent_id": memory.get("parent_id"),
        "source_run_id": memory.get("source_run_id"),
        "metadata": memory.get("metadata", {}),
        "score": round(float(score), 3),
        "created_at": memory.get("created_at"),
        "updated_at": memory.get("updated_at"),
        "deleted_at": memory.get("deleted_at"),
        "deletion_reason": memory.get("deletion_reason"),
        "superseded_by": memory.get("superseded_by"),
    }


def cap_match_items(matches: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    item_cap = max_chars_per_item()
    total_cap = max_total_chars()
    warnings: list[str] = []
    capped: list[dict[str, Any]] = []
    used = 0
    for match in matches:
        item = dict(match)
        text_value = str(item.get("text", ""))
        if len(text_value) > item_cap:
            text_value = text_value[:item_cap]
            item["text"] = text_value
            item["text_full_available"] = True
        projected = used + len(text_value)
        if projected > total_cap and capped:
            warnings.append("match output capped by max_total_chars")
            break
        used = min(total_cap, projected)
        capped.append(item)
    return capped, warnings


def memory_bundle_item(memory: dict[str, Any], max_chars: int = 300, score: float | None = None) -> dict[str, Any]:
    item = {
        "id": memory.get("id"),
        "kind": memory.get("kind"),
        "text_preview": memory_preview(memory, max_chars=max_chars),
        "tags": memory.get("tags", []),
        "agent_id": memory.get("agent_id"),
        "role": memory.get("role"),
        "scope": memory.get("scope"),
        "domain": memory.get("domain"),
        "authority": memory.get("authority"),
        "retention": memory.get("retention"),
        "confidence": memory.get("confidence"),
        "linked_ids": memory.get("linked_ids", memory.get("references", [])),
        "parent_id": memory.get("parent_id"),
        "source_run_id": memory.get("source_run_id"),
        "pinned": bool(memory.get("pinned", False)),
        "created_at": memory.get("created_at"),
        "updated_at": memory.get("updated_at"),
    }
    metadata = memory.get("metadata", {})
    if isinstance(metadata, dict) and metadata:
        item["metadata"] = metadata
    if score is not None:
        item["score"] = round(float(score), 3)
    return item


def rank_against_query(memory: dict[str, Any], query: str, salience_module: Any | None = None) -> float:
    if not query.strip():
        return 0.0
    if salience_module is not None:
        try:
            breakdown = salience_module.signal_score(query, str(memory.get("text", "")))
            return max(0.0, min(1.0, float(getattr(breakdown, "final", 0.0))))
        except Exception:
            pass
    tokens = tokenize(query)
    return max(0.0, float(score_memory(tokens, memory, phase=None)))


def select_memories_by_query(
    memories: list[dict[str, Any]],
    query: str,
    limit: int,
    salience_module: Any | None = None,
) -> list[tuple[float, dict[str, Any]]]:
    scored: list[tuple[float, dict[str, Any]]] = []
    for memory in memories:
        score = rank_against_query(memory, query, salience_module)
        if score > 0.0 or not query.strip():
            scored.append((score, memory))
    scored.sort(key=lambda item: (item[0], str(item[1].get("created_at", ""))), reverse=True)
    return scored[:limit]


def apply_text_budget(lines: list[str], max_tokens: Any) -> tuple[list[str], bool, int, int]:
    if max_tokens is None:
        text = "\n".join(lines)
        return lines, False, estimate_tokens(text), 0
    try:
        budget = int(max_tokens)
    except (TypeError, ValueError):
        budget = 100000
    budget = max(1, min(budget, 100000))
    if not lines:
        return lines, False, 0, 0

    kept = [lines[0]]
    used = estimate_tokens(lines[0])
    dropped = 0
    for index, line in enumerate(lines[1:], start=1):
        line_tokens = estimate_tokens(line)
        if used + line_tokens > budget:
            dropped = len(lines) - index
            break
        kept.append(line)
        used += line_tokens
    if dropped:
        kept.append(f"[truncated: {dropped} more]")
    text = "\n".join(kept)
    return kept, dropped > 0, estimate_tokens(text), dropped


def query_log_path() -> Path:
    return memory_path().with_name("queries.jsonl")


def query_archive_path() -> Path:
    return memory_path().with_name("queries.archive.jsonl")


def query_logging_enabled() -> bool:
    return os.environ.get("MNEMO_LOG_QUERIES", "1").strip() != "0"


def _append_log_archive(source: Path, archive: Path) -> None:
    if not log_archive_enabled() or not source.exists():
        return
    try:
        archive.parent.mkdir(parents=True, exist_ok=True)
        with source.open("r", encoding="utf-8") as src, archive.open("a", encoding="utf-8") as dst:
            for line in src:
                dst.write(line)
    except Exception:
        pass


def _rotate_query_log(path: Path) -> None:
    rotated = path.with_name("queries.1.jsonl")
    if rotated.exists():
        _append_log_archive(rotated, query_archive_path())
        rotated.unlink()
    os.replace(path, rotated)


def _rotate_event_log(path: Path) -> None:
    rotated = path.with_name("events.1.jsonl")
    if rotated.exists():
        _append_log_archive(rotated, events_archive_path())
        rotated.unlink()
    os.replace(path, rotated)


def append_query_log(
    tool: str,
    args: dict[str, Any],
    matches: list[dict[str, Any]],
    phase: str | None = None,
) -> None:
    if not query_logging_enabled():
        return
    top_score = float(matches[0].get("score", 0.0)) if matches else 0.0
    row = {
        "ts": now_iso(),
        "tool": tool,
        "args": args,
        "top_ids": [str(match.get("id")) for match in matches if match.get("id")],
        "top_score": top_score,
        "n_results": len(matches),
    }
    if phase is not None or tool in {"mnemo_search", "mnemo_compact_context"}:
        row["phase"] = phase

    if store_backend() == "sqlite":
        try:
            with _sqlite_session() as conn:
                _sqlite_ensure_schema(conn)
                _sqlite_bootstrap_if_needed(conn)
                _sqlite_insert_event(conn, None, "query", row, str(row["ts"]))
        except Exception:
            pass
        return

    path = query_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= QUERY_LOG_MAX_BYTES:
            _rotate_query_log(path)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")
    except Exception:
        pass


def events_log_path() -> Path:
    return memory_path().with_name("events.jsonl")


def events_archive_path() -> Path:
    return memory_path().with_name("events.archive.jsonl")


def append_event_log(event: str, memory_id: str, details: dict[str, Any]) -> None:
    if not event_logging_enabled() or not memory_id:
        return
    if store_backend() == "sqlite":
        try:
            with _sqlite_session() as conn:
                _sqlite_ensure_schema(conn)
                _sqlite_bootstrap_if_needed(conn)
                _sqlite_insert_event(conn, memory_id, event, details, now_iso())
        except Exception:
            pass
        return

    path = events_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= EVENT_LOG_MAX_BYTES:
            _rotate_event_log(path)
        row = {"ts": now_iso(), "event": event, "id": memory_id, "details": details}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_event_rows(include_archive: bool = False) -> list[dict[str, Any]]:
    if store_backend() == "sqlite":
        try:
            with _sqlite_session() as conn:
                _sqlite_ensure_schema(conn)
                _sqlite_bootstrap_if_needed(conn)
                rows = conn.execute(
                    "SELECT memory_id, event_type, data_json, created_at FROM events WHERE event_type != 'query' ORDER BY created_at ASC, rowid ASC"
                ).fetchall()
            out: list[dict[str, Any]] = []
            for row in rows:
                payload = json.loads(str(row["data_json"] or "{}"))
                details = payload if isinstance(payload, dict) else {"value": payload}
                is_legacy_archive = bool(details.pop("_legacy_archive", False))
                if not include_archive and is_legacy_archive:
                    continue
                out.append(
                    {
                        "ts": str(row["created_at"] or ""),
                        "event": str(row["event_type"] or ""),
                        "id": str(row["memory_id"] or ""),
                        "details": details,
                    }
                )
            return out
        except Exception:
            return []

    path = events_log_path()
    paths = [path.with_name("events.1.jsonl"), path]
    if include_archive:
        paths.insert(0, events_archive_path())
    rows: list[dict[str, Any]] = []
    for item in paths:
        if not item.exists():
            continue
        try:
            with item.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except OSError:
            continue
    return rows


def search_memories(args: dict[str, Any]) -> dict[str, Any]:
    try:
        query = str(args.get("query", "")).strip()
        phase_label, phase = resolve_phase(args, query)
        matches = search_rank(args, phase)
        matches, cap_warnings = cap_match_items(matches)
    except Exception as exc:
        return tool_error(str(exc))
    append_query_log("mnemo_search", args, matches, phase_label)
    if not matches:
        rendered, truncated, est_tokens, _ = apply_text_budget(
            ["No matching project memories found."],
            args.get("max_tokens"),
        )
        return text_result(
            "\n".join(rendered),
            {
                "matches": [],
                "inferred_phase": phase_label,
                "truncated": truncated,
                "est_tokens": est_tokens,
                "warnings": [],
            },
        )

    lines = ["Relevant project memories:"]
    for item in matches:
        source = f" [{item['source']}]" if item["source"] else ""
        state = " deleted" if match_is_deleted(item) else ""
        state += " superseded" if item.get("superseded_by") else ""
        lines.append(f"- ({item['kind']}, {item['score']}){source}{state} {item['text']}")
    rendered, truncated, est_tokens, _ = apply_text_budget(lines, args.get("max_tokens"))
    return text_result(
        "\n".join(rendered),
        {
            "matches": matches,
            "inferred_phase": phase_label,
            "truncated": truncated,
            "est_tokens": est_tokens,
            "warnings": cap_warnings,
        },
    )


def memory_salience_check(args: dict[str, Any]) -> dict[str, Any]:
    text = str(args.get("text", "")).strip()
    if not text:
        return tool_error("text is required")
    try:
        limit = int(args.get("limit", 5))
        limit = max(1, min(limit, 50, max_search_results()))
        include_deleted = parse_bool(args.get("include_deleted"), default=False)
        include_superseded = parse_bool(args.get("include_superseded"), default=False)
        raw_threshold = args.get("threshold")
        threshold = 0.70 if raw_threshold is None else float(raw_threshold)
        threshold = max(0.0, min(1.0, threshold))
    except Exception as exc:
        return tool_error(str(exc))

    salience, reason = load_optional_agent_salience()
    if salience is None:
        return salience_unavailable_result(reason)

    store = load_store()
    candidates = [
        memory
        for memory in store.get("memories", [])
        if visible_memory(memory, include_deleted, include_superseded)
    ]
    matches: list[dict[str, Any]] = []
    for memory in candidates:
        memory_text = str(memory.get("text", ""))
        breakdown = salience.signal_score(text, memory_text)
        score = max(0.0, min(1.0, float(getattr(breakdown, "final", 0.0))))
        triggered = score >= threshold
        matches.append(
            {
                "memory_id": str(memory.get("id", "")),
                "kind": str(memory.get("kind", "")),
                "text_preview": memory_text[:200],
                "score": round(score, 3),
                "triggered": triggered,
                "margin": round(score - threshold, 3),
                "breakdown": salience_breakdown_payload(breakdown),
            }
        )
    matches.sort(key=lambda item: (float(item["score"]), str(item["memory_id"])), reverse=True)
    top_matches = matches[:limit]

    warnings: list[str] = []
    if not top_matches:
        warnings.append("No visible memories available for salience comparison.")

    anchors = [
        str(memory.get("text", ""))
        for memory in candidates
        if bool(memory.get("pinned")) or str(memory.get("kind", "")) == "invariant"
    ]
    max_anchor_drift = None
    if anchors:
        max_anchor_drift = max(
            max(0.0, min(1.0, float(salience.drift_score(anchor, text)))) for anchor in anchors
        )
        if max_anchor_drift >= 0.7:
            warnings.append(
                f"High drift against pinned/invariant anchors ({max_anchor_drift:.3f}); review invariants."
            )

    triggered_count = sum(1 for item in top_matches if item["triggered"])
    explanation = (
        f"{triggered_count}/{len(top_matches)} top matches met threshold {threshold:.2f}."
        if top_matches
        else f"No matches available at threshold {threshold:.2f}."
    )
    structured: dict[str, Any] = {
        "available": True,
        "triggered": triggered_count > 0,
        "threshold": threshold,
        "matches": top_matches,
        "warnings": warnings,
        "explanation": explanation,
    }
    if max_anchor_drift is not None:
        structured["anchor_drift"] = round(max_anchor_drift, 3)
    lines = [
        f"Salience check threshold: {threshold:.2f}",
        f"Triggered matches: {triggered_count}/{len(top_matches)}",
        explanation,
    ]
    if warnings:
        lines.append("Warnings: " + "; ".join(warnings))
    return text_result("\n".join(lines), structured)


def record_memory(args: dict[str, Any]) -> dict[str, Any]:
    try:
        kind = validate_kind(str(args.get("kind", "note")))
        text = normalize_optional_string(args.get("text"))
        summary = normalize_optional_string(args.get("summary"))
        body = normalize_optional_string(args.get("body"))
        if kind == "interaction_log" and not text and summary:
            text = summary
        if kind == "context_block" and not text and body:
            text = body
        if not text:
            text = summary or body
        if not text:
            return tool_error("text is required (or use summary/body aliases)")

        tags = normalize_tags(args.get("tags", []))
        references = normalize_references(args.get("references", []))
        linked_ids = normalize_linked_ids(args.get("linked_ids", references))
        if kind == "hippocampus_entry":
            evidence_ids = normalize_linked_ids(args.get("evidence_ids", []))
            for memory_id in evidence_ids:
                if memory_id not in linked_ids:
                    linked_ids.append(memory_id)
        if not references:
            references = list(linked_ids)
        elif not linked_ids:
            linked_ids = list(references)
        for linked_id in references:
            if linked_id not in linked_ids:
                linked_ids.append(linked_id)
        for linked_id in linked_ids:
            if linked_id not in references:
                references.append(linked_id)
        pinned = False
        if "pinned" in args and args.get("pinned") is not None:
            pinned = parse_strict_bool(args.get("pinned"), "pinned")
        supersedes = args.get("supersedes")
        supersedes_id = str(supersedes).strip() if supersedes is not None else None
        if supersedes_id == "":
            supersedes_id = None
        agent_id = normalize_optional_string(args.get("agent_id"))
        role = normalize_optional_string(args.get("role"))
        scope = normalize_optional_string(args.get("scope"))
        domain = normalize_optional_string(args.get("domain"))
        authority = normalize_choice(args.get("authority"), "authority", AUTHORITY_VALUES, default=None)
        retention = normalize_choice(args.get("retention"), "retention", RETENTION_VALUES, default=None)
        confidence = normalize_choice(args.get("confidence"), "confidence", CONFIDENCE_VALUES, default=None)
        parent_id = normalize_optional_string(args.get("parent_id"))
        source_run_id = normalize_optional_string(args.get("source_run_id"))
        metadata = normalize_metadata(args.get("metadata"))
        title = normalize_optional_string(args.get("title"))
        if title:
            metadata["title"] = title
        feedback_type = normalize_optional_string(args.get("feedback_type"))
        if kind == "agent_feedback" and feedback_type:
            metadata["feedback_type"] = feedback_type
        if kind == "agent_feedback" and not any([agent_id, role, domain]):
            return tool_error("at least one of agent_id, role, or domain is required")

        if kind == "interaction_log":
            authority = authority or "low"
            retention = retention or "compressible"
            if role is None:
                role = "coordinator"
        elif kind == "context_block":
            authority = authority or "medium"
            retention = retention or "durable"
        elif kind == "hippocampus_entry":
            authority = authority or "medium"
            retention = retention or "durable"
            confidence = confidence or "medium"
        elif kind == "agent_feedback":
            authority = authority or "medium"
            retention = retention or "durable"
    except ValueError as exc:
        return tool_error(str(exc))

    try:
        with MemoryFileLock(store_lock_path()):
            store = load_store()
            memories = store.setdefault("memories", [])
            old = find_memory(store, supersedes_id) if supersedes_id else None
            if supersedes_id and old is None:
                return tool_error(f"supersedes id not found: {supersedes_id}")

            candidate_norm = normalize_text(text)
            candidate_tokens = tokenize(candidate_norm)
            near_duplicate_of: list[str] = []
            for memory in duplicate_candidates(memories, kind, supersedes_id):
                existing_norm = normalize_text(str(memory.get("text", "")))
                if existing_norm == candidate_norm:
                    structured = {
                        "memory": memory,
                        "memory_file": str(memory_path()),
                        "duplicate": True,
                    }
                    return text_result(
                        f"Duplicate {kind} memory already exists as {memory.get('id')}.",
                        structured,
                    )
                sim = jaccard(candidate_tokens, tokenize(existing_norm))
                if sim >= 0.9:
                    near_duplicate_of.append(str(memory.get("id")))

            ok_cap, cap_error = enforce_size_cap(store)
            if not ok_cap:
                return tool_error(cap_error or "memory cap reached")
            memories = store.setdefault("memories", [])

            memory = new_memory(
                make_id(text),
                kind,
                text,
                str(args.get("source", "")).strip(),
                tags,
                references,
                pinned,
                agent_id=agent_id,
                role=role,
                scope=scope,
                domain=domain,
                authority=authority,
                retention=retention,
                confidence=confidence,
                linked_ids=linked_ids,
                parent_id=parent_id,
                source_run_id=source_run_id,
                metadata=metadata,
            )
            memories.append(memory)
            if old is not None:
                old["superseded_by"] = memory["id"]
            save_store(store)
            append_event_log("create", memory["id"], {"kind": kind, "supersedes": supersedes_id})
            if old is not None:
                append_event_log("supersede", str(old.get("id")), {"superseded_by": memory["id"]})

            structured = {
                "memory": memory,
                "memory_file": str(memory_path()),
                "duplicate": False,
            }
            if near_duplicate_of:
                structured["near_duplicate_of"] = near_duplicate_of
            if supersedes_id:
                structured["supersedes"] = supersedes_id
            return text_result(f"Recorded {kind} memory {memory['id']}.", structured)
    except LockTimeout as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")


def update_memory(args: dict[str, Any]) -> dict[str, Any]:
    memory_id = str(args.get("id", "")).strip()
    if not memory_id:
        return tool_error("id is required")

    try:
        with MemoryFileLock(store_lock_path()):
            store = load_store()
            memory = find_memory(store, memory_id)
            if memory is None:
                return tool_error(f"memory not found: {memory_id}")
            changed: list[str] = []
            if "text" in args and args.get("text") is not None:
                text = str(args.get("text", "")).strip()
                if not text:
                    return tool_error("text cannot be empty")
                memory["text"] = text
                changed.append("text")
            if "kind" in args and args.get("kind") is not None:
                memory["kind"] = validate_kind(str(args["kind"]))
                changed.append("kind")
            if "tags" in args and args.get("tags") is not None:
                memory["tags"] = normalize_tags(args.get("tags"))
                changed.append("tags")
            if "source" in args and args.get("source") is not None:
                memory["source"] = str(args.get("source", "")).strip()
                changed.append("source")
            if "pinned" in args and args.get("pinned") is not None:
                memory["pinned"] = parse_bool(args.get("pinned"), default=False)
                changed.append("pinned")
            if "references" in args and args.get("references") is not None:
                normalized_refs = normalize_references(args.get("references"))
                memory["references"] = normalized_refs
                # Keep link fields mirrored when callers update legacy references only.
                memory["linked_ids"] = list(normalized_refs)
                changed.append("references")
            if "linked_ids" in args and args.get("linked_ids") is not None:
                normalized_links = normalize_linked_ids(args.get("linked_ids"))
                memory["linked_ids"] = normalized_links
                # Keep legacy references aligned for backward-compatible reads.
                memory["references"] = list(normalized_links)
                changed.append("linked_ids")
            if "agent_id" in args:
                memory["agent_id"] = normalize_optional_string(args.get("agent_id"))
                changed.append("agent_id")
            if "role" in args:
                memory["role"] = normalize_optional_string(args.get("role"))
                changed.append("role")
            if "scope" in args:
                memory["scope"] = normalize_optional_string(args.get("scope"))
                changed.append("scope")
            if "domain" in args:
                memory["domain"] = normalize_optional_string(args.get("domain"))
                changed.append("domain")
            if "authority" in args:
                memory["authority"] = normalize_choice(
                    args.get("authority"),
                    "authority",
                    AUTHORITY_VALUES,
                    default=None,
                )
                changed.append("authority")
            if "retention" in args:
                memory["retention"] = normalize_choice(
                    args.get("retention"),
                    "retention",
                    RETENTION_VALUES,
                    default=None,
                )
                changed.append("retention")
            if "confidence" in args:
                memory["confidence"] = normalize_choice(
                    args.get("confidence"),
                    "confidence",
                    CONFIDENCE_VALUES,
                    default=None,
                )
                changed.append("confidence")
            if "parent_id" in args:
                memory["parent_id"] = normalize_optional_string(args.get("parent_id"))
                changed.append("parent_id")
            if "source_run_id" in args:
                memory["source_run_id"] = normalize_optional_string(args.get("source_run_id"))
                changed.append("source_run_id")
            if "metadata" in args and args.get("metadata") is not None:
                memory["metadata"] = normalize_metadata(args.get("metadata"))
                changed.append("metadata")
            merge_link_fields(memory)
            memory["updated_at"] = now_iso()
            save_store(store)
            if changed:
                append_event_log("update", memory_id, {"changed": changed})
            return text_result(f"Updated memory {memory_id}.", {"memory": memory})
    except LockTimeout as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")


def delete_memory(args: dict[str, Any]) -> dict[str, Any]:
    memory_id = str(args.get("id", "")).strip()
    if not memory_id:
        return tool_error("id is required")
    reason = str(args.get("reason", "")).strip() or None
    try:
        with MemoryFileLock(store_lock_path()):
            store = load_store()
            memory = find_memory(store, memory_id)
            if memory is None:
                return tool_error(f"memory not found: {memory_id}")
            memory["deleted_at"] = memory.get("deleted_at") or now_iso()
            memory["deletion_reason"] = reason
            save_store(store)
            append_event_log("delete", memory_id, {"reason": reason})
            return text_result(f"Deleted memory {memory_id}.", {"memory": memory})
    except LockTimeout as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")


def memory_get(args: dict[str, Any]) -> dict[str, Any]:
    memory_id = str(args.get("id", "")).strip()
    if not memory_id:
        return tool_error("id is required")
    full = parse_bool(args.get("full"), default=False)
    include_deleted = parse_bool(args.get("include_deleted"), default=True)
    include_superseded = parse_bool(args.get("include_superseded"), default=True)

    store = load_store()
    memory = find_memory(store, memory_id)
    if memory is None:
        return tool_error(f"memory not found: {memory_id}")
    if not visible_memory(memory, include_deleted, include_superseded):
        return tool_error(f"memory {memory_id} is hidden by deleted/superseded filters")

    if full:
        return text_result(
            f"Memory {memory_id} ({memory.get('kind')}):\n{memory.get('text', '')}",
            {"memory": migrate_memory(memory), "full": True},
        )
    item = memory_bundle_item(memory, max_chars=max_chars_per_item())
    text_preview = str(item.get("text_preview", ""))
    return text_result(
        f"Memory {memory_id} ({memory.get('kind')} preview):\n{text_preview}",
        {"memory": item, "full": False},
    )


def export_root() -> Path:
    root = state_dir() / "exports"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _default_export_path(format_value: str) -> Path:
    root = export_root()
    mapping = {
        "jsonl": root / "memory.jsonl",
        "json": root / "memory.json",
        "markdown": root / "memory.md",
        "hippocampus_markdown": root / "hippocampus.md",
        "agent_feedback_markdown": root / "agent_feedback.md",
        "startup_context_markdown": root / "startup_context_latest.md",
    }
    return mapping.get(format_value, root / "memory.jsonl")


def _export_markdown(memories: list[dict[str, Any]], title: str) -> str:
    lines = [f"# {title}", ""]
    for memory in memories:
        memory_id = str(memory.get("id", ""))
        kind = str(memory.get("kind", "note"))
        created = str(memory.get("created_at", ""))
        text = str(memory.get("text", ""))
        lines.append(f"## {memory_id} ({kind})")
        lines.append(f"- created_at: {created}")
        if memory.get("domain"):
            lines.append(f"- domain: {memory.get('domain')}")
        if memory.get("role"):
            lines.append(f"- role: {memory.get('role')}")
        if memory.get("agent_id"):
            lines.append(f"- agent_id: {memory.get('agent_id')}")
        lines.append("")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def memory_export(args: dict[str, Any]) -> dict[str, Any]:
    format_value = str(args.get("format", "")).strip().lower()
    if not format_value:
        return tool_error("format is required")
    allowed = {
        "jsonl",
        "json",
        "markdown",
        "hippocampus_markdown",
        "agent_feedback_markdown",
        "startup_context_markdown",
    }
    if format_value not in allowed:
        return tool_error("format must be one of: jsonl, json, markdown, hippocampus_markdown, agent_feedback_markdown, startup_context_markdown")

    path_arg = normalize_optional_string(args.get("path"))
    output_path = Path(path_arg).expanduser() if path_arg else _default_export_path(format_value)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    include_deleted = parse_bool(args.get("include_deleted"), default=False)
    max_records = max(1, min(int(args.get("max_records", 500)), 5000))

    store = load_store()
    memories = [memory for memory in store.get("memories", []) if isinstance(memory, dict)]
    memories = filter_memories(
        memories,
        {
            "kind": args.get("kind"),
            "domain": args.get("domain"),
            "agent_id": args.get("agent_id"),
            "role": args.get("role"),
        },
        include_deleted=include_deleted,
        include_superseded=False,
    )
    memories.sort(key=lambda memory: (str(memory.get("created_at", "")), str(memory.get("id", ""))))
    memories = memories[:max_records]

    if format_value == "hippocampus_markdown" and not args.get("kind"):
        memories = [memory for memory in memories if str(memory.get("kind")) == "hippocampus_entry"]
    if format_value == "agent_feedback_markdown" and not args.get("kind"):
        memories = [memory for memory in memories if str(memory.get("kind")) == "agent_feedback"]
    if format_value == "startup_context_markdown":
        startup = memory_recall({"mode": "startup"})
        structured = startup.get("structuredContent", {})
        lines = [
            "# Startup Context",
            "",
            f"- summary: {structured.get('summary', '')}",
            "",
            "## Recent Logs",
        ]
        for item in structured.get("recent_logs", []):
            if isinstance(item, dict):
                lines.append(f"- {item.get('id')}: {item.get('text_preview')}")
        lines.append("")
        lines.append("## Context Blocks")
        for item in structured.get("context_blocks", []):
            if isinstance(item, dict):
                lines.append(f"- {item.get('id')}: {item.get('text_preview')}")
        payload = "\n".join(lines).strip() + "\n"
    elif format_value == "jsonl":
        payload = "".join(
            json.dumps(migrate_memory(memory), separators=(",", ":"), ensure_ascii=False) + "\n"
            for memory in memories
        )
    elif format_value == "json":
        payload = json.dumps({"version": 1, "memories": [migrate_memory(memory) for memory in memories]}, indent=2, ensure_ascii=False) + "\n"
    elif format_value in {"markdown", "hippocampus_markdown", "agent_feedback_markdown"}:
        title = {
            "markdown": "Mnemo Memory Export",
            "hippocampus_markdown": "Mnemo Hippocampus Entries",
            "agent_feedback_markdown": "Mnemo Agent Feedback",
        }[format_value]
        payload = _export_markdown(memories, title)
    else:
        payload = ""

    with output_path.open("w", encoding="utf-8") as f:
        f.write(payload)
    return text_result(
        f"Exported {len(memories)} memories to {output_path}.",
        {
            "format": format_value,
            "path": str(output_path),
            "records": len(memories),
        },
    )


def recent_memories(args: dict[str, Any]) -> dict[str, Any]:
    try:
        limit = int(args.get("limit", 10))
        limit = max(1, min(limit, 50, max_recent_results()))
        include_deleted = parse_bool(args.get("include_deleted"), default=False)
        include_superseded = parse_bool(args.get("include_superseded"), default=False)
        memories = [
            memory_to_match(memory, 0.0)
            for memory in load_store().get("memories", [])
            if visible_memory(memory, include_deleted, include_superseded)
        ]
        recent = list(reversed(memories[-limit:]))
    except Exception as exc:
        return tool_error(str(exc))

    append_query_log("mnemo_recent", args, recent)
    if not recent:
        return text_result("No project memories recorded yet.", {"memories": []})

    lines = ["Recent project memories:"]
    for memory in recent:
        source = f" [{memory.get('source')}]" if memory.get("source") else ""
        state = " deleted" if match_is_deleted(memory) else ""
        state += " superseded" if memory.get("superseded_by") else ""
        lines.append(f"- {memory.get('id')} ({memory.get('kind')}){source}{state}: {memory.get('text')}")
    return text_result("\n".join(lines), {"memories": recent})


def compact_context(args: dict[str, Any]) -> dict[str, Any]:
    try:
        query = str(args.get("query", "")).strip()
        limit = int(args.get("limit", 8))
        limit = max(1, min(limit, 20))
        search_args = dict(args)
        search_args["query"] = query
        search_args["limit"] = limit
        phase_label, phase = resolve_phase(search_args, query)
        matches = search_rank(search_args, phase)
    except Exception as exc:
        return tool_error(str(exc))

    append_query_log("mnemo_compact_context", args, matches, phase_label)
    if not matches:
        rendered, truncated, est_tokens, _ = apply_text_budget(
            ["[Project Memory]", "No relevant memories found."],
            args.get("max_tokens"),
        )
        return text_result(
            "\n".join(rendered),
            {
                "matches": [],
                "inferred_phase": phase_label,
                "truncated": truncated,
                "est_tokens": est_tokens,
            },
        )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for match in matches:
        grouped.setdefault(str(match["kind"]), []).append(match)

    lines = ["[Project Memory]", f"Query: {query or '(recent memories)'}"]
    for kind in ORDERED_KINDS:
        items = grouped.get(kind, [])
        if not items:
            continue
        lines.append("")
        lines.append(kind.replace("_", " ").title() + ":")
        for item in items:
            source = f"[{item['source']}] " if item["source"] else ""
            pinned = "\u2605 " if item.get("pinned") else ""
            refs = len(item.get("references", []))
            refs_text = f" \u2192 refs: {refs}" if refs > 0 else ""
            lines.append(f"- {pinned}{source}{item['text']}{refs_text}")

    rendered, truncated, est_tokens, _ = apply_text_budget(lines, args.get("max_tokens"))
    return text_result(
        "\n".join(rendered),
        {
            "matches": matches,
            "inferred_phase": phase_label,
            "truncated": truncated,
            "est_tokens": est_tokens,
        },
    )


def render_history_event(row: dict[str, Any]) -> str:
    ts = str(row.get("ts", ""))
    event = str(row.get("event", ""))
    details = row.get("details", {})
    details = details if isinstance(details, dict) else {}
    if event == "create":
        kind = details.get("kind", "")
        return f"- {ts} create ({kind})"
    if event == "update":
        changed = details.get("changed", [])
        changed_text = ", ".join(str(item) for item in changed) if isinstance(changed, list) else ""
        return f"- {ts} update (changed: {changed_text})"
    if event == "supersede":
        return f"- {ts} supersede \u2192 {details.get('superseded_by')}"
    if event == "delete":
        reason = details.get("reason")
        return f"- {ts} delete ({reason})" if reason else f"- {ts} delete"
    if event == "archive":
        return f"- {ts} archive ({details.get('archived_to')})"
    return f"- {ts} {event}"


def _compute_history(memory_id: str, limit: int, include_archive: bool) -> tuple[str, dict[str, Any]]:
    if not event_logging_enabled():
        return "No event log available; set MNEMO_LOG_EVENTS=1 to enable.", {"events": []}

    if store_backend() == "json":
        path = events_log_path()
        if (
            not path.exists()
            and not path.with_name("events.1.jsonl").exists()
            and (not include_archive or not events_archive_path().exists())
        ):
            return "No event log available; set MNEMO_LOG_EVENTS=1 to enable.", {"events": []}
    events = [row for row in read_event_rows(include_archive) if row.get("id") == memory_id]
    events.sort(key=lambda row: str(row.get("ts", "")))
    events = events[-limit:]
    if not events:
        return f"History for {memory_id}:\nNo events found.", {"events": []}
    lines = [f"History for {memory_id}:"]
    lines.extend(render_history_event(row) for row in events)
    return "\n".join(lines), {"events": events}


def _compute_related(
    root_id: str,
    depth: int,
    include_deleted: bool,
    include_superseded: bool,
) -> tuple[str, dict[str, Any]]:
    store = load_store()
    memories = [memory for memory in store.get("memories", []) if isinstance(memory, dict)]
    by_id = {str(memory.get("id")): memory for memory in memories}
    incoming: dict[str, list[dict[str, Any]]] = {}
    for memory in memories:
        for reference in memory.get("references", []):
            incoming.setdefault(str(reference), []).append(memory)

    seen = {root_id}
    queue: list[tuple[str, int]] = [(root_id, 0)]
    related: list[dict[str, Any]] = []
    while queue:
        current_id, distance = queue.pop(0)
        if distance >= depth:
            continue
        current = by_id.get(current_id)
        neighbors: list[tuple[str, str, dict[str, Any] | None]] = []
        if current is not None:
            for reference in current.get("references", []):
                ref_id = str(reference)
                neighbors.append(("outgoing", ref_id, by_id.get(ref_id)))
        for memory in incoming.get(current_id, []):
            neighbors.append(("incoming", str(memory.get("id")), memory))

        for direction, next_id, memory in neighbors:
            if next_id in seen:
                continue
            seen.add(next_id)
            if memory is None:
                continue
            next_distance = distance + 1
            if visible_memory(memory, include_deleted, include_superseded):
                related.append(
                    {
                        "id": next_id,
                        "direction": direction,
                        "distance": next_distance,
                        "memory": memory,
                    }
                )
            queue.append((next_id, next_distance))

    related.sort(key=lambda item: (int(item["distance"]), str(item["direction"]), str(item["id"])))
    if not related:
        return f"No related memories found for {root_id}.", {"related": []}
    lines = [f"Related memories for {root_id}:"]
    for item in related:
        memory = item["memory"]
        lines.append(
            f"- {item['id']} ({item['direction']}, distance {item['distance']}): {memory.get('text')}"
        )
    return "\n".join(lines), {"related": related}


def memory_inspect(args: dict[str, Any]) -> dict[str, Any]:
    memory_id = str(args.get("id", "")).strip()
    if not memory_id:
        return tool_error("id is required")
    mode = str(args.get("mode", "both")).strip().lower() or "both"
    if mode not in {"history", "related", "both"}:
        return tool_error("mode must be one of: history, related, both")

    limit = int(args.get("limit", 50))
    limit = max(1, min(limit, 200))
    depth = int(args.get("depth", 1))
    depth = max(1, min(depth, 3))
    include_deleted = parse_bool(args.get("include_deleted"), default=False)
    include_superseded = parse_bool(args.get("include_superseded"), default=False)
    include_archive = parse_bool(args.get("include_archive"), default=False)

    sections: list[str] = []
    structured: dict[str, Any] = {"id": memory_id, "mode": mode}
    if mode in {"history", "both"}:
        history_text, history_structured = _compute_history(memory_id, limit, include_archive)
        sections.append(history_text)
        structured["events"] = history_structured.get("events", [])
    if mode in {"related", "both"}:
        related_text, related_structured = _compute_related(memory_id, depth, include_deleted, include_superseded)
        sections.append(related_text)
        structured["related"] = related_structured.get("related", [])

    return text_result("\n\n".join(section for section in sections if section), structured)


def group_tokens(memories: list[dict[str, Any]]) -> set[str]:
    tokens: set[str] = set()
    for memory in memories:
        tokens.update(tokenize(str(memory.get("text", ""))))
    return tokens


def drift_interpretation(drift: float) -> str:
    if drift < 0.3:
        return "low"
    if drift <= 0.7:
        return "medium"
    return "high"


def memory_drift_compute(
    recent_count: int = 50,
    older_count: int = 50,
    store: dict[str, Any] | None = None,
) -> dict[str, Any]:
    recent_count = max(2, min(int(recent_count), 200))
    older_count = max(2, min(int(older_count), 200))
    local_store = store
    if local_store is None:
        try:
            local_store = load_store()
        except Exception:
            local_store = {"version": 1, "memories": []}
    memories = [
        memory
        for memory in local_store.get("memories", [])
        if is_active(memory)
        and not memory.get("pinned")
        and str(memory.get("kind", "")) != "invariant"
    ]
    memories.sort(key=lambda memory: str(memory.get("created_at", "")))
    if len(memories) < 4:
        return {
            "value": 0.0,
            "recent_count": recent_count,
            "older_count": older_count,
            "interpretation": "insufficient_history",
        }

    half = len(memories) // 2
    recent_n = min(recent_count, half)
    older_n = min(older_count, half)
    older = memories[:older_n]
    recent = memories[-recent_n:]
    drift = 1.0 - jaccard(group_tokens(recent), group_tokens(older))
    drift = max(0.0, min(1.0, drift))
    drift = round(drift, 3)
    structured = {
        "value": drift,
        "recent_count": len(recent),
        "older_count": len(older),
        "interpretation": drift_interpretation(drift),
    }
    return structured


def timestamp_key(memory: dict[str, Any]) -> str:
    return str(memory.get("updated_at") or memory.get("created_at") or "")


def build_consolidation_clusters(store: dict[str, Any], threshold: float) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    for kind in MEMORY_KINDS:
        memories = [
            memory
            for memory in store.get("memories", [])
            if memory.get("kind") == kind
            and is_active(memory)
            and not memory.get("pinned")
        ]
        if len(memories) < 2:
            continue
        parent = list(range(len(memories)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        tokens = [tokenize(str(memory.get("text", ""))) for memory in memories]
        for i in range(len(memories)):
            for j in range(i + 1, len(memories)):
                if jaccard(tokens[i], tokens[j]) >= threshold:
                    union(i, j)

        grouped: dict[int, list[dict[str, Any]]] = {}
        for index, memory in enumerate(memories):
            grouped.setdefault(find(index), []).append(memory)
        for members in grouped.values():
            if len(members) < 2:
                continue
            survivor = max(members, key=timestamp_key)
            ids = [str(memory.get("id")) for memory in members]
            to_retire = [memory_id for memory_id in ids if memory_id != survivor.get("id")]
            clusters.append(
                {
                    "kind": kind,
                    "size": len(members),
                    "ids": ids,
                    "survivor": str(survivor.get("id")),
                    "to_retire": to_retire,
                }
            )
    return clusters


def render_consolidation_text(clusters: list[dict[str, Any]], threshold: float, applied: bool, retired: int) -> str:
    lines = [f"Consolidation candidates (threshold {threshold}):"]
    if not clusters:
        lines.append("- none")
    for cluster in clusters:
        retire = ", ".join(cluster["to_retire"])
        lines.append(
            f"- {cluster['kind']} cluster ({cluster['size']} members): "
            f"keep {cluster['survivor']}, retire {retire}"
        )
    if applied:
        lines.append("")
        lines.append(f"Applied: {len(clusters)} cluster(s), {retired} retired.")
    else:
        lines.append("")
        lines.append("Run with dry_run=false to apply.")
    return "\n".join(lines)


def _consolidate_maintenance(args: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    threshold = consolidate_threshold(args.get("threshold") if "threshold" in args else None)

    if dry_run:
        clusters = build_consolidation_clusters(load_store(), threshold)
        structured = {"action": "consolidate", "applied": False, "threshold": threshold, "clusters": clusters}
        return text_result(render_consolidation_text(clusters, threshold, False, 0), structured)

    retired = 0
    try:
        with MemoryFileLock(store_lock_path()):
            store = load_store()
            clusters = build_consolidation_clusters(store, threshold)
            by_id = {str(memory.get("id")): memory for memory in store.get("memories", [])}
            for cluster in clusters:
                survivor = cluster["survivor"]
                for memory_id in cluster["to_retire"]:
                    memory = by_id.get(memory_id)
                    if memory is None:
                        continue
                    memory["superseded_by"] = survivor
                    retired += 1
                    append_event_log("supersede", memory_id, {"superseded_by": survivor})
            if retired:
                save_store(store)
    except LockTimeout as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")

    structured = {"action": "consolidate", "applied": True, "threshold": threshold, "clusters": clusters}
    return text_result(render_consolidation_text(clusters, threshold, True, retired), structured)


def _import_json_maintenance(args: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    path_arg = normalize_optional_string(args.get("path"))
    source_path = Path(path_arg).expanduser() if path_arg else memory_path()
    rows = _read_json_memories(source_path)
    if not rows:
        return text_result(
            f"No importable memories found in {source_path}.",
            {"action": "import_json", "path": str(source_path), "imported_count": 0, "skipped_count": 0, "errors": []},
        )

    errors: list[str] = []
    if store_backend() == "sqlite":
        try:
            with _sqlite_session() as conn:
                _sqlite_ensure_schema(conn)
                _sqlite_bootstrap_if_needed(conn)
                imported, skipped, errors = _sqlite_import_memory_rows(conn, rows, dry_run=dry_run)
                if imported and not dry_run:
                    _sqlite_set_meta(conn, "last_import_at", now_iso())
        except Exception as exc:
            return tool_error(f"{type(exc).__name__}: {exc}")
    else:
        try:
            with MemoryFileLock(store_lock_path()):
                store = load_store()
                existing = [memory for memory in store.get("memories", []) if isinstance(memory, dict)]
                existing_ids = {str(memory.get("id", "")) for memory in existing}
                existing_hashes = {memory_content_hash(memory) for memory in existing}
                imported = 0
                skipped = 0
                for row in rows:
                    try:
                        memory = migrate_memory(row)
                        memory_id = str(memory.get("id", "")).strip()
                        digest = memory_content_hash(memory)
                        if not memory_id or memory_id in existing_ids or digest in existing_hashes:
                            skipped += 1
                            continue
                        imported += 1
                        if not dry_run:
                            existing.append(memory)
                            existing_ids.add(memory_id)
                            existing_hashes.add(digest)
                    except Exception as exc:
                        errors.append(str(exc))
                if imported and not dry_run:
                    store["memories"] = existing
                    save_store(store)
        except Exception as exc:
            return tool_error(f"{type(exc).__name__}: {exc}")

    return text_result(
        f"Import complete from {source_path}: imported={imported}, skipped={skipped}.",
        {
            "action": "import_json",
            "path": str(source_path),
            "dry_run": dry_run,
            "imported_count": imported,
            "skipped_count": skipped,
            "errors": errors,
        },
    )


def memory_maintenance(args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action", "")).strip().lower()
    if action not in {"compact_logs", "consolidate", "import_json"}:
        return tool_error("action must be one of: compact_logs, consolidate, import_json")
    dry_run = parse_bool(args.get("dry_run"), default=True)
    if action == "compact_logs":
        return _compact_logs_maintenance(args, dry_run)
    if action == "import_json":
        return _import_json_maintenance(args, dry_run)
    return _consolidate_maintenance(args, dry_run)


def memory_link(args: dict[str, Any]) -> dict[str, Any]:
    source_id = str(args.get("source_id", "")).strip()
    target_id = str(args.get("target_id", "")).strip()
    relation = normalize_optional_string(args.get("relation"))
    bidirectional = parse_bool(args.get("bidirectional"), default=False)
    if not source_id or not target_id:
        return tool_error("source_id and target_id are required")
    if source_id == target_id:
        return tool_error("source_id and target_id must be different")

    try:
        with MemoryFileLock(store_lock_path()):
            store = load_store()
            source = find_memory(store, source_id)
            target = find_memory(store, target_id)
            if source is None:
                return tool_error(f"memory not found: {source_id}")
            if target is None:
                return tool_error(f"memory not found: {target_id}")

            source_links = normalize_linked_ids(source.get("linked_ids", source.get("references", [])))
            if target_id not in source_links:
                source_links.append(target_id)
            source["linked_ids"] = source_links
            source["references"] = list(source_links)
            source["updated_at"] = now_iso()
            if relation:
                source_meta = normalize_metadata(source.get("metadata"))
                relations = source_meta.get("link_relations", {})
                if not isinstance(relations, dict):
                    relations = {}
                relations[str(target_id)] = relation
                source_meta["link_relations"] = relations
                source["metadata"] = source_meta

            target_links: list[str] = normalize_linked_ids(target.get("linked_ids", target.get("references", [])))
            if bidirectional:
                if source_id not in target_links:
                    target_links.append(source_id)
                target["linked_ids"] = target_links
                target["references"] = list(target_links)
                target["updated_at"] = now_iso()
                if relation:
                    target_meta = normalize_metadata(target.get("metadata"))
                    reverse_relations = target_meta.get("link_relations", {})
                    if not isinstance(reverse_relations, dict):
                        reverse_relations = {}
                    reverse_relations[str(source_id)] = relation
                    target_meta["link_relations"] = reverse_relations
                    target["metadata"] = target_meta

            save_store(store)
            append_event_log(
                "link",
                source_id,
                {"target_id": target_id, "relation": relation, "bidirectional": bidirectional},
            )
            if bidirectional:
                append_event_log(
                    "link",
                    target_id,
                    {"target_id": source_id, "relation": relation, "bidirectional": True},
                )
    except LockTimeout as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")

    return text_result(
        f"Linked {source_id} -> {target_id}.",
        {
            "source_id": source_id,
            "target_id": target_id,
            "relation": relation,
            "bidirectional": bidirectional,
            "source_links": source.get("linked_ids", source.get("references", [])),  # type: ignore[name-defined]
            "target_links": target.get("linked_ids", target.get("references", [])),  # type: ignore[name-defined]
        },
    )


def _build_recall_bundle(
    store: dict[str, Any],
    *,
    mode: str,
    agent_id: str | None,
    role: str | None,
    domain: str | None,
    task: str,
    query: str,
    recent_logs_limit: int,
    max_blocks: int,
    max_hippocampus: int,
    max_feedback: int,
    include_pinned: bool,
    include_recent_logs: bool,
    salience_module: Any | None,
) -> tuple[str, dict[str, Any]]:
    visible = [
        memory
        for memory in store.get("memories", [])
        if isinstance(memory, dict) and visible_memory(memory, False, False)
    ]

    if mode == "startup":
        startup_role = role or "coordinator"
        logs = [
            memory
            for memory in visible
            if str(memory.get("kind", "")) == "interaction_log"
            and (agent_id is None or str(memory.get("agent_id", "")).strip() == agent_id)
            and (startup_role is None or str(memory.get("role", "")).strip() == startup_role)
        ]
        logs.sort(key=lambda memory: str(memory.get("created_at", "")), reverse=True)
        recent_logs = logs[:recent_logs_limit]

        linked_ids: list[str] = []
        for memory in recent_logs:
            for linked_id in normalize_linked_ids(memory.get("linked_ids", memory.get("references", []))):
                if linked_id not in linked_ids:
                    linked_ids.append(linked_id)
        linked_blocks = [
            memory
            for memory_id in linked_ids
            for memory in [find_memory(store, memory_id)]
            if memory is not None and str(memory.get("kind", "")) == "context_block" and visible_memory(memory, False, False)
        ]

        block_candidates = [memory for memory in visible if str(memory.get("kind", "")) == "context_block"]
        scored_blocks = select_memories_by_query(block_candidates, query, max_blocks * 2, salience_module)
        blocks_by_id: dict[str, dict[str, Any]] = {}
        for memory in linked_blocks:
            blocks_by_id[str(memory.get("id"))] = memory
        for _, memory in scored_blocks:
            if len(blocks_by_id) >= max_blocks:
                break
            blocks_by_id.setdefault(str(memory.get("id")), memory)
        context_blocks = list(blocks_by_id.values())[:max_blocks]

        hippocampus = [memory for memory in visible if str(memory.get("kind", "")) == "hippocampus_entry"]
        scored_hippocampus = select_memories_by_query(hippocampus, query, max_hippocampus, salience_module)
        hippocampus_entries = [memory for _, memory in scored_hippocampus]

        feedback = [
            memory
            for memory in visible
            if str(memory.get("kind", "")) == "agent_feedback"
            and (
                (agent_id is not None and str(memory.get("agent_id", "")).strip() == agent_id)
                or (startup_role and str(memory.get("role", "")).strip() == startup_role)
                or (
                    query
                    and str(memory.get("domain", "")).strip()
                    and str(memory.get("domain", "")).strip().lower() in query.lower()
                )
            )
        ]
        feedback.sort(key=lambda memory: str(memory.get("created_at", "")), reverse=True)
        feedback = feedback[:max_feedback]

        pinned: list[dict[str, Any]] = []
        if include_pinned:
            pinned = [
                memory
                for memory in visible
                if bool(memory.get("pinned"))
                or str(memory.get("authority", "")).strip().lower() in {"high", "pinned"}
                or str(memory.get("retention", "")).strip().lower() == "pinned"
                or str(memory.get("kind", "")) == "invariant"
            ][:20]

        warnings: list[str] = []
        if not recent_logs:
            warnings.append("No interaction logs matched the startup context filters.")
        if not context_blocks:
            warnings.append("No context blocks selected for startup context.")
        if not hippocampus_entries:
            warnings.append("No hippocampus entries selected for startup context.")

        summary = (
            f"startup bundle: {len(recent_logs)} logs, {len(context_blocks)} blocks, "
            f"{len(hippocampus_entries)} hippocampus entries, {len(feedback)} feedback items."
        )
        return summary, {
            "mode": "startup",
            "role": startup_role,
            "agent_id": agent_id,
            "recent_logs": [memory_bundle_item(memory, max_chars=500) for memory in recent_logs],
            "context_blocks": [memory_bundle_item(memory, max_chars=1200) for memory in context_blocks],
            "hippocampus_entries": [memory_bundle_item(memory, max_chars=900) for memory in hippocampus_entries],
            "agent_feedback": [memory_bundle_item(memory, max_chars=600) for memory in feedback],
            "pinned": [memory_bundle_item(memory, max_chars=700) for memory in pinned],
            "summary": summary,
            "warnings": warnings,
        }

    feedback_candidates = [memory for memory in visible if str(memory.get("kind", "")) == "agent_feedback"]
    scored_feedback: list[tuple[float, dict[str, Any]]] = []
    for memory in feedback_candidates:
        score = 0.0
        if agent_id and str(memory.get("agent_id", "")).strip() == agent_id:
            score += 3.0
        if role and str(memory.get("role", "")).strip() == role:
            score += 2.0
        if domain and str(memory.get("domain", "")).strip() == domain:
            score += 2.0
        score += rank_against_query(memory, task, salience_module) if task else 0.0
        if score > 0:
            scored_feedback.append((score, memory))
    scored_feedback.sort(key=lambda item: (item[0], str(item[1].get("created_at", ""))), reverse=True)
    feedback = [memory for _, memory in scored_feedback[:max_feedback]]

    hippocampus_candidates = [
        memory
        for memory in visible
        if str(memory.get("kind", "")) == "hippocampus_entry"
        and (
            not domain
            or str(memory.get("domain", "")).strip() == domain
            or str(memory.get("domain", "")).strip() == ""
        )
    ]
    scored_hippocampus = select_memories_by_query(hippocampus_candidates, task, max_hippocampus, salience_module)
    hippocampus_entries = [memory for _, memory in scored_hippocampus]

    block_candidates = [memory for memory in visible if str(memory.get("kind", "")) == "context_block"]
    if domain:
        block_candidates = [memory for memory in block_candidates if str(memory.get("domain", "")).strip() in {"", domain}]
    scored_blocks = select_memories_by_query(block_candidates, task, max_blocks, salience_module)
    context_blocks = [memory for _, memory in scored_blocks]

    recent_logs: list[dict[str, Any]] = []
    if include_recent_logs:
        recent_logs = [
            memory
            for memory in visible
            if str(memory.get("kind", "")) == "interaction_log"
            and (
                (agent_id and str(memory.get("agent_id", "")).strip() == agent_id)
                or (role and str(memory.get("role", "")).strip() == role)
            )
        ]
        recent_logs.sort(key=lambda memory: str(memory.get("created_at", "")), reverse=True)
        recent_logs = recent_logs[:recent_logs_limit]

    warnings = []
    if not feedback:
        warnings.append("No scoped agent feedback matched the provided filters.")
    summary = (
        f"agent context: {len(feedback)} feedback, {len(hippocampus_entries)} hippocampus entries, "
        f"{len(context_blocks)} context blocks."
    )
    return summary, {
        "mode": "agent",
        "agent_id": agent_id,
        "role": role,
        "domain": domain,
        "task": task,
        "agent_feedback": [memory_bundle_item(memory, max_chars=600) for memory in feedback],
        "hippocampus_entries": [memory_bundle_item(memory, max_chars=900) for memory in hippocampus_entries],
        "context_blocks": [memory_bundle_item(memory, max_chars=1200) for memory in context_blocks],
        "recent_logs": [memory_bundle_item(memory, max_chars=500) for memory in recent_logs],
        "warnings": warnings,
    }


def _apply_recall_output_caps(structured: dict[str, Any]) -> dict[str, Any]:
    total_cap = max_total_chars()
    used = 0
    warnings: list[str] = list(structured.get("warnings", [])) if isinstance(structured.get("warnings"), list) else []
    ordered_keys = ("recent_logs", "context_blocks", "hippocampus_entries", "agent_feedback", "pinned")
    for key in ordered_keys:
        section = structured.get(key)
        if not isinstance(section, list):
            continue
        kept: list[dict[str, Any]] = []
        for item in section:
            if not isinstance(item, dict):
                continue
            preview = str(item.get("text_preview", ""))
            if used + len(preview) > total_cap and kept:
                warnings.append("recall output capped by max_total_chars")
                break
            if used + len(preview) > total_cap and not kept:
                truncated = preview[: max(0, total_cap - used)]
                item = dict(item)
                item["text_preview"] = truncated
                item["truncated"] = len(preview) > len(truncated)
                kept.append(item)
                used = total_cap
                warnings.append("recall output capped by max_total_chars")
                break
            kept.append(item)
            used += len(preview)
        structured[key] = kept
        if used >= total_cap:
            break
    structured["warnings"] = warnings
    return structured


def memory_recall(args: dict[str, Any]) -> dict[str, Any]:
    mode = normalize_optional_string(args.get("mode")) or "startup"
    if mode not in {"startup", "agent"}:
        return tool_error("mode must be one of: startup, agent")

    agent_id = normalize_optional_string(args.get("agent_id"))
    role = normalize_optional_string(args.get("role"))
    domain = normalize_optional_string(args.get("domain"))
    task = str(args.get("task", "")).strip()
    query = str(args.get("query", "")).strip()

    if mode == "startup":
        role = role or "coordinator"
        recent_logs_limit = max(1, min(int(args.get("recent_logs", 20)), 100))
        max_blocks = max(1, min(int(args.get("max_blocks", 5)), 20))
        max_hippocampus = max(1, min(int(args.get("max_hippocampus", 8)), 20))
        max_feedback = max(1, min(int(args.get("max_feedback", 5)), 30))
        include_pinned = parse_bool(args.get("include_pinned"), default=True)
        include_recent_logs = False
    else:
        recent_logs_limit = max(1, min(int(args.get("recent_logs", 20)), 100))
        max_blocks = max(1, min(int(args.get("max_context_blocks", 5)), 20))
        max_hippocampus = max(1, min(int(args.get("max_hippocampus", 8)), 20))
        max_feedback = max(1, min(int(args.get("max_feedback", 10)), 30))
        include_pinned = False
        include_recent_logs = parse_bool(args.get("include_recent_logs"), default=False)
        if not query:
            query = task

    salience, _ = load_optional_agent_salience()
    summary, structured = _build_recall_bundle(
        load_store(),
        mode=mode,
        agent_id=agent_id,
        role=role,
        domain=domain,
        task=task,
        query=query,
        recent_logs_limit=recent_logs_limit,
        max_blocks=max_blocks,
        max_hippocampus=max_hippocampus,
        max_feedback=max_feedback,
        include_pinned=include_pinned,
        include_recent_logs=include_recent_logs,
        salience_module=salience,
    )
    structured = _apply_recall_output_caps(structured)
    return text_result(summary, structured)


def _compact_logs_maintenance(args: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    older_than_count = max(1, min(int(args.get("older_than_count", 20)), 500))
    agent_id = normalize_optional_string(args.get("agent_id"))
    role = normalize_optional_string(args.get("role"))
    max_logs = max(1, min(int(args.get("max_logs", 50)), 200))

    store = load_store()
    candidates = [
        memory
        for memory in store.get("memories", [])
        if isinstance(memory, dict)
        and str(memory.get("kind", "")) == "interaction_log"
        and visible_memory(memory, False, False)
        and (agent_id is None or str(memory.get("agent_id", "")).strip() == agent_id)
        and (role is None or str(memory.get("role", "")).strip() == role)
    ]
    candidates.sort(key=lambda memory: str(memory.get("created_at", "")), reverse=True)
    older = candidates[older_than_count:]
    selected = older[:max_logs]
    selected = sorted(selected, key=lambda memory: str(memory.get("created_at", "")))

    if not selected:
        return text_result(
            "No interaction logs eligible for compaction.",
            {
                "action": "compact_logs",
                "dry_run": dry_run,
                "selected_count": 0,
                "older_than_count": older_than_count,
                "candidate": None,
            },
        )

    linked_ids = [str(memory.get("id")) for memory in selected if memory.get("id")]
    lines = ["Compacted interaction log summary:"]
    for memory in selected[:20]:
        stamp = str(memory.get("created_at", ""))
        preview = memory_preview(memory, max_chars=220)
        lines.append(f"- {stamp}: {preview}")
    if len(selected) > 20:
        lines.append(f"- ... {len(selected) - 20} additional logs omitted")
    candidate_text = "\n".join(lines)
    candidate_metadata = {
        "title": f"Compacted interaction logs ({len(selected)})",
        "compacted_count": len(selected),
        "older_than_count": older_than_count,
    }
    candidate = {
        "kind": "context_block",
        "text": candidate_text,
        "linked_ids": linked_ids,
        "agent_id": agent_id,
        "role": role,
        "retention": "durable",
        "authority": "medium",
        "metadata": candidate_metadata,
    }
    if dry_run:
        return text_result(
            "Generated compaction candidate (dry_run=true).",
            {
                "action": "compact_logs",
                "dry_run": True,
                "selected_count": len(selected),
                "candidate": candidate,
            },
        )

    payload = {
        "body": candidate_text,
        "linked_ids": linked_ids,
        "agent_id": agent_id,
        "role": role,
        "metadata": candidate_metadata,
    }
    recorded = record_memory(
        {
            "kind": "context_block",
            "body": payload["body"],
            "linked_ids": payload["linked_ids"],
            "agent_id": payload["agent_id"],
            "role": payload["role"],
            "metadata": payload["metadata"],
        }
    )
    if recorded.get("isError"):
        return recorded
    structured = recorded.get("structuredContent", {})
    block_id = str(
        structured.get("id")
        or (structured.get("memory", {}) if isinstance(structured.get("memory"), dict) else {}).get("id")
        or ""
    )

    try:
        with MemoryFileLock(store_lock_path()):
            writable_store = load_store()
            for memory in writable_store.get("memories", []):
                if str(memory.get("id")) not in linked_ids:
                    continue
                metadata = normalize_metadata(memory.get("metadata"))
                metadata["compacted"] = True
                metadata["compacted_into"] = block_id
                memory["metadata"] = metadata
                memory["updated_at"] = now_iso()
            save_store(writable_store)
    except LockTimeout as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")

    return text_result(
        f"Compacted {len(selected)} interaction logs into context block {block_id}.",
        {
            "action": "compact_logs",
            "dry_run": False,
            "selected_count": len(selected),
            "block_id": block_id,
            "candidate": candidate,
        },
    )


DEFINITION_PATTERNS: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    ".py": [
        ("def", re.compile(r"^\s*(?:async\s+)?def\s+(?P<name>[A-Za-z_]\w*)\b")),
        ("class", re.compile(r"^\s*class\s+(?P<name>[A-Za-z_]\w*)\b")),
    ],
    ".js": [],
    ".jsx": [],
    ".mjs": [],
    ".cjs": [],
    ".ts": [],
    ".tsx": [],
    ".go": [
        ("func", re.compile(r"^\s*func\s+(?:\([^)]+\)\s*)?(?P<name>[A-Za-z_]\w*)\s*\(")),
        ("type", re.compile(r"^\s*type\s+(?P<name>[A-Za-z_]\w*)\b")),
    ],
    ".rs": [
        ("fn", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(?P<name>[A-Za-z_]\w*)\b")),
        ("type", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|type)\s+(?P<name>[A-Za-z_]\w*)\b")),
    ],
    ".java": [
        ("type", re.compile(r"\b(?:class|interface|enum|record)\s+(?P<name>[A-Za-z_]\w*)\b")),
        ("method", re.compile(r"^\s*(?:public|private|protected|static|final|synchronized|\s)+[\w<>\[\], ?]+\s+(?P<name>[A-Za-z_]\w*)\s*\(")),
    ],
    ".cs": [
        ("type", re.compile(r"\b(?:class|interface|struct|enum|record)\s+(?P<name>[A-Za-z_]\w*)\b")),
        ("method", re.compile(r"^\s*(?:public|private|protected|internal|static|async|virtual|override|sealed|\s)+[\w<>\[\], ?]+\s+(?P<name>[A-Za-z_]\w*)\s*\(")),
    ],
    ".php": [
        ("function", re.compile(r"^\s*(?:public|private|protected|static|\s)*function\s+(?P<name>[A-Za-z_]\w*)\b")),
        ("type", re.compile(r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait)\s+(?P<name>[A-Za-z_]\w*)\b")),
    ],
    ".rb": [
        ("def", re.compile(r"^\s*def\s+(?P<name>[A-Za-z_]\w*[!?=]?)\b")),
        ("type", re.compile(r"^\s*(?:class|module)\s+(?P<name>[A-Za-z_]\w*)\b")),
    ],
}

JS_PATTERNS = [
    ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\b")),
    ("class", re.compile(r"^\s*(?:export\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)\b")),
    ("const", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=")),
]
TS_EXTRA_PATTERNS = [
    ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+(?P<name>[A-Za-z_$][\w$]*)\b")),
    ("type", re.compile(r"^\s*(?:export\s+)?type\s+(?P<name>[A-Za-z_$][\w$]*)\b")),
    ("enum", re.compile(r"^\s*(?:export\s+)?enum\s+(?P<name>[A-Za-z_$][\w$]*)\b")),
]
for _ext in (".js", ".jsx", ".mjs", ".cjs"):
    DEFINITION_PATTERNS[_ext] = JS_PATTERNS
for _ext in (".ts", ".tsx"):
    DEFINITION_PATTERNS[_ext] = TS_EXTRA_PATTERNS + JS_PATTERNS
del _ext

CODE_FALLBACK_EXTS = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".kt",
    ".kts",
    ".swift",
    ".scala",
    ".clj",
    ".cljs",
    ".cljc",
    ".ex",
    ".exs",
    ".lua",
    ".sh",
    ".bash",
    ".zsh",
    ".sql",
    ".sas",
}
FALLBACK_MAX_BYTES = 256 * 1024


def should_skip_dir(dirname: str) -> bool:
    return dirname in SKIP_DIRS or dirname.startswith(".")


def iter_workspace_files(root: Path) -> tuple[list[Path], dict[str, Any]]:
    files: list[Path] = []
    warnings: list[str] = []
    skipped_files = 0
    scanned_bytes = 0
    max_files = max_files_scanned()
    max_total = max_total_bytes()
    max_single = max_file_bytes()
    if not root.exists():
        return files, {"warnings": warnings, "skipped_files": skipped_files, "scanned_bytes": scanned_bytes}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not should_skip_dir(d))
        current = Path(dirpath)
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            path = current / filename
            ext = path.suffix.lower()
            if ext not in DEFINITION_PATTERNS and ext not in CODE_FALLBACK_EXTS:
                continue
            if len(files) >= max_files:
                warnings.append(f"max files scanned reached ({max_files})")
                return files, {
                    "warnings": warnings,
                    "skipped_files": skipped_files,
                    "scanned_bytes": scanned_bytes,
                }
            try:
                size = path.stat().st_size
            except OSError:
                skipped_files += 1
                continue
            if size > max_single:
                skipped_files += 1
                warnings.append(f"skipped {path.name}: file exceeds MNEMO_MAX_FILE_BYTES")
                continue
            if scanned_bytes + size > max_total:
                skipped_files += 1
                warnings.append(f"max total bytes reached ({max_total})")
                return files, {
                    "warnings": warnings,
                    "skipped_files": skipped_files,
                    "scanned_bytes": scanned_bytes,
                }
            scanned_bytes += size
            files.append(path)
    return files, {"warnings": warnings, "skipped_files": skipped_files, "scanned_bytes": scanned_bytes}


def signature_for_files(root: Path, files: list[Path]) -> str:
    h = hashlib.sha1()
    for path in sorted(files, key=lambda p: str(p)):
        try:
            stat = path.stat()
        except OSError:
            continue
        rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()
        h.update(rel.encode("utf-8", "ignore"))
        h.update(str(stat.st_mtime_ns).encode("ascii"))
        h.update(str(stat.st_size).encode("ascii"))
    return h.hexdigest()


def build_symbol_index(root: Path, files: list[Path]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    unknown_lines: list[dict[str, Any]] = []
    indexed = 0
    for path in files:
        ext = path.suffix.lower()
        if ext in DEFINITION_PATTERNS:
            patterns = DEFINITION_PATTERNS[ext]
        elif ext in CODE_FALLBACK_EXTS:
            patterns = []
        else:
            continue
        if not patterns:
            try:
                if path.stat().st_size > FALLBACK_MAX_BYTES:
                    continue
            except OSError:
                continue
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                lines = list(f)
        except OSError:
            continue
        indexed += 1
        rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()
        if patterns:
            for line_no, line in enumerate(lines, start=1):
                for kind, pattern in patterns:
                    match = pattern.search(line)
                    if not match:
                        continue
                    entries.append(
                        {
                            "name": match.group("name"),
                            "file": rel,
                            "line": line_no,
                            "kind": kind,
                            "preview": line.strip()[:160],
                        }
                    )
        else:
            for line_no, line in enumerate(lines, start=1):
                preview = line.strip()
                if preview:
                    unknown_lines.append(
                        {
                            "file": rel,
                            "line": line_no,
                            "kind": "match",
                            "preview": preview[:160],
                        }
                    )
    return {"entries": entries, "unknown_lines": unknown_lines, "indexed_files": indexed}


def get_symbol_index(root: Path) -> tuple[dict[str, Any], bool]:
    key = str(root.resolve() if root.exists() else root)
    ttl = symbol_ttl_seconds()
    cached = _SYMBOL_CACHE.get(key)
    now = time.monotonic()
    if cached is not None and ttl > 0 and now - cached[0] < ttl:
        return cached[2], True

    files, scan_info = iter_workspace_files(root)
    signature = signature_for_files(root, files)
    if cached is not None and ttl > 0 and cached[1] == signature:
        index = dict(cached[2])
        index.update(scan_info)
        _SYMBOL_CACHE[key] = (time.monotonic(), signature, index)
        return index, True

    index = build_symbol_index(root, files)
    index.update(scan_info)
    _SYMBOL_CACHE[key] = (time.monotonic(), signature, index)
    return index, False


def lookup_symbol(args: dict[str, Any]) -> dict[str, Any]:
    name = str(args.get("name", "")).strip()
    if not name:
        return tool_error("name is required")
    limit = int(args.get("limit", 10))
    limit = max(1, min(limit, 50))
    case_sensitive = parse_bool(args.get("case_sensitive"), default=False)
    root = workspace_root()
    try:
        index, cache_hit = get_symbol_index(root)
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")

    matches: list[dict[str, Any]] = []
    wanted = name if case_sensitive else name.lower()
    for entry in index["entries"]:
        candidate = str(entry.get("name", ""))
        comparable = candidate if case_sensitive else candidate.lower()
        if comparable == wanted:
            item = {k: v for k, v in entry.items() if k != "name"}
            matches.append(item)
    if len(matches) < limit:
        flags = 0 if case_sensitive else re.IGNORECASE
        word_re = re.compile(rf"\b{re.escape(name)}\b", flags)
        for line in index["unknown_lines"]:
            if word_re.search(str(line.get("preview", ""))):
                matches.append(dict(line))
                if len(matches) >= limit:
                    break
    matches = sorted(matches, key=lambda item: (str(item["file"]), int(item["line"])))[:limit]

    if matches:
        lines = [f"Symbol matches for {name}:"]
        for item in matches:
            lines.append(f"- {item['file']}:{item['line']} ({item['kind']}) {item['preview']}")
    else:
        lines = [f"No symbol matches found for {name}."]
    return text_result(
        "\n".join(lines),
        {
            "matches": matches,
            "indexed_files": index["indexed_files"],
            "cache_hit": cache_hit,
            "warnings": index.get("warnings", []),
            "skipped_files": index.get("skipped_files", 0),
            "scanned_bytes": index.get("scanned_bytes", 0),
        },
    )


def _iso_from_epoch(timestamp: float | int | None) -> str | None:
    if timestamp is None:
        return None
    try:
        dt = datetime.fromtimestamp(float(timestamp), timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _latest_memory_id(memories: list[dict[str, Any]]) -> str | None:
    if not memories:
        return None
    latest_index = max(range(len(memories)), key=lambda index: (timestamp_key(memories[index]), index))
    latest = memories[latest_index]
    memory_id = str(latest.get("id", "")).strip()
    return memory_id or None


def _kind_counts(memories: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for memory in memories:
        kind = str(memory.get("kind", "")).strip() or "note"
        counts[kind] = counts.get(kind, 0) + 1
    return {kind: counts[kind] for kind in sorted(counts)}


def _count_by_field(memories: list[dict[str, Any]], field: str, *, default_label: str = "unset") -> dict[str, int]:
    counts: dict[str, int] = {}
    for memory in memories:
        raw = normalize_optional_string(memory.get(field))
        key = raw if raw else default_label
        counts[key] = counts.get(key, 0) + 1
    return {name: counts[name] for name in sorted(counts)}


def _oldest_newest(memories: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not memories:
        return None, None
    ordered = sorted(memories, key=lambda memory: (timestamp_key(memory), str(memory.get("id", ""))))
    oldest_raw = ordered[0]
    newest_raw = ordered[-1]

    def _summary(memory: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(memory.get("id", "")),
            "kind": str(memory.get("kind", "")),
            "created_at": str(memory.get("created_at", "")),
            "updated_at": normalize_optional_string(memory.get("updated_at")),
        }

    return _summary(oldest_raw), _summary(newest_raw)


def _compaction_counts(memories: list[dict[str, Any]]) -> tuple[int, int]:
    compacted = 0
    uncompacted = 0
    for memory in memories:
        if str(memory.get("kind", "")) != "interaction_log":
            continue
        if not is_active(memory):
            continue
        metadata = normalize_metadata(memory.get("metadata"))
        if metadata.get("compacted") or metadata.get("compacted_into"):
            compacted += 1
        else:
            uncompacted += 1
    return compacted, uncompacted


def _list_export_files() -> dict[str, dict[str, Any]]:
    files = {
        "memory_jsonl": export_root() / "memory.jsonl",
        "memory_json": export_root() / "memory.json",
        "hippocampus_markdown": export_root() / "hippocampus.md",
        "agent_feedback_markdown": export_root() / "agent_feedback.md",
        "startup_context_markdown": export_root() / "startup_context_latest.md",
    }
    payload: dict[str, dict[str, Any]] = {}
    for name, path in files.items():
        exists = path.exists()
        size = 0
        if exists:
            try:
                size = int(path.stat().st_size)
            except OSError:
                size = 0
        payload[name] = {
            "path": str(path.resolve()),
            "exists": exists,
            "size_bytes": size,
        }
    return payload


def _sqlite_fts_flag() -> bool:
    if store_backend() != "sqlite":
        return False
    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            _sqlite_bootstrap_if_needed(conn)
            meta_value = _sqlite_get_meta(conn, "fts_available")
            if meta_value is not None:
                return str(meta_value).strip() == "1"
            available = _sqlite_fts_available(conn)
            _sqlite_set_meta(conn, "fts_available", "1" if available else "0")
            return available
    except Exception:
        return False


def _salience_source(module: Any) -> str | None:
    module_file = str(getattr(module, "__file__", "") or "")
    if not module_file:
        return None
    home_raw = os.environ.get("AGENT_SALIENCE_HOME", "").strip()
    if not home_raw:
        return "sys.path"
    try:
        home = Path(home_raw).expanduser().resolve()
        module_path = Path(module_file).resolve()
    except Exception:
        return "sys.path"
    try:
        module_path.relative_to(home)
        return "AGENT_SALIENCE_HOME"
    except ValueError:
        return "sys.path"


def mnemo_doctor(args: dict[str, Any]) -> dict[str, Any]:
    del args
    backend = store_backend()
    sqlite_file = sqlite_path().resolve()
    mem_file = memory_path().resolve()
    events_file = events_log_path().resolve()
    memory_archive = archived_path().resolve()
    queries_archive = query_archive_path().resolve()
    events_archive = events_archive_path().resolve()
    warnings: list[str] = []

    try:
        store = load_store()
    except Exception:
        store = {"version": 1, "memories": []}
        warnings.append("memory store is unreadable; check local state files")

    memories = [memory for memory in store.get("memories", []) if isinstance(memory, dict)]
    memory_count = len(memories)
    count_by_kind = _kind_counts(memories)
    latest_id = _latest_memory_id(memories)

    mem_exists = mem_file.exists()
    mem_size = 0
    mem_mtime_iso: str | None = None
    if mem_exists:
        try:
            stat = mem_file.stat()
            mem_size = int(stat.st_size)
            mem_mtime_iso = _iso_from_epoch(stat.st_mtime)
        except OSError:
            mem_size = 0

    sqlite_exists = sqlite_file.exists()
    sqlite_size = 0
    if sqlite_exists:
        try:
            sqlite_size = int(sqlite_file.stat().st_size)
        except OSError:
            sqlite_size = 0

    if backend == "json" and mem_exists and mem_size < 100:
        warnings.append("memory file < 100 bytes; possibly empty")
    if backend == "json" and mem_mtime_iso is None:
        warnings.append("memory file timestamp unavailable")
    elif backend == "json":
        try:
            mtime_dt = datetime.fromisoformat(mem_mtime_iso.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - mtime_dt).total_seconds() / 3600.0
            if age_hours > 24:
                warnings.append("no writes detected in the last 24 hours")
        except ValueError:
            pass

    events_exists = events_file.exists()
    events_size = 0
    if events_exists:
        try:
            events_size = int(events_file.stat().st_size)
        except OSError:
            events_size = 0
    rows = read_event_rows(include_archive=False) if event_logging_enabled() else []
    rows.sort(key=lambda row: (str(row.get("ts", "")), str(row.get("id", ""))))
    last_event = rows[-1] if rows else {}
    last_event_iso = str(last_event.get("ts", "")).strip() or None
    last_event_kind = str(last_event.get("event", "")).strip() or None

    if not event_logging_enabled():
        warnings.append("MNEMO_LOG_EVENTS=0; event history is not being recorded")
    elif backend == "json" and not events_exists:
        warnings.append("events log file not found yet; no events recorded")

    drift = memory_drift_compute(store=store)
    if float(drift.get("value", 0.0)) >= 0.7:
        warnings.append("high memory drift detected; review durable/pinned guidance")

    salience_module, _reason = load_optional_agent_salience()
    salience_loaded = salience_module is not None
    if not salience_loaded:
        warnings.append("agent-salience not loaded; salience checks unavailable")
    salience_payload = {
        "loaded": salience_loaded,
        "version": str(getattr(salience_module, "__version__", "")) if salience_loaded else None,
        "source": _salience_source(salience_module) if salience_loaded else None,
    }

    profile = mcp_profile()
    visible = exposed_tools(profile)
    available_actions = sorted(GATEWAY_ACTIONS)
    available = {str(tool.get("name", "")) for tool in visible}
    structured_available = GATEWAY_TOOL_NAME in available

    count_by_authority = _count_by_field(memories, "authority")
    count_by_retention = _count_by_field(memories, "retention")
    deleted_count = sum(1 for memory in memories if not is_active(memory))
    compacted_logs_count, uncompacted_logs_count = _compaction_counts(memories)
    oldest_memory, newest_memory = _oldest_newest(memories)
    export_files = _list_export_files()
    fts_available = _sqlite_fts_flag() if backend == "sqlite" else False
    search_backend = (
        "sqlite_fts5"
        if backend == "sqlite" and fts_available
        else ("sqlite_lexical" if backend == "sqlite" else "json_lexical")
    )

    if backend == "sqlite" and not sqlite_exists:
        warnings.append("sqlite database file does not exist yet")
    if backend == "sqlite" and not fts_available:
        warnings.append("FTS unavailable, using simple search")
    if uncompacted_logs_count >= 40:
        warnings.append("many uncompacted interaction logs; consider compact_logs maintenance")
    if not any(info.get("exists") for info in export_files.values()):
        warnings.append("no export files found under state/mnemo/exports")
    if backend == "sqlite" and sqlite_size > 5_000_000 and not any(info.get("exists") for info in export_files.values()):
        warnings.append("SQLite file is large but no recent exports were found")
    if backend == "sqlite" and (
        events_file.exists() or query_log_path().exists() or events_archive.exists() or queries_archive.exists()
    ):
        warnings.append("legacy JSONL logs exist; SQLite events table is authoritative in sqlite mode")

    recommendations: list[str] = []
    if uncompacted_logs_count >= 40:
        recommendations.append(
            "Run mnemo_maintenance with action='compact_logs' dry_run=true, then dry_run=false if the candidate looks correct."
        )
    if not any(info.get("exists") for info in export_files.values()):
        recommendations.append(
            "Run mnemo_export with format='jsonl' and format='hippocampus_markdown' to produce portable readable exports."
        )
    if memory_count == 0:
        recommendations.append("Record a starter memory with mnemo_record to seed project context.")
    if backend == "sqlite" and not fts_available:
        recommendations.append("SQLite FTS5 is unavailable; lexical search is active.")

    memory_file_payload = {
        "path": str(mem_file),
        "exists": mem_exists,
        "size_bytes": mem_size,
        "memory_count": memory_count,
        "kinds": count_by_kind,
        "last_write_iso": mem_mtime_iso,
        "last_memory_id": latest_id,
    }
    events_payload = {
        "path": str(events_file),
        "exists": events_exists,
        "size_bytes": events_size,
        "last_event_iso": last_event_iso,
        "last_event_kind": last_event_kind,
    }
    archive_payload = {
        "memory_archive_path": str(memory_archive),
        "memory_archive_exists": memory_archive.exists(),
        "queries_archive_exists": queries_archive.exists(),
        "events_archive_exists": events_archive.exists(),
    }

    kind_summary = ", ".join(f"{count} {kind}" for kind, count in memory_file_payload["kinds"].items())
    if not kind_summary:
        kind_summary = "none"
    warning_summary = "none" if not warnings else "; ".join(warnings)
    drift_value = float(drift.get("value", 0.0))
    drift_interp = str(drift.get("interpretation", "low"))
    salience_text = (
        f"loaded (agent-salience {salience_payload['version']})"
        if salience_loaded
        else "not loaded"
    )
    backend_exists = sqlite_exists if backend == "sqlite" else mem_exists
    backend_file_name = "mnemo.sqlite" if backend == "sqlite" else "memory.json"
    backend_size = sqlite_size if backend == "sqlite" else memory_file_payload["size_bytes"]
    summary_lines = [
        f"Mnemo {SERVER_VERSION} - {backend_file_name} {'exists' if backend_exists else 'missing'} ({backend_size} bytes, {memory_file_payload['memory_count']} memories)",
        f"Last write: {memory_file_payload['last_write_iso']}  Last id: {memory_file_payload['last_memory_id']}",
        f"Kinds: {kind_summary}",
        f"Drift: {drift_value:.2f} ({drift_interp})  Salience: {salience_text}  Search: {search_backend}",
        f"Warnings: {warning_summary}",
    ]

    payload = {
        "server_name": SERVER_NAME,
        "version": SERVER_VERSION,
        "package_file": str(Path(__file__).resolve()),
        "python": sys.version,
        "executable": sys.executable,
        "workspace_root": str(workspace_root().resolve()),
        "public_tool_prefix": "mnemo",
        "mcp_profile": profile,
        "exposed_tool_count": len(visible),
        "public_tool_count": len(visible),
        "gateway": True,
        "gateway_tool": GATEWAY_TOOL_NAME,
        "available_actions": available_actions,
        "expected_core_tools": [GATEWAY_TOOL_NAME],
        "structured_memory_tools_available": structured_available,
        "backend": backend,
        "sqlite_file": str(sqlite_file),
        "sqlite_file_exists": sqlite_exists,
        "sqlite_size_bytes": sqlite_size,
        "memory_json_exists": mem_exists,
        "memory_count": memory_count,
        "count_by_kind": count_by_kind,
        "count_by_authority": count_by_authority,
        "count_by_retention": count_by_retention,
        "deleted_count": deleted_count,
        "compacted_logs_count": compacted_logs_count,
        "uncompacted_interaction_logs_count": uncompacted_logs_count,
        "oldest_memory": oldest_memory,
        "newest_memory": newest_memory,
        "export_files": export_files,
        "fts_available": fts_available,
        "search_backend": search_backend,
        "memory_file": memory_file_payload,
        "events_log": events_payload,
        "archive": archive_payload,
        "drift": drift,
        "salience": salience_payload,
        "warnings": warnings,
        "recommendations": recommendations,
    }
    return text_result("\n".join(summary_lines), payload)


GATEWAY_ACTIONS: dict[str, Any] = {
    "doctor": mnemo_doctor,
    "search": search_memories,
    "salience_check": memory_salience_check,
    "record": record_memory,
    "link": memory_link,
    "recall": memory_recall,
    "get": memory_get,
    "export": memory_export,
    "update": update_memory,
    "delete": delete_memory,
    "recent": recent_memories,
    "compact_context": compact_context,
    "inspect": memory_inspect,
    "maintenance": memory_maintenance,
    "lookup_symbol": lookup_symbol,
}


def gateway_error(error: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    structured = {"error": error, "message": message}
    if details:
        structured.update(details)
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "isError": True,
        "structuredContent": structured,
    }


def mnemo_gateway(args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch the single public Mnemo MCP tool to an internal action."""
    action = normalize_optional_string(args.get("action")) if isinstance(args, dict) else None
    params = args.get("params", {}) if isinstance(args, dict) else {}
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return gateway_error(
            "invalid_params",
            "Mnemo gateway params must be an object when provided.",
            {"available_actions": sorted(GATEWAY_ACTIONS)},
        )
    if not action:
        return gateway_error(
            "missing_action",
            "Mnemo gateway requires an action.",
            {"available_actions": sorted(GATEWAY_ACTIONS)},
        )
    handler = GATEWAY_ACTIONS.get(action)
    if handler is None:
        return gateway_error(
            "unknown_action",
            f"Unknown Mnemo action: {action}",
            {"action": action, "available_actions": sorted(GATEWAY_ACTIONS)},
        )
    return handler(params)


TOOLS = [
    {
        "name": GATEWAY_TOOL_NAME,
        "title": "Mnemo Project Memory Gateway",
        "description": (
            "Mnemo project-memory gateway. Use this for portable project memory, startup recall, "
            "hippocampus entries, agent feedback, exports, maintenance, salience diagnostics, "
            "and source symbol lookup. This is not Copilot native memory. Supported actions: "
            + ", ".join(sorted(GATEWAY_ACTIONS))
            + ". Pass action plus optional params."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Required action name. Supported actions are listed in the tool description.",
                },
                "params": {
                    "type": "object",
                    "description": "Optional action parameters. Omit when not needed.",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
]


_TOOL_FIELD_CONSTRAINT_NOTES: dict[str, dict[str, str]] = {
    "mnemo_search": {
        "limit": "Range 1-20. When omitted, 5 is used.",
        "include_deleted": "When omitted, false is used.",
        "include_superseded": "When omitted, false is used.",
        "max_tokens": "Range 1-100000 when provided.",
    },
    "mnemo_salience_check": {
        "limit": "Range 1-50. When omitted, 5 is used.",
        "include_deleted": "When omitted, false is used.",
        "include_superseded": "When omitted, false is used.",
        "threshold": "When omitted, 0.70 is used. Range 0.0-1.0.",
    },
    "mnemo_record": {
        "kind": "When omitted, note is used.",
        "tags": "When omitted, an empty list is used.",
        "references": "When omitted, an empty list is used.",
        "linked_ids": "When omitted, an empty list is used.",
        "evidence_ids": "When omitted, an empty list is used.",
        "pinned": "When omitted, false is used.",
    },
    "mnemo_link": {
        "bidirectional": "When omitted, false is used.",
    },
    "mnemo_recall": {
        "recent_logs": "Range 1-100. When omitted, 20 is used.",
        "max_blocks": "Range 1-20. When omitted, 5 is used for startup mode.",
        "max_context_blocks": "Range 1-20. When omitted, 5 is used for agent mode.",
        "max_hippocampus": "Range 1-20. When omitted, 8 is used.",
        "max_feedback": "Range 1-30. When omitted: 5 in startup mode, 10 in agent mode.",
        "include_pinned": "When omitted, true is used in startup mode.",
        "include_recent_logs": "When omitted, false is used in agent mode.",
    },
    "mnemo_recent": {
        "limit": "Range 1-50. When omitted, 10 is used.",
        "include_deleted": "When omitted, false is used.",
        "include_superseded": "When omitted, false is used.",
    },
    "mnemo_compact_context": {
        "limit": "Range 1-20. When omitted, 8 is used.",
        "include_deleted": "When omitted, false is used.",
        "include_superseded": "When omitted, false is used.",
        "max_tokens": "Range 1-100000 when provided.",
    },
    "mnemo_inspect": {
        "mode": "Allowed values: history, related, both. When omitted, both is used.",
        "limit": "Range 1-200. When omitted, 50 is used.",
        "depth": "Range 1-3. When omitted, 1 is used.",
        "include_deleted": "When omitted, false is used.",
        "include_superseded": "When omitted, false is used.",
        "include_archive": "When omitted, false is used.",
    },
    "mnemo_maintenance": {
        "action": "Allowed values: compact_logs, consolidate, import_json.",
        "older_than_count": "compact_logs: range 1-500. When omitted, 20 is used.",
        "max_logs": "compact_logs: range 1-200. When omitted, 50 is used.",
        "threshold": "Range 0.5-1.0. When omitted, env fallback 0.7 is used.",
        "dry_run": "When omitted, true is used.",
    },
    "mnemo_export": {
        "format": "Allowed values: jsonl, json, markdown, hippocampus_markdown, agent_feedback_markdown, startup_context_markdown.",
        "max_records": "Range 1-5000. When omitted, 500 is used.",
        "include_deleted": "When omitted, false is used.",
    },
    "mnemo_lookup_symbol": {
        "limit": "Range 1-50. When omitted, 10 is used.",
        "case_sensitive": "When omitted, false is used.",
    },
}


def _apply_tool_constraint_notes() -> None:
    for tool in TOOLS:
        tool_name = str(tool.get("name", ""))
        field_notes = _TOOL_FIELD_CONSTRAINT_NOTES.get(tool_name)
        if not field_notes:
            continue
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict):
            continue
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            continue
        for field_name, note in field_notes.items():
            field_schema = properties.get(field_name)
            if isinstance(field_schema, dict):
                _append_description_note(field_schema, note)


_apply_tool_constraint_notes()


_TOOL_DESC_PREFIX = (
    "Mnemo project-memory tool. Use this for portable project memory, not Copilot native memory. "
)
for _tool in TOOLS:
    _desc = str(_tool.get("description", ""))
    if _desc and not _desc.startswith(_TOOL_DESC_PREFIX):
        _tool["description"] = _TOOL_DESC_PREFIX + _desc


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def ok(request_id: Any, result: dict[str, Any]) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def rpc_error(request_id: Any, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def handle_request(message: dict[str, Any]) -> None:
    global _SHOULD_EXIT

    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if request_id is None:
        # MCP clients commonly send notifications/initialized after initialize.
        # Notifications do not receive JSON-RPC responses.
        return

    if method == "initialize":
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        ok(
            request_id,
            {
                "protocolVersion": requested or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "title": SERVER_TITLE,
                    "version": SERVER_VERSION,
                },
                "instructions": (
                    "Use the single mnemo gateway tool with action plus optional params. "
                    "Common actions: doctor, search, record, recall, get, link, export, "
                    "compact_context, inspect, maintenance, salience_check, update, delete, "
                    "recent, lookup_symbol. Do not look for individual mnemo_* tools; "
                    "they are gateway actions now."
                ),
            },
        )
        return

    if method == "shutdown":
        ok(request_id, {})
        _SHOULD_EXIT = True
        return

    if method == "tools/list":
        ok(request_id, {"tools": copilot_safe_tools()})
        return

    if method == "tools/call":
        if not isinstance(params, dict):
            rpc_error(request_id, -32602, "Invalid tools/call params")
            return
        name = str(params.get("name", ""))
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            rpc_error(request_id, -32602, "Tool arguments must be an object")
            return
        handlers = {
            GATEWAY_TOOL_NAME: mnemo_gateway,
        }
        handler = handlers.get(name)
        if handler is None:
            rpc_error(request_id, -32602, f"Unknown tool: {name}")
            return
        try:
            ok(request_id, handler(args))
        except Exception as exc:
            ok(request_id, tool_error(f"{type(exc).__name__}: {exc}"))
        return

    rpc_error(request_id, -32601, f"Method not found: {method}")


def main() -> int:
    global _SHOULD_EXIT
    _SHOULD_EXIT = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            rpc_error(None, -32700, f"Parse error: {exc}")
            continue
        if isinstance(message, list):
            for item in message:
                if isinstance(item, dict):
                    handle_request(item)
                if _SHOULD_EXIT:
                    break
        elif isinstance(message, dict):
            handle_request(message)
        else:
            rpc_error(None, -32600, "Invalid request")
        if _SHOULD_EXIT:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

