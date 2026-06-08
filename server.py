#!/usr/bin/env python3
"""Mnemo: dependency-free local MCP memory server.

Transport: newline-delimited JSON-RPC on stdin/stdout.
Storage: SQLite primary store (default) with optional JSON compatibility mode.

Environment variables:
- MNEMO_STORE: sqlite|json. Defaults to sqlite.
- MNEMO_FILE: compatibility/import/export path for memory.json.
- MNEMO_SQLITE_FILE: sqlite db path. Defaults to <workspace>/state/mnemo/mnemo.sqlite.
- MNEMO_MAX_MEMORIES: total memory cap including retired entries. Defaults to 5000.
- MNEMO_PACK_LANDING_DIR: default landing folder for inbound .mem packs.
  Defaults to <workspace>/state/mnemo/packs/inbox.
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
- MNEMO_IDF_MODE: auto|off|force. Defaults to auto.
- MNEMO_IDF_MIN_DOCUMENTS: project corpus documents threshold. Defaults to 200.
- MNEMO_IDF_MIN_UNIQUE_TERMS: project corpus unique-terms threshold. Defaults to 1000.
- MNEMO_IDF_MIN_TOTAL_TOKENS: project corpus token threshold. Defaults to 10000.
- MNEMO_IDF_DOMAIN_MIN_DOCUMENTS: domain corpus documents threshold. Defaults to 50.
- MNEMO_IDF_DOMAIN_MIN_UNIQUE_TERMS: domain corpus unique-terms threshold. Defaults to 300.
- MNEMO_IDF_DOMAIN_MIN_TOTAL_TOKENS: domain corpus token threshold. Defaults to 3000.
- MNEMO_IDF_MIN_TEXT_TOKENS: per-memory minimum tokens for IDF corpus inclusion. Defaults to 5.
- MNEMO_MISS_TOP_SCORE_THRESHOLD: query miss threshold for top score. Defaults to 0.15.
- AGENT_SALIENCE_HOME: optional path to local agent-salience checkout for diagnostics when not installed.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from git_context import capture_git_context, current_file_sha, file_sha_at_head
from salience_loader import load_optional_agent_salience


PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "mnemo"
SERVER_TITLE = "Mnemo Project Memory"
SERVER_VERSION = "0.21.7"
SQLITE_SCHEMA_VERSION = "7"
DEFAULT_MEMORY_FILE = Path(__file__).with_name("memory.json")
DEFAULT_MEMORY_NAMESPACE = "local"
DEFAULT_MEMORY_ORIGIN = "local"
PACK_PREVIEW_DEFAULT_KINDS = ("context_block", "hippocampus_entry")
PACK_PREVIEW_POLICY_WARNING_KINDS = ("interaction_log", "agent_feedback")
PACK_EXPORT_ALLOWED_KINDS = ("context_block", "hippocampus_entry")
BASELINE_REDACTION_RULESET_VERSION = "baseline-v1"
PACK_REDACTION_RULE_ORDER = (
    "private_key_header",
    "jwt",
    "aws_access_key",
    "email",
    "user_path",
    "ipv4",
)
PACK_REDACTION_TEXT_FIELDS = ("text", "title")
PACK_RESERVED_BASENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}
PACK_REQUIRED_MEMBERS = (
    "manifest.json",
    "content/memories.jsonl",
    "content/topics.json",
    "content/file_fingerprints.json",
    "provenance/origin.json",
    "provenance/redactions.json",
)
PACK_CONTENT_HASH_COVERED_MEMBERS = (
    "content/file_fingerprints.json",
    "content/memories.jsonl",
    "content/topics.json",
    "provenance/origin.json",
    "provenance/redactions.json",
)
PACK_INSPECT_REQUIRED_MEMBERS = (
    "manifest.json",
    "content/memories.jsonl",
    "content/topics.json",
    "content/file_fingerprints.json",
    "provenance/origin.json",
    "provenance/redactions.json",
)
PACK_INSPECT_KNOWN_EXTRA_MEMBERS = {
    "signature/pack.sig",
    "signature/pubkey.txt",
    "signature/signature.json",
}
PACK_INSPECT_MAX_MEMBERS = 1000
PACK_INSPECT_MAX_MEMBER_SIZE = 25 * 1024 * 1024
PACK_INSPECT_MAX_TOTAL_SIZE = 100 * 1024 * 1024
PACK_INSPECT_SOURCE_ID_RE = re.compile(r"\bmem_[A-Za-z0-9_:-]+\b")
PACK_INSPECT_PACK_ID_RE = re.compile(r"^pack_\d{8}T\d{6}Z_[0-9a-f]{8}$")
PACK_INSPECT_UTC_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
PACK_BASELINE_WARNING_MESSAGE = (
    "Redaction covered: private_key_header, jwt, aws_access_key, email, user_path, ipv4. "
    "Other secret formats such as GitHub, Slack, GCP, Stripe, Azure SAS, generic Bearer tokens, "
    "and IPv6 are not detected."
)
PACK_LOCAL_HMAC_WARNING_MESSAGE = (
    "Local HMAC signing is not public-key signing and is intended for local/dev trust workflows only."
)
PACK_KIND_PREVIEW_ERROR_TEMPLATE = "kind '{kind}' is previewable but not exportable in v1 policy"
PACK_ALLOW_UNSIGNED_ERROR = (
    "Unsigned export requires allow_unsigned=true when sign_pack is not enabled."
)
PACK_IMPORT_ALLOW_UNSIGNED_QUARANTINE_ERROR = (
    "Unsigned pack import is quarantine-only in this phase; pass allow_unsigned_quarantine=true to import into quarantine."
)
PACK_IMPORT_TARGET_NOT_ALLOWED_ERROR = (
    "Import target is not allowed in this phase; pass allow_trusted_import=true for trusted import or "
    "allow_unsigned_quarantine=true for quarantine import."
)
PACK_IMPORT_OUTPUT_MAX_ROWS = 100
PACK_IMPORT_FRESHNESS_VALUES = ("verified", "stale", "missing", "unknown")
PACK_IMPORT_UNKNOWN_TEXT_FIELD_WARNING_CODE = "unknown_text_field_skipped"
PACK_IMPORT_OUTPUT_TRUNCATED_WARNING_CODE = "imported_rows_truncated"
PACK_SIGNATURE_MEMBER = "signature/signature.json"
PACK_UNSIGNED_REASON_SIGNING_NOT_IMPLEMENTED = "signing_not_implemented"
PACK_UNSIGNED_REASON_OPERATOR = "operator_chose_unsigned"
PACK_FILE_EXTENSION = ".mem"
PACK_LEGACY_FILE_EXTENSION = ".zip"
PACK_SIGNATURE_ALGORITHM_HMAC_LOCAL = "hmac-sha256-local-v1"
PACK_SIGNATURE_PAYLOAD_VERSION_V1 = "memory-pack-signing-v1"
PACK_SIGNATURE_SCHEMA_VERSION = 1
PACK_SECRET_MIN_LENGTH = 32
PACK_SIGNER_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{3,128}$")
SECRET_PARAM_NAMES = {"signing_secret", "verification_secret", "secret"}
PACK_LIST_IMPORTS_LIMIT_DEFAULT = 50
PACK_LIST_IMPORTS_LIMIT_MAX = 200
PACK_REVIEW_LIMIT_DEFAULT = 100
PACK_REVIEW_LIMIT_MAX = 1000
PACK_REVIEW_SAMPLE_LIMIT_DEFAULT = 10
PACK_REVIEW_SAMPLE_LIMIT_MAX = 50
MEMORY_GROUP_DISCOVER_LIMIT_DEFAULT = 20
MEMORY_GROUP_DISCOVER_LIMIT_MAX = 100
MEMORY_GROUP_SAMPLE_PER_GROUP_DEFAULT = 3
MEMORY_GROUP_PREVIEW_LIMIT_DEFAULT = 500
MEMORY_GROUP_PREVIEW_LIMIT_MAX = 1000
MEMORY_GROUP_MECHANICAL_TOPIC_PREFIXES = ("export:", "synthetic:run:", "synthetic:cohort:")
PACK_PROMOTE_PREVIEW_CANDIDATE_OUTPUT_MAX = 100
PACK_PROMOTE_OUTPUT_MAX_ROWS = 100
PACK_SAMPLES_SCAN_ORDER = tuple(PACK_REDACTION_TEXT_FIELDS)
PACK_NAMESPACE_PREFIX = "pack:"
PACK_QUARANTINE_PREFIX = "pack:quarantine:"
PACK_TRUSTED_PREFIX = "pack:trusted:"
TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]+")
CAMEL_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|[0-9]+")
TOKEN_CHARS_PER_TOKEN = 3.7
NORMALIZER_VERSION = 1
SIGNATURE_VERSION = 1
IDF_PROFILE_VERSION = 1
DEFAULT_MAX_SIGNATURE_SHINGLES = 256
DEFAULT_SHINGLE_SIZE = 3
DEFAULT_TOP_TERMS = 32
MAX_SIGNATURE_TEXT_CHARS = 50_000
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
_GIT_CONTEXT_CACHE: dict[str, dict[str, Any]] = {}
_SQLITE_FTS_CANDIDATE_LIMIT = 500
DEFAULT_EVENT_LIMIT = 20
MAX_EVENT_LIMIT = 200
DEFAULT_ALIAS_PROPOSAL_WINDOW_DAYS = 30
DEFAULT_ALIAS_PROPOSAL_MIN_RECURRENCE = 3
DEFAULT_ALIAS_PROPOSAL_LIMIT = 20
DEFAULT_ALIAS_PROPOSAL_MIN_LOOSE_SCORE = 0.20
DEFAULT_ALIAS_PROPOSAL_MAX_CANDIDATES_PER_CLUSTER = 5
MAX_ALIAS_PROPOSAL_EVENT_SCAN = 4000
ALIAS_CLUSTER_SHINGLE_OVERLAP_THRESHOLD = 0.35
DEFAULT_ALIAS_LANGUAGE = "en"
DEFAULT_ALIAS_PROPOSAL_LIST_LIMIT = 50
DEFAULT_ALIAS_LIST_LIMIT = 200
ALIAS_CONCEPT_BASE_BOOST = 0.10
ALIAS_CONCEPT_MAX_BOOST = 0.18
MISS_EVENT_ACTIONS = {
    "mnemo_search",
    "search",
    "mnemo_recall",
    "recall",
    "mnemo_salience_check",
    "salience_check",
    "mnemo_compact_context",
    "mnemo_recent",
}
EVENT_SALIENCE_ACTIONS = {
    "create",
    "update",
    "delete",
    "supersede",
    "link",
    "archive",
    "query",
    "mnemo_search",
    "mnemo_compact_context",
    "mnemo_recent",
    "mnemo_recall",
    "alias_hint",
    "mnemo_get",
    "mnemo_lookup_symbol",
    "mnemo_maintenance",
    "record",
    "search",
    "recent",
    "recall",
    "get",
    "lookup_symbol",
}
IDF_ACTIVE_WEIGHTS: dict[str, float] = {
    "idf_cosine": 0.55,
    "idf_jaccard": 0.35,
    "cosine": 0.05,
    "jaccard": 0.05,
}
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


def miss_top_score_threshold() -> float:
    raw = str(os.environ.get("MNEMO_MISS_TOP_SCORE_THRESHOLD", "0.15")).strip() or "0.15"
    try:
        value = float(raw)
    except ValueError:
        value = 0.15
    return max(0.0, min(value, 1.0))


def idf_mode() -> str:
    raw = str(os.environ.get("MNEMO_IDF_MODE", "auto")).strip().lower()
    if raw in {"auto", "off", "force"}:
        return raw
    return "auto"


def idf_min_text_tokens() -> int:
    return positive_int_env("MNEMO_IDF_MIN_TEXT_TOKENS", 5)


def idf_thresholds(scope: str) -> dict[str, int]:
    scope_name = str(scope).strip().lower()
    if scope_name == "domain":
        return {
            "min_documents": positive_int_env("MNEMO_IDF_DOMAIN_MIN_DOCUMENTS", 50),
            "min_unique_terms": positive_int_env("MNEMO_IDF_DOMAIN_MIN_UNIQUE_TERMS", 300),
            "min_total_tokens": positive_int_env("MNEMO_IDF_DOMAIN_MIN_TOTAL_TOKENS", 3000),
        }
    return {
        "min_documents": positive_int_env("MNEMO_IDF_MIN_DOCUMENTS", 200),
        "min_unique_terms": positive_int_env("MNEMO_IDF_MIN_UNIQUE_TERMS", 1000),
        "min_total_tokens": positive_int_env("MNEMO_IDF_MIN_TOTAL_TOKENS", 10000),
    }


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


def scrub_secret_params(payload: Any) -> Any:
    if isinstance(payload, dict):
        out: dict[Any, Any] = {}
        for key, value in payload.items():
            key_text = str(key)
            if key_text in SECRET_PARAM_NAMES:
                out[key] = "[REDACTED]"
            else:
                out[key] = scrub_secret_params(value)
        return out
    if isinstance(payload, list):
        return [scrub_secret_params(item) for item in payload]
    if isinstance(payload, tuple):
        return tuple(scrub_secret_params(item) for item in payload)
    return payload


def _secret_fingerprint(secret: str) -> str:
    return hashlib.sha256(str(secret).encode("utf-8")).hexdigest()[:32]


def _validate_secret_length(secret: Any, *, field_name: str) -> str:
    value = normalize_optional_string(secret)
    if value is None or len(value) < PACK_SECRET_MIN_LENGTH:
        raise ValueError(f"{field_name} must be at least {PACK_SECRET_MIN_LENGTH} characters")
    return value


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


# Fallback mirrors Agent Salience semantics when agent-salience is unavailable.
# Do not change this independently from Agent Salience behavior.
def _jaccard_similarity_fallback(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def jaccard(a: set[str], b: set[str]) -> float:
    """Compatibility alias for _jaccard_similarity_fallback."""
    return _jaccard_similarity_fallback(a, b)


def _normalize_for_signature(text: str) -> list[str]:
    """Normalize text to a token list for signature purposes. Caps at MAX_SIGNATURE_TEXT_CHARS."""
    capped = text[:MAX_SIGNATURE_TEXT_CHARS]
    tokens: list[str] = []
    for match in TOKEN_RE.finditer(capped.lower()):
        raw = match.group(0)
        tokens.extend(token_variants(raw))
    return tokens


def _stable_hash_hex(value: str) -> str:
    """Deterministic blake2b hex hash. Never uses Python's built-in hash()."""
    return hashlib.blake2b(value.encode("utf-8"), digest_size=8).hexdigest()


def _build_word_shingles(tokens: list[str], n: int = DEFAULT_SHINGLE_SIZE) -> list[str]:
    """Build word n-gram shingles. Returns [] if fewer than n tokens (tiny texts have no shingle signature)."""
    if len(tokens) < n:
        return []
    return [" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


def _build_min_k_shingle_hashes(tokens: list[str], k: int = DEFAULT_MAX_SIGNATURE_SHINGLES) -> list[str]:
    """Compute deterministic blake2b hashes for all shingles, return the k smallest sorted ascending."""
    shingles = _build_word_shingles(tokens)
    if not shingles:
        return []
    hashes = [_stable_hash_hex(s) for s in shingles]
    hashes.sort()
    return hashes[:k]


def _build_top_terms(tokens: list[str], k: int = DEFAULT_TOP_TERMS) -> list[str]:
    """Return the top k tokens by descending frequency, alpha tiebreak. All unique tokens if fewer than k."""
    if not tokens:
        return []
    freq: dict[str, int] = {}
    for t in tokens:
        freq[t] = freq.get(t, 0) + 1
    sorted_terms = sorted(freq.keys(), key=lambda t: (-freq[t], t))
    return sorted_terms[:k]


def _normalize_raw_text_for_content_hash(text: str) -> str:
    """Normalize raw text for exact content hashing without truncating content."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.split("\n"))


def _build_memory_signature(text: str) -> dict[str, Any]:
    """Compute all signature fields for a memory text.

    content_hash covers the full raw text after stable line-ending normalization.
    Normalized-token signatures are capped at MAX_SIGNATURE_TEXT_CHARS to avoid
    runaway tokenization on pasted logs or dumps. Raw text storage is unaffected.
    """
    content_hash = _stable_hash_hex(_normalize_raw_text_for_content_hash(text))
    tokens = _normalize_for_signature(text)
    unique_tokens = list(dict.fromkeys(tokens))
    normalized_hash = _stable_hash_hex(" ".join(tokens))
    top_terms = _build_top_terms(tokens)
    shingle_hashes = _build_min_k_shingle_hashes(tokens)
    return {
        "content_hash": content_hash,
        "normalized_hash": normalized_hash,
        "token_count": len(tokens),
        "unique_token_count": len(unique_tokens),
        "top_terms_json": json.dumps(top_terms, ensure_ascii=False),
        "shingle_hashes_json": json.dumps(shingle_hashes, ensure_ascii=False),
        "signature_version": SIGNATURE_VERSION,
        "normalizer_version": NORMALIZER_VERSION,
        "signature_updated_at": now_iso(),
    }


def _signature_overlap(sig_a: list[str], sig_b: list[str]) -> float:
    """Jaccard overlap between two sorted min-K shingle hash lists. 0.0 if either is empty.

    Uses a linear merge on sorted input — faster than set operations for the hot
    consolidation candidate loop.
    """
    if not sig_a or not sig_b:
        return 0.0
    intersection = 0
    i = j = 0
    la, lb = len(sig_a), len(sig_b)
    while i < la and j < lb:
        a, b = sig_a[i], sig_b[j]
        if a == b:
            intersection += 1
            i += 1
            j += 1
        elif a < b:
            i += 1
        else:
            j += 1
    union = la + lb - intersection
    return intersection / union if union > 0 else 0.0


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


def normalize_touched_files(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError("touched_files must be an array of strings")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("touched_files must be an array of strings")
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def normalize_repo_relative_path(raw: Any, *, root: Path | None = None) -> str | None:
    value = normalize_optional_string(raw)
    if value is None:
        return None
    root_path = (root or workspace_root()).resolve()
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        try:
            value = candidate.resolve().relative_to(root_path).as_posix()
        except Exception:
            value = candidate.as_posix()
    else:
        value = value.replace("\\", "/")
    value = re.sub(r"/+", "/", value).strip()
    while value.startswith("./"):
        value = value[2:]
    if value.endswith("/") and len(value) > 1:
        value = value.rstrip("/")
    return value or None


def normalize_touched_paths(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    values = normalize_optional_string_list(raw, "touched_paths")
    if values is None:
        return None
    out: list[str] = []
    seen: set[str] = set()
    root = workspace_root()
    for item in values:
        normalized = normalize_repo_relative_path(item, root=root)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def normalize_iso_utc_timestamp(raw: Any, field: str) -> str | None:
    value = normalize_optional_string(raw)
    if value is None:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field} must be ISO-8601 UTC")
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_topic(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("topic must be a non-empty string")
    return value


def normalize_optional_string_list(raw: Any, field: str) -> list[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError(f"{field} must be an array of strings")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"{field} must be an array of strings")
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def normalize_memory_namespace(raw: Any, default: str = DEFAULT_MEMORY_NAMESPACE) -> str:
    return normalize_optional_string(raw) or default


def normalize_memory_origin(raw: Any, default: str = DEFAULT_MEMORY_ORIGIN) -> str:
    return normalize_optional_string(raw) or default


def derive_pack_id_from_namespace(namespace: Any) -> str | None:
    namespace_text = normalize_optional_string(namespace)
    if namespace_text is None:
        return None
    if namespace_text.startswith(PACK_TRUSTED_PREFIX):
        pack_id = namespace_text[len(PACK_TRUSTED_PREFIX) :].strip()
        return pack_id or None
    if namespace_text.startswith(PACK_QUARANTINE_PREFIX):
        pack_id = namespace_text[len(PACK_QUARANTINE_PREFIX) :].strip()
        return pack_id or None
    if namespace_text.startswith(PACK_NAMESPACE_PREFIX):
        pack_id = namespace_text[len(PACK_NAMESPACE_PREFIX) :].strip()
        return pack_id or None
    return None


def memory_namespace(memory: dict[str, Any]) -> str:
    return normalize_memory_namespace(memory.get("namespace"), DEFAULT_MEMORY_NAMESPACE)


def memory_origin(memory: dict[str, Any]) -> str:
    return normalize_memory_origin(memory.get("origin"), DEFAULT_MEMORY_ORIGIN)


def memory_import_freshness(memory: dict[str, Any]) -> str | None:
    return normalize_optional_string(memory.get("import_freshness"))


def _memory_in_scope(memory: dict[str, Any], namespaces: list[str], origins: list[str] | None) -> bool:
    namespace_value = memory_namespace(memory)
    if namespace_value not in namespaces:
        return False
    if origins is not None and memory_origin(memory) not in origins:
        return False
    return True


def normalize_git_dirty(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return 1 if raw else 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        text = str(raw).strip().lower()
        if text in {"true", "yes", "on"}:
            return 1
        if text in {"false", "no", "off"}:
            return 0
        return None
    return 1 if value else 0


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
    touched_files: list[str] | None = None,
    parent_id: str | None = None,
    source_run_id: str | None = None,
    git_sha: str | None = None,
    git_branch: str | None = None,
    git_dirty: int | None = None,
    namespace: str = DEFAULT_MEMORY_NAMESPACE,
    origin: str = DEFAULT_MEMORY_ORIGIN,
    import_freshness: str | None = None,
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
        "git_sha": normalize_optional_string(git_sha),
        "git_branch": normalize_optional_string(git_branch),
        "git_dirty": normalize_git_dirty(git_dirty),
        "namespace": normalize_memory_namespace(namespace, DEFAULT_MEMORY_NAMESPACE),
        "origin": normalize_memory_origin(origin, DEFAULT_MEMORY_ORIGIN),
        "import_freshness": normalize_optional_string(import_freshness),
        "metadata": metadata or {},
        "created_at": now_iso(),
        "updated_at": None,
        "deleted_at": None,
        "deletion_reason": None,
        "superseded_by": None,
        "_touched_files": list(touched_files or []),
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
    memory["namespace"] = normalize_memory_namespace(args.get("namespace"), memory_namespace(memory))
    memory["origin"] = normalize_memory_origin(args.get("origin"), memory_origin(memory))
    memory["import_freshness"] = normalize_optional_string(args.get("import_freshness"))
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
    migrated["git_sha"] = normalize_optional_string(migrated.get("git_sha"))
    migrated["git_branch"] = normalize_optional_string(migrated.get("git_branch"))
    migrated["git_dirty"] = normalize_git_dirty(migrated.get("git_dirty"))
    migrated["namespace"] = normalize_memory_namespace(migrated.get("namespace"), DEFAULT_MEMORY_NAMESPACE)
    migrated["origin"] = normalize_memory_origin(migrated.get("origin"), DEFAULT_MEMORY_ORIGIN)
    migrated["import_freshness"] = normalize_optional_string(migrated.get("import_freshness"))
    touched_files = migrated.get("_touched_files")
    if isinstance(touched_files, list):
        migrated["_touched_files"] = [str(path).strip() for path in touched_files if str(path).strip()]
    else:
        migrated["_touched_files"] = []
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


def _event_payload_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        scrubbed = scrub_secret_params(payload)
        return dict(scrubbed) if isinstance(scrubbed, dict) else {"value": scrubbed}
    return {"value": scrub_secret_params(payload)}


def _event_int_value(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _event_float_value(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _event_query_text(payload: dict[str, Any]) -> str | None:
    query_text = normalize_optional_string(payload.get("query_text"))
    if query_text:
        return query_text
    query_text = normalize_optional_string(payload.get("query"))
    if query_text:
        return query_text
    query_text = normalize_optional_string(payload.get("original_query"))
    if query_text:
        return query_text
    query_text = normalize_optional_string(payload.get("successful_query"))
    if query_text:
        return query_text
    query_text = normalize_optional_string(payload.get("candidate_alias"))
    if query_text:
        return query_text
    args = payload.get("args")
    if isinstance(args, dict):
        query_text = normalize_optional_string(args.get("query"))
        if query_text:
            return query_text
        query_text = normalize_optional_string(args.get("task"))
        if query_text:
            return query_text
    return None


def _event_action(payload: dict[str, Any], event_type: str) -> str:
    action = normalize_optional_string(payload.get("action"))
    if action:
        return action
    tool_name = normalize_optional_string(payload.get("tool"))
    if tool_name:
        return tool_name
    return event_type


def _event_summary(action: str, event_type: str, payload: dict[str, Any], query_text: str | None) -> str | None:
    explicit = normalize_optional_string(payload.get("summary"))
    if explicit:
        return explicit
    if query_text:
        n_results = _event_int_value(payload.get("result_count"))
        if n_results is None:
            n_results = _event_int_value(payload.get("n_results"))
        if n_results is not None:
            return f"{action}: {query_text} ({n_results} results)"
        return f"{action}: {query_text}"
    if event_type == "create":
        kind = normalize_optional_string(payload.get("kind"))
        if kind:
            return f"created {kind} memory"
        return "created memory"
    if event_type == "update":
        changed = payload.get("changed")
        if isinstance(changed, list) and changed:
            fields = ", ".join(str(field) for field in changed[:6])
            return f"updated fields: {fields}"
        return "updated memory"
    if event_type == "delete":
        reason = normalize_optional_string(payload.get("reason"))
        if reason:
            return f"deleted memory ({reason})"
        return "deleted memory"
    if event_type == "supersede":
        target = normalize_optional_string(payload.get("superseded_by"))
        return f"superseded by {target}" if target else "superseded memory"
    if event_type == "link":
        target = normalize_optional_string(payload.get("target_id"))
        relation = normalize_optional_string(payload.get("relation"))
        if target and relation:
            return f"linked to {target} ({relation})"
        if target:
            return f"linked to {target}"
        return "linked memory"
    if event_type == "archive":
        archived_to = normalize_optional_string(payload.get("archived_to"))
        return f"archived to {archived_to}" if archived_to else "archived memory"
    return normalize_optional_string(payload.get("message")) or event_type


def _event_include_in_salience(action: str, payload: dict[str, Any]) -> int:
    explicit = payload.get("include_in_salience")
    if explicit is not None:
        return 1 if parse_bool(explicit, default=False) else 0
    return 1 if action in EVENT_SALIENCE_ACTIONS else 0


def _event_salience_text(
    action: str,
    memory_id: str | None,
    source_id: str | None,
    target_id: str | None,
    relation: str | None,
    query_text: str | None,
    summary: str | None,
    payload: dict[str, Any],
) -> str | None:
    parts = [
        action,
        memory_id,
        source_id,
        target_id,
        relation,
        query_text,
        summary,
        normalize_optional_string(payload.get("error")),
        normalize_optional_string(payload.get("message")),
    ]
    text = " | ".join(str(part).strip() for part in parts if part and str(part).strip())
    return text or None


def _event_record_fields(
    memory_id: str | None,
    event_type: str,
    payload_raw: Any,
    created: str,
    event_id: str,
) -> dict[str, Any]:
    payload = _event_payload_dict(payload_raw)
    action = _event_action(payload, event_type)
    query_text = _event_query_text(payload)
    args = payload.get("args")
    args_dict = args if isinstance(args, dict) else {}

    memory_value = normalize_optional_string(memory_id) or normalize_optional_string(payload.get("memory_id"))
    source_id = normalize_optional_string(payload.get("source_id"))
    target_id = normalize_optional_string(payload.get("target_id")) or normalize_optional_string(payload.get("superseded_by"))
    relation = normalize_optional_string(payload.get("relation"))
    if source_id is None and action == "link":
        source_id = memory_value
    if target_id is None and action == "link":
        target_id = normalize_optional_string(payload.get("id"))
    result_count = _event_int_value(payload.get("result_count"))
    if result_count is None:
        result_count = _event_int_value(payload.get("n_results"))
    top_score = _event_float_value(payload.get("top_score"))
    if top_score is None:
        top_score = _event_float_value(payload.get("score"))
    success = _event_int_value(payload.get("success"))
    if success is None:
        success = 1
    kind = normalize_optional_string(payload.get("kind")) or normalize_optional_string(args_dict.get("kind"))
    domain = normalize_optional_string(payload.get("domain")) or normalize_optional_string(args_dict.get("domain"))
    role = normalize_optional_string(payload.get("role")) or normalize_optional_string(args_dict.get("role"))
    agent_id = normalize_optional_string(payload.get("agent_id")) or normalize_optional_string(args_dict.get("agent_id"))
    summary = _event_summary(action, event_type, payload, query_text)
    include_in_salience = _event_include_in_salience(action, payload)
    salience_text = _event_salience_text(
        action,
        memory_value,
        source_id,
        target_id,
        relation,
        query_text,
        summary,
        payload,
    )

    return {
        "id": event_id,
        "event_id": event_id,
        "memory_id": memory_value,
        "event_type": event_type,
        "action": action,
        "source_id": source_id,
        "target_id": target_id,
        "relation": relation,
        "query_text": query_text,
        "result_count": result_count,
        "top_score": top_score,
        "success": success,
        "agent_id": agent_id,
        "role": role,
        "domain": domain,
        "kind": kind,
        "summary": summary,
        "salience_text": salience_text,
        "include_in_salience": include_in_salience,
        "data_json": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "created_at": created,
        "ts": created,
    }


def _sqlite_events_fts_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
                event_id UNINDEXED,
                action,
                memory_id,
                source_id,
                target_id,
                relation,
                query_text,
                summary,
                salience_text,
                agent_id,
                role,
                domain,
                kind
            )
            """
        )
        return True
    except sqlite3.OperationalError:
        return False


def _sqlite_has_events_fts_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='events_fts'"
    ).fetchone()
    return row is not None


def _sqlite_sync_events_fts_for_record(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    if not _sqlite_has_events_fts_table(conn) and not _sqlite_events_fts_available(conn):
        return
    conn.execute(
        """
        INSERT OR REPLACE INTO events_fts(
            event_id, action, memory_id, source_id, target_id, relation, query_text,
            summary, salience_text, agent_id, role, domain, kind
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(record.get("event_id") or ""),
            str(record.get("action") or ""),
            str(record.get("memory_id") or ""),
            str(record.get("source_id") or ""),
            str(record.get("target_id") or ""),
            str(record.get("relation") or ""),
            str(record.get("query_text") or ""),
            str(record.get("summary") or ""),
            str(record.get("salience_text") or ""),
            str(record.get("agent_id") or ""),
            str(record.get("role") or ""),
            str(record.get("domain") or ""),
            str(record.get("kind") or ""),
        ),
    )


def _sqlite_rebuild_events_fts_index(conn: sqlite3.Connection) -> bool:
    if not _sqlite_has_events_fts_table(conn) and not _sqlite_events_fts_available(conn):
        return False
    conn.execute("DELETE FROM events_fts")
    rows = conn.execute(
        """
        SELECT event_id, action, memory_id, source_id, target_id, relation, query_text,
               summary, salience_text, agent_id, role, domain, kind
        FROM events
        """
    ).fetchall()
    for row in rows:
        record = {
            "event_id": row["event_id"],
            "action": row["action"],
            "memory_id": row["memory_id"],
            "source_id": row["source_id"],
            "target_id": row["target_id"],
            "relation": row["relation"],
            "query_text": row["query_text"],
            "summary": row["summary"],
            "salience_text": row["salience_text"],
            "agent_id": row["agent_id"],
            "role": row["role"],
            "domain": row["domain"],
            "kind": row["kind"],
        }
        _sqlite_sync_events_fts_for_record(conn, record)
    _sqlite_set_meta(conn, "events_fts_index_built_at", now_iso())
    return True


def _sqlite_enrich_event_record_from_memory(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    memory_id = normalize_optional_string(record.get("memory_id"))
    if not memory_id:
        return
    needs = any(
        normalize_optional_string(record.get(field)) is None
        for field in ("kind", "domain", "role", "agent_id")
    )
    if not needs:
        return
    row = conn.execute(
        "SELECT kind, domain, role, agent_id FROM memories WHERE id = ?",
        (memory_id,),
    ).fetchone()
    if row is None:
        return
    for field in ("kind", "domain", "role", "agent_id"):
        if normalize_optional_string(record.get(field)) is None:
            record[field] = normalize_optional_string(row[field])


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
            git_sha TEXT,
            git_branch TEXT,
            git_dirty INTEGER,
            namespace TEXT NOT NULL DEFAULT 'local',
            origin TEXT NOT NULL DEFAULT 'local',
            import_freshness TEXT,
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
        """
        CREATE TABLE IF NOT EXISTS alias_concepts (
            concept_id TEXT PRIMARY KEY,
            canonical TEXT NOT NULL,
            domain TEXT,
            language TEXT DEFAULT 'en',
            status TEXT NOT NULL DEFAULT 'active',
            weight REAL NOT NULL DEFAULT 1.0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            source TEXT,
            notes TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alias_terms (
            alias_id TEXT PRIMARY KEY,
            concept_id TEXT NOT NULL,
            term TEXT NOT NULL,
            normalized_term TEXT NOT NULL,
            language TEXT DEFAULT 'en',
            domain TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            weight REAL NOT NULL DEFAULT 1.0,
            source TEXT,
            approved_by TEXT,
            approved_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (concept_id) REFERENCES alias_concepts(concept_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alias_proposals (
            proposal_id TEXT PRIMARY KEY,
            domain TEXT,
            language TEXT DEFAULT 'en',
            canonical TEXT,
            candidate_alias TEXT NOT NULL,
            normalized_alias TEXT NOT NULL,
            score REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            recommendation TEXT,
            evidence_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alias_proposal_events (
            proposal_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            relation TEXT NOT NULL,
            PRIMARY KEY (proposal_id, event_id)
        )
        """
    )
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
            PRIMARY KEY (memory_id, topic),
            FOREIGN KEY (memory_id) REFERENCES memories(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS imported_packs (
            pack_id TEXT PRIMARY KEY,
            pack_name TEXT NOT NULL,
            source_label TEXT,
            trust_level TEXT NOT NULL CHECK (trust_level IN ('trusted', 'quarantine')),
            namespace TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            freshness_summary_json TEXT,
            received_zip_sha256 TEXT
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS promoted_pack_rows (
            pack_id TEXT NOT NULL,
            row_id_in_pack TEXT NOT NULL,
            imported_memory_id TEXT NOT NULL,
            promoted_memory_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            promoted_at TEXT NOT NULL,
            original_import_freshness TEXT,
            promotion_id TEXT,
            PRIMARY KEY (pack_id, row_id_in_pack),
            UNIQUE(promoted_memory_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS promotion_audit (
            promotion_id TEXT PRIMARY KEY,
            pack_id TEXT NOT NULL,
            promoted_at TEXT NOT NULL,
            filters_json TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            limited INTEGER NOT NULL DEFAULT 0,
            allow_promote_all INTEGER NOT NULL DEFAULT 0,
            allow_limited_promotion INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trusted_signers (
            signer_id TEXT PRIMARY KEY,
            label TEXT,
            trust_level TEXT NOT NULL,
            signature_algorithm TEXT NOT NULL,
            secret_fingerprint TEXT,
            public_key TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            notes TEXT
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
        "CREATE INDEX IF NOT EXISTS idx_idf_profiles_scope_name ON idf_profiles(scope, name)",
        "CREATE INDEX IF NOT EXISTS idx_idf_profiles_updated_at ON idf_profiles(updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_alias_terms_norm ON alias_terms(normalized_term)",
        "CREATE INDEX IF NOT EXISTS idx_alias_terms_domain_norm ON alias_terms(domain, normalized_term)",
        "CREATE INDEX IF NOT EXISTS idx_alias_terms_concept ON alias_terms(concept_id)",
        "CREATE INDEX IF NOT EXISTS idx_alias_concepts_domain ON alias_concepts(domain, status)",
        "CREATE INDEX IF NOT EXISTS idx_alias_proposals_status ON alias_proposals(status, score)",
        "CREATE INDEX IF NOT EXISTS idx_alias_proposals_domain_status ON alias_proposals(domain, status, score)",
        "CREATE INDEX IF NOT EXISTS idx_memory_files_path ON memory_files(path)",
        "CREATE INDEX IF NOT EXISTS idx_memory_topics_topic ON memory_topics(topic)",
        "CREATE INDEX IF NOT EXISTS idx_memory_topics_memory_id ON memory_topics(memory_id)",
        "CREATE INDEX IF NOT EXISTS idx_imported_pack_rows_pack_id ON imported_pack_rows(pack_id)",
        "CREATE INDEX IF NOT EXISTS idx_imported_pack_rows_memory_id ON imported_pack_rows(memory_id)",
        "CREATE INDEX IF NOT EXISTS idx_promoted_pack_rows_pack_id ON promoted_pack_rows(pack_id)",
        "CREATE INDEX IF NOT EXISTS idx_promoted_pack_rows_imported_memory_id ON promoted_pack_rows(imported_memory_id)",
        "CREATE INDEX IF NOT EXISTS idx_promoted_pack_rows_promoted_memory_id ON promoted_pack_rows(promoted_memory_id)",
        "CREATE INDEX IF NOT EXISTS idx_promoted_pack_rows_promotion_id ON promoted_pack_rows(promotion_id)",
        "CREATE INDEX IF NOT EXISTS idx_promotion_audit_pack_id ON promotion_audit(pack_id)",
        "CREATE INDEX IF NOT EXISTS idx_trusted_signers_status ON trusted_signers(status)",
        "CREATE INDEX IF NOT EXISTS idx_trusted_signers_trust_level ON trusted_signers(trust_level)",
    ):
        conn.execute(statement)
    conn.execute(
        """
        CREATE VIEW IF NOT EXISTS v_alias_vocabulary AS
        SELECT
            c.domain AS domain,
            c.language AS language,
            c.concept_id AS concept_id,
            c.canonical AS canonical,
            t.term AS alias,
            t.normalized_term AS normalized_term,
            t.status AS status,
            t.weight AS weight,
            t.approved_at AS approved_at,
            t.approved_by AS approved_by,
            t.source AS source,
            t.updated_at AS updated_at
        FROM alias_terms t
        JOIN alias_concepts c ON c.concept_id = t.concept_id
        ORDER BY c.domain, c.language, c.concept_id, t.term
        """
    )
    conn.execute(
        """
        CREATE VIEW IF NOT EXISTS v_alias_pending_proposals AS
        SELECT
            domain,
            language,
            canonical,
            candidate_alias,
            normalized_alias,
            score,
            status,
            recommendation,
            created_at,
            updated_at
        FROM alias_proposals
        WHERE status = 'pending'
        ORDER BY score DESC, created_at DESC
        """
    )
    conn.execute(
        """
        CREATE VIEW IF NOT EXISTS v_alias_concept_counts AS
        SELECT
            c.domain AS domain,
            c.language AS language,
            c.concept_id AS concept_id,
            c.canonical AS canonical,
            c.status AS status,
            COUNT(t.alias_id) AS alias_count
        FROM alias_concepts c
        LEFT JOIN alias_terms t ON t.concept_id = c.concept_id
        GROUP BY c.domain, c.language, c.concept_id, c.canonical, c.status
        ORDER BY c.domain, c.language, alias_count DESC
        """
    )
    schema_version_raw = str(_sqlite_get_meta(conn, "schema_version") or "").strip()
    try:
        schema_version = int(schema_version_raw) if schema_version_raw else 0
    except ValueError:
        schema_version = 0

    # Idempotent column migrations for v0.12.0 signature columns
    _v12_signature_columns = [
        ("normalized_hash", "TEXT"),
        ("token_count", "INTEGER"),
        ("unique_token_count", "INTEGER"),
        ("top_terms_json", "TEXT"),
        ("shingle_hashes_json", "TEXT"),
        ("signature_version", "INTEGER"),
        ("normalizer_version", "INTEGER"),
        ("signature_updated_at", "TEXT"),
    ]
    _existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    for _col_name, _col_type in _v12_signature_columns:
        if _col_name not in _existing_cols:
            conn.execute(f"ALTER TABLE memories ADD COLUMN {_col_name} {_col_type}")
    _git_and_pack_columns = [
        ("git_sha", "TEXT"),
        ("git_branch", "TEXT"),
        ("git_dirty", "INTEGER"),
        ("namespace", "TEXT NOT NULL DEFAULT 'local'"),
        ("origin", "TEXT NOT NULL DEFAULT 'local'"),
        ("import_freshness", "TEXT"),
    ]
    _existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    for _col_name, _col_type in _git_and_pack_columns:
        if _col_name not in _existing_cols:
            conn.execute(f"ALTER TABLE memories ADD COLUMN {_col_name} {_col_type}")
    _imported_pack_columns = {row[1] for row in conn.execute("PRAGMA table_info(imported_packs)").fetchall()}
    if "received_zip_sha256" not in _imported_pack_columns:
        conn.execute("ALTER TABLE imported_packs ADD COLUMN received_zip_sha256 TEXT")
    _promoted_pack_row_columns = {row[1] for row in conn.execute("PRAGMA table_info(promoted_pack_rows)").fetchall()}
    if "promotion_id" not in _promoted_pack_row_columns:
        conn.execute("ALTER TABLE promoted_pack_rows ADD COLUMN promotion_id TEXT")
    for _statement in (
        "CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories(namespace)",
        "CREATE INDEX IF NOT EXISTS idx_memories_origin ON memories(origin)",
        "CREATE INDEX IF NOT EXISTS idx_memories_namespace_kind ON memories(namespace, kind)",
    ):
        conn.execute(_statement)
    _event_columns = [
        ("event_id", "TEXT"),
        ("ts", "TEXT"),
        ("action", "TEXT"),
        ("source_id", "TEXT"),
        ("target_id", "TEXT"),
        ("relation", "TEXT"),
        ("query_text", "TEXT"),
        ("result_count", "INTEGER"),
        ("top_score", "REAL"),
        ("success", "INTEGER"),
        ("agent_id", "TEXT"),
        ("role", "TEXT"),
        ("domain", "TEXT"),
        ("kind", "TEXT"),
        ("summary", "TEXT"),
        ("salience_text", "TEXT"),
        ("include_in_salience", "INTEGER"),
    ]
    _event_existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
    for _col_name, _col_type in _event_columns:
        if _col_name not in _event_existing_cols:
            conn.execute(f"ALTER TABLE events ADD COLUMN {_col_name} {_col_type}")
    conn.execute("UPDATE events SET event_id = id WHERE event_id IS NULL OR event_id = ''")
    conn.execute("UPDATE events SET ts = created_at WHERE ts IS NULL OR ts = ''")
    conn.execute(
        "UPDATE events SET action = COALESCE(NULLIF(action, ''), event_type) WHERE action IS NULL OR action = ''"
    )
    # Opportunistic backfill from payload JSON when a numeric top score is present.
    try:
        conn.execute(
            """
            UPDATE events
            SET top_score = CAST(json_extract(data_json, '$.top_score') AS REAL)
            WHERE top_score IS NULL
              AND json_valid(data_json) = 1
              AND json_type(data_json, '$.top_score') IN ('integer', 'real')
            """
        )
        conn.execute(
            """
            UPDATE events
            SET top_score = CAST(json_extract(data_json, '$.score') AS REAL)
            WHERE top_score IS NULL
              AND json_valid(data_json) = 1
              AND json_type(data_json, '$.score') IN ('integer', 'real')
            """
        )
    except sqlite3.OperationalError:
        # SQLite build without JSON1; leave top_score NULL for legacy rows.
        pass
    conn.execute("UPDATE events SET success = 1 WHERE success IS NULL")
    conn.execute("UPDATE events SET include_in_salience = 0 WHERE include_in_salience IS NULL")
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_mnemo_events_ts ON events(ts)",
        "CREATE INDEX IF NOT EXISTS idx_mnemo_events_action ON events(action)",
        "CREATE INDEX IF NOT EXISTS idx_mnemo_events_memory_id ON events(memory_id)",
        "CREATE INDEX IF NOT EXISTS idx_mnemo_events_domain ON events(domain)",
        "CREATE INDEX IF NOT EXISTS idx_mnemo_events_success ON events(success)",
        "CREATE INDEX IF NOT EXISTS idx_mnemo_events_include_salience ON events(include_in_salience)",
    ):
        conn.execute(statement)
    events_fts_available = _sqlite_events_fts_available(conn)
    _sqlite_set_meta(conn, "events_fts_available", "1" if events_fts_available else "0")
    if events_fts_available and _sqlite_get_meta(conn, "events_fts_index_built_at") is None:
        _sqlite_rebuild_events_fts_index(conn)
    _sqlite_set_meta(conn, "schema_version", SQLITE_SCHEMA_VERSION)
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
        "git_sha": normalize_optional_string(migrated.get("git_sha")),
        "git_branch": normalize_optional_string(migrated.get("git_branch")),
        "git_dirty": normalize_git_dirty(migrated.get("git_dirty")),
        "namespace": normalize_memory_namespace(migrated.get("namespace"), DEFAULT_MEMORY_NAMESPACE),
        "origin": normalize_memory_origin(migrated.get("origin"), DEFAULT_MEMORY_ORIGIN),
        "import_freshness": normalize_optional_string(migrated.get("import_freshness")),
        "metadata_json": json.dumps(metadata, ensure_ascii=False),
        "pinned": 1 if bool(migrated.get("pinned")) else 0,
        "deleted": 1 if bool(migrated.get("deleted_at")) else 0,
        "superseded_by": normalize_optional_string(migrated.get("superseded_by")),
        "created_at": str(migrated.get("created_at") or now_iso()),
        "updated_at": normalize_optional_string(migrated.get("updated_at")),
        "token_estimate": int(estimate_tokens(text_value)),
        "deletion_reason": normalize_optional_string(migrated.get("deletion_reason")),
        **_build_memory_signature(text_value),
    }


def _safe_row_get(row: sqlite3.Row, col: str) -> Any:
    """Access a sqlite3.Row column by name, returning None if the column doesn't exist."""
    try:
        return row[col]
    except (IndexError, KeyError):
        return None


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
        "git_sha": _safe_row_get(row, "git_sha"),
        "git_branch": _safe_row_get(row, "git_branch"),
        "git_dirty": normalize_git_dirty(_safe_row_get(row, "git_dirty")),
        "namespace": normalize_memory_namespace(_safe_row_get(row, "namespace"), DEFAULT_MEMORY_NAMESPACE),
        "origin": normalize_memory_origin(_safe_row_get(row, "origin"), DEFAULT_MEMORY_ORIGIN),
        "import_freshness": normalize_optional_string(_safe_row_get(row, "import_freshness")),
        "metadata": metadata if isinstance(metadata, dict) else {},
        "created_at": str(row["created_at"] or now_iso()),
        "updated_at": row["updated_at"],
        "deleted_at": deleted_at,
        "deletion_reason": deletion_reason,
        "superseded_by": row["superseded_by"],
        "content_hash": _safe_row_get(row, "content_hash"),
        "normalized_hash": _safe_row_get(row, "normalized_hash"),
        "token_count": _safe_row_get(row, "token_count"),
        "unique_token_count": _safe_row_get(row, "unique_token_count"),
        "top_terms_json": _safe_row_get(row, "top_terms_json"),
        "shingle_hashes_json": _safe_row_get(row, "shingle_hashes_json"),
        "signature_version": _safe_row_get(row, "signature_version"),
        "normalizer_version": _safe_row_get(row, "normalizer_version"),
        "signature_updated_at": _safe_row_get(row, "signature_updated_at"),
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
    payload = _event_payload_dict(data)
    if not event_id:
        digest = hashlib.sha1(
            f"{created}:{event_type}:{memory_id or ''}:{json.dumps(payload, sort_keys=True, ensure_ascii=False)}".encode(
                "utf-8"
            )
        ).hexdigest()[:16]
        event_id = f"evt_{digest}"
    record = _event_record_fields(memory_id, event_type, payload, created, event_id)
    _sqlite_enrich_event_record_from_memory(conn, record)
    conn.execute(
        """
        INSERT OR IGNORE INTO events(
            id, event_id, memory_id, event_type, action, source_id, target_id, relation,
            query_text, result_count, top_score, success, agent_id, role, domain, kind, summary,
            salience_text, include_in_salience, data_json, created_at, ts
        ) VALUES(
            :id, :event_id, :memory_id, :event_type, :action, :source_id, :target_id, :relation,
            :query_text, :result_count, :top_score, :success, :agent_id, :role, :domain, :kind, :summary,
            :salience_text, :include_in_salience, :data_json, :created_at, :ts
        )
        """,
        record,
    )
    _sqlite_sync_events_fts_for_record(conn, record)


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


def _git_repo_root() -> str:
    return str(workspace_root())


def _store_memory_file_rows(
    conn: sqlite3.Connection,
    *,
    memory_table: str,
    memory_id: str,
    touched_files: list[str],
    repo_root: str,
) -> None:
    for raw_path in touched_files:
        path_text = str(raw_path).strip()
        if not path_text:
            continue
        file_sha = file_sha_at_head(repo_root, path_text)
        if file_sha is None:
            file_sha = current_file_sha(repo_root, path_text)
        if not file_sha:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO memory_files(memory_table, memory_id, path, file_sha)
            VALUES(?, ?, ?, ?)
            """,
            (memory_table, memory_id, path_text, file_sha),
        )


def _sqlite_upsert_memory(
    conn: sqlite3.Connection,
    memory: dict[str, Any],
    *,
    respect_provided_git_on_new: bool = False,
    store_touched_files: bool = True,
) -> None:
    row = _memory_to_sqlite_row(memory)
    memory_id = str(row.get("id", "")).strip()
    existing_row = conn.execute("SELECT git_sha, git_branch, git_dirty FROM memories WHERE id = ?", (memory_id,)).fetchone()
    is_new_row = existing_row is None
    if is_new_row:
        keep_provided = bool(
            respect_provided_git_on_new
            and (
                row.get("git_sha") is not None
                or row.get("git_branch") is not None
                or row.get("git_dirty") is not None
            )
        )
        if not keep_provided:
            git_ctx = capture_git_context(_git_repo_root())
            row["git_sha"] = normalize_optional_string(git_ctx.get("sha"))
            row["git_branch"] = normalize_optional_string(git_ctx.get("branch"))
            row["git_dirty"] = normalize_git_dirty(git_ctx.get("dirty"))
    else:
        row["git_sha"] = normalize_optional_string(row.get("git_sha")) or normalize_optional_string(existing_row["git_sha"])
        row["git_branch"] = normalize_optional_string(row.get("git_branch")) or normalize_optional_string(existing_row["git_branch"])
        if row.get("git_dirty") is None:
            row["git_dirty"] = normalize_git_dirty(existing_row["git_dirty"])

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
            parent_id, source_run_id, git_sha, git_branch, git_dirty,
            namespace, origin, import_freshness,
            metadata_json, pinned, deleted, superseded_by,
            created_at, updated_at, token_estimate, content_hash,
            normalized_hash, token_count, unique_token_count,
            top_terms_json, shingle_hashes_json,
            signature_version, normalizer_version, signature_updated_at
        ) VALUES(
            :id, :kind, :text, :title, :preview, :source, :tags_json, :linked_ids_json,
            :agent_id, :role, :scope, :domain, :authority, :retention, :confidence,
            :parent_id, :source_run_id, :git_sha, :git_branch, :git_dirty,
            :namespace, :origin, :import_freshness,
            :metadata_json, :pinned, :deleted, :superseded_by,
            :created_at, :updated_at, :token_estimate, :content_hash,
            :normalized_hash, :token_count, :unique_token_count,
            :top_terms_json, :shingle_hashes_json,
            :signature_version, :normalizer_version, :signature_updated_at
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
            git_sha=excluded.git_sha,
            git_branch=excluded.git_branch,
            git_dirty=excluded.git_dirty,
            namespace=excluded.namespace,
            origin=excluded.origin,
            import_freshness=excluded.import_freshness,
            metadata_json=excluded.metadata_json,
            pinned=excluded.pinned,
            deleted=excluded.deleted,
            superseded_by=excluded.superseded_by,
            created_at=excluded.created_at,
            updated_at=excluded.updated_at,
            token_estimate=excluded.token_estimate,
            content_hash=excluded.content_hash,
            normalized_hash=excluded.normalized_hash,
            token_count=excluded.token_count,
            unique_token_count=excluded.unique_token_count,
            top_terms_json=excluded.top_terms_json,
            shingle_hashes_json=excluded.shingle_hashes_json,
            signature_version=excluded.signature_version,
            normalizer_version=excluded.normalizer_version,
            signature_updated_at=excluded.signature_updated_at
        """,
        row,
    )
    touched_files = normalize_touched_files(memory.get("_touched_files"))
    if is_new_row and touched_files and store_touched_files:
        _store_memory_file_rows(
            conn,
            memory_table=str(row.get("kind", "")),
            memory_id=memory_id,
            touched_files=touched_files,
            repo_root=_git_repo_root(),
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
            conn.execute("DELETE FROM memory_files WHERE memory_id = ?", (memory_id,))
            conn.execute("DELETE FROM memory_topics WHERE memory_id = ?", (memory_id,))
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


def tool_error_code(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "code": str(code),
        "message": str(message),
    }
    if isinstance(details, dict) and details:
        payload.update(details)
    return {
        "content": [{"type": "text", "text": f"Error [{code}]: {message}"}],
        "isError": True,
        "structuredContent": {"error": payload},
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
        "idf_cosine": float(getattr(breakdown, "idf_cosine", 0.0)),
        "idf_jaccard": float(getattr(breakdown, "idf_jaccard", 0.0)),
        "repetition": float(getattr(breakdown, "repetition", 0.0)),
        "recency": float(getattr(breakdown, "recency", 0.0)),
        "novelty": float(getattr(breakdown, "novelty", 0.0)),
        "drift": float(getattr(breakdown, "drift", 0.0)),
        "final": float(getattr(breakdown, "final", 0.0)),
        "weights": dict(getattr(breakdown, "weights", {})),
        "idf_status": str(getattr(breakdown, "idf_status", "not_requested")),
        "idf_used": bool(getattr(breakdown, "idf_used", False)),
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


def collapsed_preview_text(text: Any, max_chars: int = 200) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= max_chars else value[:max_chars]


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


def _imported_pack_namespaces(conn: sqlite3.Connection, trust_level: str) -> list[str]:
    try:
        rows = conn.execute(
            "SELECT DISTINCT namespace FROM imported_packs WHERE trust_level = ? ORDER BY namespace ASC",
            (trust_level,),
        ).fetchall()
    except Exception:
        return []
    out: list[str] = []
    for row in rows:
        value = normalize_optional_string(row[0] if not isinstance(row, sqlite3.Row) else row["namespace"])
        if value is not None:
            out.append(value)
    return out


def resolve_namespace_origin_filters(
    args: dict[str, Any],
    conn: sqlite3.Connection | None = None,
) -> tuple[list[str], list[str] | None]:
    has_namespace = "namespace" in args and args.get("namespace") is not None
    has_namespaces = "namespaces" in args and args.get("namespaces") is not None
    if has_namespace and has_namespaces:
        raise ValueError("namespace and namespaces cannot both be supplied")

    namespaces: list[str]
    explicit_namespace_filter = bool(has_namespace or has_namespaces)
    if has_namespace:
        namespace_value = normalize_optional_string(args.get("namespace"))
        if namespace_value is None:
            raise ValueError("namespace cannot be empty")
        namespaces = [namespace_value]
    elif has_namespaces:
        provided = normalize_optional_string_list(args.get("namespaces"), "namespaces") or []
        if not provided:
            raise ValueError("namespaces must contain at least one value")
        namespaces = list(provided)
    else:
        namespaces = [DEFAULT_MEMORY_NAMESPACE]

    include_imported = parse_bool(args.get("include_imported"), default=False)
    include_quarantine = parse_bool(args.get("include_quarantine"), default=False)
    if not explicit_namespace_filter:
        if conn is not None and include_imported:
            namespaces.extend(_imported_pack_namespaces(conn, "trusted"))
        if conn is not None and include_quarantine:
            namespaces.extend(_imported_pack_namespaces(conn, "quarantine"))

    deduped_namespaces: list[str] = []
    seen_namespaces: set[str] = set()
    for namespace_value in namespaces:
        value = normalize_optional_string(namespace_value)
        if value is None or value in seen_namespaces:
            continue
        seen_namespaces.add(value)
        deduped_namespaces.append(value)
    if not deduped_namespaces:
        deduped_namespaces = [DEFAULT_MEMORY_NAMESPACE]

    has_origin = "origin" in args and args.get("origin") is not None
    has_origins = "origins" in args and args.get("origins") is not None
    if has_origin and has_origins:
        raise ValueError("origin and origins cannot both be supplied")
    origins: list[str] | None = None
    if has_origin:
        origin_value = normalize_optional_string(args.get("origin"))
        if origin_value is None:
            raise ValueError("origin cannot be empty")
        origins = [origin_value]
    elif has_origins:
        provided_origins = normalize_optional_string_list(args.get("origins"), "origins") or []
        if not provided_origins:
            raise ValueError("origins must contain at least one value")
        origins = list(provided_origins)

    return deduped_namespaces, origins


def _memory_pack_metadata(memory: dict[str, Any]) -> dict[str, Any]:
    namespace_value = memory_namespace(memory)
    origin_value = memory_origin(memory)
    import_freshness_value = memory_import_freshness(memory)
    return {
        "namespace": namespace_value,
        "origin": origin_value,
        "import_freshness": import_freshness_value,
        "pack_id": derive_pack_id_from_namespace(namespace_value),
    }


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
    apply_scope_filter = (
        "_resolved_namespaces" in filters
        or ("namespace" in filters and filters.get("namespace") is not None)
        or ("namespaces" in filters and filters.get("namespaces") is not None)
        or ("origin" in filters and filters.get("origin") is not None)
        or ("origins" in filters and filters.get("origins") is not None)
    )
    namespaces: list[str] = [DEFAULT_MEMORY_NAMESPACE]
    if apply_scope_filter:
        namespaces_filter = filters.get("_resolved_namespaces")
        if not isinstance(namespaces_filter, list):
            namespace_single = normalize_optional_string(filters.get("namespace"))
            namespaces_filter = [namespace_single] if namespace_single else [DEFAULT_MEMORY_NAMESPACE]
        namespaces = [str(item) for item in namespaces_filter if str(item).strip()]
        if not namespaces:
            namespaces = [DEFAULT_MEMORY_NAMESPACE]
    origins_filter = filters.get("_resolved_origins")
    if origins_filter is not None and not isinstance(origins_filter, list):
        origin_single = normalize_optional_string(filters.get("origin"))
        origins_filter = [origin_single] if origin_single else None
    origins = [str(item) for item in origins_filter if str(item).strip()] if isinstance(origins_filter, list) else None

    out: list[dict[str, Any]] = []
    for memory in memories:
        if not visible_memory(memory, include_deleted, include_superseded):
            continue
        if apply_scope_filter and not _memory_in_scope(memory, namespaces, origins):
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
    alias_runtime: dict[str, Any] | None = None,
) -> list[tuple[float, dict[str, Any]]]:
    ranked: list[tuple[float, dict[str, Any]]] = []
    for memory in memories:
        base_score = score_memory(query_tokens, memory, phase)
        alias_concept_score, alias_concepts = _alias_concept_score_for_memory(memory, alias_runtime)
        score = base_score + alias_concept_score
        candidate_memory = memory
        if alias_concept_score > 0.0:
            candidate_memory = dict(memory)
            candidate_memory["_alias_concept_score"] = alias_concept_score
            candidate_memory["_alias_concepts"] = alias_concepts
        if score > 0 or not query_text:
            ranked.append((score, candidate_memory))
    ranked.sort(key=lambda item: (item[0], str(item[1].get("created_at", ""))), reverse=True)
    return ranked


def _quote_sqlite_fts_token(token: str) -> str:
    # SQLite FTS phrase quoting uses doubled internal quote characters.
    return '"' + token.replace('"', '""') + '"'


def _sqlite_fts_match_expression(query_tokens: set[str]) -> str:
    tokens = sorted(token for token in query_tokens if token)
    if not tokens:
        return ""
    limited = tokens[:24]
    quoted = [_quote_sqlite_fts_token(token) for token in limited]
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
    namespaces_filter = args.get("_resolved_namespaces")
    if not isinstance(namespaces_filter, list):
        namespace_value = normalize_optional_string(args.get("namespace"))
        namespaces_filter = [namespace_value] if namespace_value else [DEFAULT_MEMORY_NAMESPACE]
    namespaces = [str(item).strip() for item in namespaces_filter if str(item).strip()]
    if not namespaces:
        namespaces = [DEFAULT_MEMORY_NAMESPACE]
    namespace_placeholders = ",".join("?" for _ in namespaces)
    clauses.append(f"m.namespace IN ({namespace_placeholders})")
    params.extend(namespaces)

    origins_filter = args.get("_resolved_origins")
    origins: list[str] | None = None
    if isinstance(origins_filter, list):
        origins = [str(item).strip() for item in origins_filter if str(item).strip()]
    elif "origin" in args and args.get("origin") is not None:
        origin_value = normalize_optional_string(args.get("origin"))
        origins = [origin_value] if origin_value else []
    if origins:
        origin_placeholders = ",".join("?" for _ in origins)
        clauses.append(f"m.origin IN ({origin_placeholders})")
        params.extend(origins)

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




def _load_json_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        if isinstance(parsed, list):
            return [str(item) for item in parsed if str(item)]
    return []


def _memory_top_terms(memory: dict[str, Any], max_terms: int = DEFAULT_TOP_TERMS) -> list[str]:
    terms = _load_json_string_list(memory.get("top_terms_json"))
    if terms:
        return terms[:max_terms]
    return _build_top_terms(_normalize_for_signature(str(memory.get("text", ""))), max_terms)


def _metadata_compatible_for_duplicate(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if str(a.get("kind", "")) != str(b.get("kind", "")):
        return False
    for field in ("domain", "role", "agent_id"):
        av = normalize_optional_string(a.get(field))
        bv = normalize_optional_string(b.get(field))
        if av is not None and bv is not None and av != bv:
            return False
    return True


def _sqlite_fts_candidate_ids_for_memory(memory: dict[str, Any], *, limit: int, exclude_pinned: bool = True) -> list[str]:
    terms = _memory_top_terms(memory)
    match_expression = _sqlite_fts_match_expression(set(terms))
    if not match_expression:
        return []
    clauses = ["memories_fts MATCH ?", "m.deleted = 0", "(m.superseded_by IS NULL OR m.superseded_by = '')", "m.id <> ?"]
    params: list[Any] = [match_expression, str(memory.get("id", ""))]
    kind = normalize_optional_string(memory.get("kind"))
    if kind is not None:
        clauses.append("m.kind = ?")
        params.append(kind)
    if exclude_pinned:
        clauses.append("m.pinned = 0")
    sql = (
        "SELECT m.id "
        "FROM memories_fts "
        "JOIN memories m ON m.id = memories_fts.id "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY bm25(memories_fts), COALESCE(m.updated_at, m.created_at) DESC, m.id DESC "
        "LIMIT ?"
    )
    params.append(max(1, int(limit)))
    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            _sqlite_bootstrap_if_needed(conn)
            if not _sqlite_has_fts_table(conn) and not _sqlite_fts_available(conn):
                return []
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [str(row["id"]) for row in rows]
    except Exception:
        return []


def _cached_git_context(repo_root: str | None) -> dict[str, Any]:
    key = str(repo_root or "").strip()
    if not key:
        return {"sha": None, "branch": None, "dirty": None}
    cached = _GIT_CONTEXT_CACHE.get(key)
    if cached is not None:
        return cached
    ctx = capture_git_context(key)
    _GIT_CONTEXT_CACHE[key] = ctx
    return ctx


def freshness_multiplier(
    conn: sqlite3.Connection,
    memory_table: str,
    memory_id: int | str,
    repo_root: str | None,
) -> float:
    """Apply git freshness weighting to an already-scored memory candidate."""
    root = str(repo_root or "").strip()
    if not root:
        return 1.0
    try:
        ctx = _cached_git_context(root)
        if normalize_optional_string(ctx.get("sha")) is None:
            return 1.0

        row = conn.execute(
            "SELECT git_sha FROM memories WHERE id = ? AND kind = ?",
            (str(memory_id), str(memory_table)),
        ).fetchone()
        if row is None:
            row = conn.execute("SELECT git_sha FROM memories WHERE id = ?", (str(memory_id),)).fetchone()
        if row is None or normalize_optional_string(row["git_sha"]) is None:
            return 1.0

        file_rows = conn.execute(
            "SELECT path, file_sha FROM memory_files WHERE memory_table = ? AND memory_id = ?",
            (str(memory_table), str(memory_id)),
        ).fetchall()
        if not file_rows:
            return 1.0

        bucket = 1.0
        for file_row in file_rows:
            path_text = str(file_row["path"] or "").strip()
            stored_sha = str(file_row["file_sha"] or "").strip()
            current_sha = current_file_sha(root, path_text)
            if current_sha is None:
                bucket = min(bucket, 0.3)
                continue
            if stored_sha and current_sha != stored_sha:
                bucket = min(bucket, 0.7)
        return float(bucket)
    except Exception:
        return 1.0


def _apply_freshness_to_ranked(
    conn: sqlite3.Connection,
    ranked: list[tuple[float, dict[str, Any]]],
    repo_root: str | None,
) -> list[tuple[float, dict[str, Any]]]:
    adjusted: list[tuple[float, dict[str, Any]]] = []
    for score, memory in ranked:
        multiplier = freshness_multiplier(
            conn,
            str(memory.get("kind", "")),
            str(memory.get("id", "")),
            repo_root,
        )
        candidate = memory
        if multiplier != 1.0:
            candidate = dict(memory)
            candidate["_freshness_multiplier"] = multiplier
        adjusted.append((float(score) * float(multiplier), candidate))
    adjusted.sort(key=lambda item: (item[0], str(item[1].get("created_at", ""))), reverse=True)
    return adjusted


def search_rank(args: dict[str, Any], phase: str | None = None) -> list[dict[str, Any]]:
    query = str(args.get("query", "")).strip()
    expanded_query = normalize_optional_string(args.get("_expanded_query"))
    query_for_retrieval = expanded_query or query
    alias_runtime = args.get("_alias_runtime") if isinstance(args.get("_alias_runtime"), dict) else None
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
    search_filters = dict(args)
    if store_backend() == "sqlite":
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            _sqlite_bootstrap_if_needed(conn)
            resolved_namespaces, resolved_origins = resolve_namespace_origin_filters(args, conn)
    else:
        resolved_namespaces, resolved_origins = resolve_namespace_origin_filters(args, None)
    search_filters["_resolved_namespaces"] = list(resolved_namespaces)
    if resolved_origins is not None:
        search_filters["_resolved_origins"] = list(resolved_origins)

    query_tokens = tokenize(query_for_retrieval)
    candidates: list[dict[str, Any]] = []
    if store_backend() == "sqlite" and query_for_retrieval and _sqlite_fts_flag():
        candidates = _sqlite_fts_candidate_memories(
            search_filters,
            query_for_retrieval,
            include_deleted=include_deleted,
            include_superseded=include_superseded,
            limit=limit,
        )
    if not candidates:
        store = load_store()
        candidates = filter_memories(
            [memory for memory in store.get("memories", []) if isinstance(memory, dict)],
            search_filters,
            include_deleted=include_deleted,
            include_superseded=include_superseded,
        )
    ranked = rank_memories_for_query(
        candidates,
        query_tokens,
        phase=phase,
        query_text=query_for_retrieval,
        alias_runtime=alias_runtime,
    )
    if store_backend() == "sqlite" and ranked:
        try:
            with _sqlite_session() as conn:
                _sqlite_ensure_schema(conn)
                _sqlite_bootstrap_if_needed(conn)
                ranked = _apply_freshness_to_ranked(conn, ranked, _git_repo_root())
        except Exception:
            pass
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
    matched_alias_concepts = memory.get("_alias_concepts") if isinstance(memory.get("_alias_concepts"), list) else []
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
        **_memory_pack_metadata(memory),
        "metadata": memory.get("metadata", {}),
        "score": round(float(score), 3),
        "created_at": memory.get("created_at"),
        "updated_at": memory.get("updated_at"),
        "deleted_at": memory.get("deleted_at"),
        "deletion_reason": memory.get("deletion_reason"),
        "superseded_by": memory.get("superseded_by"),
        "alias_concept_score": round(float(memory.get("_alias_concept_score") or 0.0), 3),
        "alias_concepts": [str(item) for item in matched_alias_concepts if str(item).strip()],
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
        **_memory_pack_metadata(memory),
        "pinned": bool(memory.get("pinned", False)),
        "created_at": memory.get("created_at"),
        "updated_at": memory.get("updated_at"),
    }
    metadata = memory.get("metadata", {})
    if isinstance(metadata, dict) and metadata:
        item["metadata"] = metadata
    if score is not None:
        item["score"] = round(float(score), 3)
    alias_concept_score = float(memory.get("_alias_concept_score") or 0.0)
    if alias_concept_score > 0:
        item["alias_concept_score"] = round(alias_concept_score, 3)
        alias_concepts = memory.get("_alias_concepts") if isinstance(memory.get("_alias_concepts"), list) else []
        item["alias_concepts"] = [str(value) for value in alias_concepts if str(value).strip()]
    return item


def rank_against_query(
    memory: dict[str, Any],
    query: str,
    salience_module: Any | None = None,
    idf_profile: dict[str, Any] | None = None,
    alias_runtime: dict[str, Any] | None = None,
) -> float:
    runtime = alias_runtime if isinstance(alias_runtime, dict) else {}
    query_text = str(query).strip()
    if not query_text:
        return 0.0
    effective_query = str(runtime.get("expanded_query") or query_text).strip() or query_text
    alias_map = runtime.get("alias_map") if isinstance(runtime.get("alias_map"), dict) else {}
    alias_concept_score, _alias_concepts = _alias_concept_score_for_memory(memory, runtime)
    base_score = 0.0
    if salience_module is not None:
        try:
            kwargs: dict[str, Any] = {}
            if isinstance(idf_profile, dict):
                kwargs = {
                    "mode": "auto",
                    "idf_profile": idf_profile,
                    "weights": dict(IDF_ACTIVE_WEIGHTS),
                }
            if alias_map:
                kwargs["alias_map"] = dict(alias_map)
            try:
                breakdown = salience_module.signal_score(effective_query, str(memory.get("text", "")), **kwargs)
            except TypeError:
                kwargs.pop("alias_map", None)
                breakdown = salience_module.signal_score(effective_query, str(memory.get("text", "")), **kwargs)
            base_score = max(0.0, min(1.0, float(getattr(breakdown, "final", 0.0))))
        except Exception:
            base_score = 0.0
    if base_score <= 0.0:
        tokens = tokenize(effective_query)
        base_score = max(0.0, float(score_memory(tokens, memory, phase=None)))
    final_score = max(0.0, min(1.0, base_score + alias_concept_score))
    return final_score


def select_memories_by_query(
    memories: list[dict[str, Any]],
    query: str,
    limit: int,
    salience_module: Any | None = None,
    idf_profile: dict[str, Any] | None = None,
    alias_runtime: dict[str, Any] | None = None,
) -> list[tuple[float, dict[str, Any]]]:
    scored: list[tuple[float, dict[str, Any]]] = []
    for memory in memories:
        score = rank_against_query(memory, query, salience_module, idf_profile, alias_runtime)
        alias_concept_score, alias_concepts = _alias_concept_score_for_memory(memory, alias_runtime)
        candidate = memory
        if alias_concept_score > 0.0:
            candidate = dict(memory)
            candidate["_alias_concept_score"] = alias_concept_score
            candidate["_alias_concepts"] = alias_concepts
        if score > 0.0 or not query.strip():
            scored.append((score, candidate))
    if store_backend() == "sqlite" and scored:
        try:
            with _sqlite_session() as conn:
                _sqlite_ensure_schema(conn)
                _sqlite_bootstrap_if_needed(conn)
                scored = _apply_freshness_to_ranked(conn, scored, _git_repo_root())
        except Exception:
            pass
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
    *,
    query_text: str | None = None,
    domain: str | None = None,
    result_count: int | None = None,
    top_score: float | None = None,
    success: int | None = None,
    include_in_salience: bool | None = None,
    summary: str | None = None,
    action: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    if not query_logging_enabled():
        return
    derived_top_score = top_score
    if derived_top_score is None:
        if matches:
            derived_top_score = _event_float_value(matches[0].get("score"))
        if derived_top_score is None:
            derived_top_score = 0.0
    if result_count is None:
        n_results = len(matches)
    else:
        try:
            n_results = max(0, int(result_count))
        except (TypeError, ValueError):
            n_results = len(matches)
    derived_success = success
    if derived_success is None:
        threshold = miss_top_score_threshold()
        derived_success = 0 if n_results == 0 or float(derived_top_score) < threshold else 1
    derived_include = include_in_salience
    if derived_include is None and int(derived_success) == 0:
        derived_include = True

    top_ids: list[str] = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        identifier = match.get("id")
        if not identifier:
            identifier = match.get("memory_id")
        if identifier:
            top_ids.append(str(identifier))

    row = {
        "ts": now_iso(),
        "tool": tool,
        "args": scrub_secret_params(args),
        "top_ids": top_ids,
        "top_score": float(derived_top_score),
        "n_results": n_results,
        "result_count": n_results,
        "success": int(derived_success),
    }
    resolved_query_text = normalize_optional_string(query_text) or normalize_optional_string(args.get("query"))
    if resolved_query_text is not None:
        row["query_text"] = resolved_query_text
    resolved_domain = normalize_optional_string(domain) or normalize_optional_string(args.get("domain"))
    if resolved_domain is not None:
        row["domain"] = resolved_domain
    if summary is not None:
        row["summary"] = str(summary)
    if action is not None:
        row["action"] = str(action)
    if derived_include is not None:
        row["include_in_salience"] = bool(derived_include)
    if extra:
        for key, value in extra.items():
            if key in row:
                continue
            row[str(key)] = value
    if phase is not None or tool in {"mnemo_search", "mnemo_compact_context"}:
        row["phase"] = phase

    row = scrub_secret_params(row)

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
    scrubbed_details = _event_payload_dict(details)
    if store_backend() == "sqlite":
        try:
            with _sqlite_session() as conn:
                _sqlite_ensure_schema(conn)
                _sqlite_bootstrap_if_needed(conn)
                _sqlite_insert_event(conn, memory_id, event, scrubbed_details, now_iso())
        except Exception:
            pass
        return

    path = events_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= EVENT_LOG_MAX_BYTES:
            _rotate_event_log(path)
        row = {"ts": now_iso(), "event": event, "id": memory_id, "details": scrubbed_details}
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
                    """
                    SELECT memory_id, event_type, data_json, COALESCE(ts, created_at) AS ts
                    FROM events
                    WHERE event_type != 'query'
                    ORDER BY COALESCE(ts, created_at) ASC, rowid ASC
                    """
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
                        "ts": str(row["ts"] or ""),
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


def _clamp_event_limit(value: Any, default: int = DEFAULT_EVENT_LIMIT) -> int:
    try:
        raw = default if value is None else int(value)
    except (TypeError, ValueError):
        raw = default
    return max(1, min(raw, MAX_EVENT_LIMIT))


def _event_output_row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(record.get("event_id") or record.get("id") or ""),
        "memory_id": normalize_optional_string(record.get("memory_id")),
        "timestamp": str(record.get("ts") or record.get("created_at") or ""),
        "action": str(record.get("action") or record.get("event_type") or ""),
        "event_type": str(record.get("event_type") or record.get("action") or ""),
        "kind": normalize_optional_string(record.get("kind")),
        "domain": normalize_optional_string(record.get("domain")),
        "role": normalize_optional_string(record.get("role")),
        "agent_id": normalize_optional_string(record.get("agent_id")),
        "summary": normalize_optional_string(record.get("summary")),
        "salience_text": normalize_optional_string(record.get("salience_text")),
        "source_id": normalize_optional_string(record.get("source_id")),
        "target_id": normalize_optional_string(record.get("target_id")),
        "relation": normalize_optional_string(record.get("relation")),
        "query_text": normalize_optional_string(record.get("query_text")),
        "result_count": _event_int_value(record.get("result_count")),
        "top_score": _event_float_value(record.get("top_score")),
        "success": _event_int_value(record.get("success")),
        "include_in_salience": _event_int_value(record.get("include_in_salience")),
    }


def _event_output_row_full(record: dict[str, Any]) -> dict[str, Any]:
    out = _event_output_row(record)
    data_raw = record.get("data_json")
    if isinstance(data_raw, str):
        try:
            parsed = json.loads(data_raw)
        except json.JSONDecodeError:
            parsed = {"raw": data_raw}
    elif isinstance(data_raw, dict):
        parsed = data_raw
    else:
        parsed = {}
    out["data"] = parsed if isinstance(parsed, dict) else {"value": parsed}
    return out


def _sqlite_event_row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    data_json = str(_safe_row_get(row, "data_json") or "{}")
    try:
        payload = json.loads(data_json)
    except json.JSONDecodeError:
        payload = {"raw": data_json}
    if not isinstance(payload, dict):
        payload = {"value": payload}

    event_id = str(_safe_row_get(row, "event_id") or _safe_row_get(row, "id") or "")
    event_type = str(_safe_row_get(row, "event_type") or _safe_row_get(row, "action") or "event")
    ts = str(_safe_row_get(row, "ts") or _safe_row_get(row, "created_at") or now_iso())
    memory_id = normalize_optional_string(_safe_row_get(row, "memory_id"))
    base = _event_record_fields(memory_id, event_type, payload, ts, event_id)

    for key in (
        "action",
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
    ):
        value = _safe_row_get(row, key)
        if value is None:
            continue
        base[key] = value
    base["id"] = str(_safe_row_get(row, "id") or event_id)
    base["event_id"] = str(base.get("event_id") or event_id)
    base["created_at"] = str(_safe_row_get(row, "created_at") or ts)
    base["ts"] = str(base.get("ts") or ts)
    base["data_json"] = data_json
    return base


def _legacy_event_rows(include_archive: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in read_event_rows(include_archive=include_archive):
        created = str(row.get("ts", "")).strip() or now_iso()
        memory_id = normalize_optional_string(row.get("id"))
        event_type = str(row.get("event", "")).strip() or "event"
        details = row.get("details")
        payload = details if isinstance(details, dict) else {"details": details}
        digest = hashlib.sha1(
            f"{created}:{event_type}:{memory_id or ''}:{json.dumps(payload, sort_keys=True, ensure_ascii=False)}".encode("utf-8")
        ).hexdigest()[:16]
        event_id = f"evt_{digest}"
        rows.append(_event_record_fields(memory_id, event_type, payload, created, event_id))

    query_paths = [query_log_path().with_name("queries.1.jsonl"), query_log_path()]
    if include_archive:
        query_paths.insert(0, query_archive_path())
    for path in query_paths:
        for row in _read_jsonl_rows(path):
            created = str(row.get("ts", "")).strip() or now_iso()
            payload = dict(row)
            event_type = "query"
            digest = hashlib.sha1(
                f"{created}:{event_type}:{json.dumps(payload, sort_keys=True, ensure_ascii=False)}".encode("utf-8")
            ).hexdigest()[:16]
            event_id = f"evt_{digest}"
            rows.append(_event_record_fields(None, event_type, payload, created, event_id))
    rows.sort(key=lambda row: (str(row.get("ts") or ""), str(row.get("event_id") or "")), reverse=True)
    return rows


def _sqlite_recent_event_records(
    *,
    limit: int,
    action: str | None = None,
    kind: str | None = None,
    domain: str | None = None,
    memory_id: str | None = None,
    event_id: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if action:
        clauses.append("COALESCE(action, event_type) = ?")
        params.append(action)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if domain:
        clauses.append("domain = ?")
        params.append(domain)
    if memory_id:
        clauses.append("memory_id = ?")
        params.append(memory_id)
    if event_id:
        clauses.append("(event_id = ? OR id = ?)")
        params.extend([event_id, event_id])
    sql = "SELECT * FROM events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY COALESCE(ts, created_at) DESC, rowid DESC LIMIT ?"
    params.append(limit)
    with _sqlite_session() as conn:
        _sqlite_ensure_schema(conn)
        _sqlite_bootstrap_if_needed(conn)
        rows = conn.execute(sql, params).fetchall()
    return [_sqlite_event_row_to_record(row) for row in rows]


def _event_matches_query(record: dict[str, Any], query_terms: set[str]) -> bool:
    if not query_terms:
        return True
    fields = [
        str(record.get("action") or ""),
        str(record.get("event_type") or ""),
        str(record.get("memory_id") or ""),
        str(record.get("query_text") or ""),
        str(record.get("summary") or ""),
        str(record.get("salience_text") or ""),
        str(record.get("domain") or ""),
        str(record.get("role") or ""),
        str(record.get("agent_id") or ""),
        str(record.get("source_id") or ""),
        str(record.get("target_id") or ""),
        str(record.get("relation") or ""),
    ]
    haystack = " ".join(fields)
    hay_tokens = tokenize(haystack)
    return bool(query_terms & hay_tokens)


def recent_events(args: dict[str, Any]) -> dict[str, Any]:
    limit = _clamp_event_limit(args.get("limit"), default=DEFAULT_EVENT_LIMIT)
    action = normalize_optional_string(args.get("action"))
    kind = normalize_optional_string(args.get("kind"))
    domain = normalize_optional_string(args.get("domain"))
    try:
        if store_backend() == "sqlite":
            rows = _sqlite_recent_event_records(limit=limit, action=action, kind=kind, domain=domain)
        else:
            rows = _legacy_event_rows(include_archive=False)
            if action:
                rows = [row for row in rows if str(row.get("action") or row.get("event_type") or "") == action]
            if kind:
                rows = [row for row in rows if normalize_optional_string(row.get("kind")) == kind]
            if domain:
                rows = [row for row in rows if normalize_optional_string(row.get("domain")) == domain]
            rows = rows[:limit]
    except PackSelectorError as exc:
        return tool_error_code(exc.code, exc.message)
    except PackSelectorError as exc:
        return tool_error_code(exc.code, exc.message)
    except PackSelectorError as exc:
        return tool_error_code(exc.code, exc.message)
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")

    compact = [_event_output_row(row) for row in rows]
    text_lines = [f"Recent events ({len(compact)}):"]
    for item in compact:
        text_lines.append(
            f"- {item['timestamp']} {item['action']} event_id={item['event_id']} memory_id={item.get('memory_id') or '-'}"
        )
    return text_result("\n".join(text_lines), {"events": compact, "count": len(compact)})


def memory_events(args: dict[str, Any]) -> dict[str, Any]:
    memory_id = str(args.get("memory_id", "")).strip()
    if not memory_id:
        return tool_error("memory_id is required")
    limit = _clamp_event_limit(args.get("limit"), default=50)
    try:
        if store_backend() == "sqlite":
            rows = _sqlite_recent_event_records(limit=limit, memory_id=memory_id)
        else:
            rows = [row for row in _legacy_event_rows(include_archive=False) if str(row.get("memory_id") or "") == memory_id]
            rows = rows[:limit]
    except PackSelectorError as exc:
        return tool_error_code(exc.code, exc.message)
    except PackSelectorError as exc:
        return tool_error_code(exc.code, exc.message)
    except PackSelectorError as exc:
        return tool_error_code(exc.code, exc.message)
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")

    compact = [_event_output_row(row) for row in rows]
    text_lines = [f"Events for memory {memory_id} ({len(compact)}):"]
    for item in compact:
        text_lines.append(f"- {item['timestamp']} {item['action']} event_id={item['event_id']}")
    return text_result("\n".join(text_lines), {"memory_id": memory_id, "events": compact, "count": len(compact)})


def get_event(args: dict[str, Any]) -> dict[str, Any]:
    event_id = str(args.get("event_id", "")).strip()
    if not event_id:
        return tool_error("event_id is required")
    try:
        if store_backend() == "sqlite":
            rows = _sqlite_recent_event_records(limit=1, event_id=event_id)
        else:
            rows = [row for row in _legacy_event_rows(include_archive=True) if str(row.get("event_id") or "") == event_id]
            rows = rows[:1]
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")
    if not rows:
        return tool_error(f"event not found: {event_id}")
    full = _event_output_row_full(rows[0])
    return text_result(
        f"Event {event_id}: {full.get('action')} {full.get('timestamp')}",
        {"event": full},
    )


def _search_events_sqlite(
    query: str,
    limit: int,
    action: str | None = None,
    domain: str | None = None,
) -> list[dict[str, Any]]:
    tokens = tokenize(query)
    with _sqlite_session() as conn:
        _sqlite_ensure_schema(conn)
        _sqlite_bootstrap_if_needed(conn)
        use_fts = bool(tokens) and _sqlite_events_fts_flag()
        if use_fts:
            try:
                match_expression = _sqlite_fts_match_expression(tokens)
                clauses = ["events_fts MATCH ?"]
                params: list[Any] = [match_expression]
                if action:
                    clauses.append("COALESCE(e.action, e.event_type) = ?")
                    params.append(action)
                if domain:
                    clauses.append("e.domain = ?")
                    params.append(domain)
                sql = (
                    "SELECT e.* FROM events_fts "
                    "JOIN events e ON e.event_id = events_fts.event_id "
                    "WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY COALESCE(e.ts, e.created_at) DESC, e.rowid DESC LIMIT ?"
                )
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()
                return [_sqlite_event_row_to_record(row) for row in rows]
            except sqlite3.OperationalError:
                _sqlite_set_meta(conn, "events_fts_available", "0")

        clauses = []
        params = []
        if action:
            clauses.append("COALESCE(action, event_type) = ?")
            params.append(action)
        if domain:
            clauses.append("domain = ?")
            params.append(domain)
        base = "SELECT * FROM events"
        if clauses:
            base += " WHERE " + " AND ".join(clauses)
        base += " ORDER BY COALESCE(ts, created_at) DESC, rowid DESC LIMIT ?"
        params.append(max(limit * 6, limit))
        rows = conn.execute(base, params).fetchall()
    records = [_sqlite_event_row_to_record(row) for row in rows]
    terms = tokenize(query)
    filtered = [record for record in records if _event_matches_query(record, terms)]
    return filtered[:limit]


def search_events(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    if not query:
        return tool_error("query is required")
    limit = _clamp_event_limit(args.get("limit"), default=DEFAULT_EVENT_LIMIT)
    action = normalize_optional_string(args.get("action"))
    domain = normalize_optional_string(args.get("domain"))
    try:
        if store_backend() == "sqlite":
            rows = _search_events_sqlite(query, limit, action=action, domain=domain)
        else:
            rows = _legacy_event_rows(include_archive=True)
            if action:
                rows = [row for row in rows if str(row.get("action") or row.get("event_type") or "") == action]
            if domain:
                rows = [row for row in rows if normalize_optional_string(row.get("domain")) == domain]
            terms = tokenize(query)
            rows = [row for row in rows if _event_matches_query(row, terms)]
            rows = rows[:limit]
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")

    compact = [_event_output_row(row) for row in rows[:limit]]
    lines = [f"Event search matches ({len(compact)}):"]
    for item in compact:
        lines.append(
            f"- {item['timestamp']} {item['action']} event_id={item['event_id']} summary={item.get('summary') or ''}"
        )
    return text_result("\n".join(lines), {"query": query, "events": compact, "count": len(compact)})


def search_memories(args: dict[str, Any]) -> dict[str, Any]:
    try:
        query = str(args.get("query", "")).strip()
        domain = normalize_optional_string(args.get("domain"))
        language = _normalize_alias_language(args.get("language"))
        alias_runtime = _expand_query_with_aliases(query, domain=domain, language=language)
        search_args = dict(args)
        search_args["_alias_runtime"] = alias_runtime
        search_args["_expanded_query"] = str(alias_runtime.get("expanded_query") or query)
        phase_label, phase = resolve_phase(args, query)
        matches = search_rank(search_args, phase)
        matches, cap_warnings = cap_match_items(matches)
        alias_diag = _alias_diagnostics_payload(alias_runtime, matches)
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
                **alias_diag,
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
            **alias_diag,
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
        candidate_limit = max(1, min(int(args.get("candidate_limit") or 500), 5000))
        max_scored = max(1, min(int(args.get("max_scored") or 100), candidate_limit))
        min_token_count = max(1, int(args.get("min_token_count") or 5))
        use_fts = parse_bool(args.get("use_fts"), default=True)
        raw_shingle_overlap_threshold = args.get("shingle_overlap_threshold")
        shingle_overlap_threshold = 0.30 if raw_shingle_overlap_threshold is None else float(raw_shingle_overlap_threshold)
        shingle_overlap_threshold = max(0.0, min(1.0, shingle_overlap_threshold))
        if store_backend() == "sqlite":
            with _sqlite_session() as conn:
                _sqlite_ensure_schema(conn)
                _sqlite_bootstrap_if_needed(conn)
                resolved_namespaces, resolved_origins = resolve_namespace_origin_filters(args, conn)
        else:
            resolved_namespaces, resolved_origins = resolve_namespace_origin_filters(args, None)
    except Exception as exc:
        return tool_error(str(exc))

    salience, reason = load_optional_agent_salience()
    if salience is None:
        return salience_unavailable_result(reason)
    idf_state = _ensure_idf_profiles(trigger="salience_check")
    idf_selection = _resolve_idf_profile_for_memory_or_query(
        domain=normalize_optional_string(args.get("domain")),
        idf_state=idf_state,
    )
    active_idf_profile = dict(idf_selection["profile"]) if isinstance(idf_selection.get("profile"), dict) else None
    use_idf = bool(idf_selection.get("active")) and active_idf_profile is not None
    query_domain = normalize_optional_string(args.get("domain"))
    query_language = _normalize_alias_language(args.get("language"))
    alias_runtime = _expand_query_with_aliases(text, domain=query_domain, language=query_language)
    expanded_text = str(alias_runtime.get("expanded_query") or text)
    alias_map = alias_runtime.get("alias_map") if isinstance(alias_runtime.get("alias_map"), dict) else {}

    input_sig = _build_memory_signature(expanded_text)
    input_shingles = _load_json_string_list(input_sig.get("shingle_hashes_json"))
    input_token_count = int(input_sig.get("token_count") or 0)

    candidate_source = "fallback"
    fts_used = False
    fts_available = _sqlite_fts_flag() if store_backend() == "sqlite" else False
    candidates: list[dict[str, Any]] = []

    if store_backend() == "sqlite" and use_fts and fts_available:
        candidate_args = dict(args)
        candidate_args["_resolved_namespaces"] = list(resolved_namespaces)
        if resolved_origins is not None:
            candidate_args["_resolved_origins"] = list(resolved_origins)
        fts_query = " ".join(_load_json_string_list(input_sig.get("top_terms_json")))
        candidates = _sqlite_fts_candidate_memories(
            candidate_args,
            fts_query,
            include_deleted=include_deleted,
            include_superseded=include_superseded,
            limit=candidate_limit,
        )[:candidate_limit]
        if candidates:
            candidate_source = "fts5"
            fts_used = True

    if not candidates:
        store = load_store()
        all_visible = [
            memory
            for memory in store.get("memories", [])
            if visible_memory(memory, include_deleted, include_superseded)
            and _memory_in_scope(memory, list(resolved_namespaces), resolved_origins)
        ]
        # Bounded fallback chain: filter by metadata, shared top terms, then recent window.
        wanted_terms = set(_load_json_string_list(input_sig.get("top_terms_json")))
        ranked: list[tuple[int, str, dict[str, Any]]] = []
        for memory in all_visible:
            if normalize_optional_string(args.get("kind")) and str(memory.get("kind")) != normalize_optional_string(args.get("kind")):
                continue
            for field in ("role", "agent_id", "domain", "scope", "source_run_id"):
                wanted = normalize_optional_string(args.get(field))
                if wanted is not None and normalize_optional_string(memory.get(field)) != wanted:
                    break
            else:
                mem_terms = set(_memory_top_terms(memory))
                overlap = len(wanted_terms & mem_terms) if wanted_terms and mem_terms else 0
                if overlap > 0:
                    ranked.append((overlap, str(memory.get("created_at") or ""), memory))
        ranked.sort(key=lambda item: (item[0], item[1], str(item[2].get("id", ""))), reverse=True)
        candidates = [item[2] for item in ranked[:candidate_limit]]
        if not candidates:
            candidates = all_visible[-candidate_limit:]
        candidate_source = "signature" if ranked else "fallback"

    scored_candidates: list[tuple[dict[str, Any], float]] = []
    if input_token_count >= min_token_count and input_shingles:
        for memory in candidates:
            cand_token_count = int(memory.get("token_count") or 0)
            cand_shingles = _load_json_string_list(memory.get("shingle_hashes_json"))
            if cand_token_count < min_token_count or not cand_shingles:
                continue
            overlap = _signature_overlap(input_shingles, cand_shingles)
            if overlap >= shingle_overlap_threshold:
                scored_candidates.append((memory, overlap))
            if len(scored_candidates) >= max_scored:
                break
    if not scored_candidates:
        # Tiny inputs / missing signatures still get bounded scoring, never all-memory scoring.
        scored_candidates = [(memory, 0.0) for memory in candidates[:max_scored]]

    matches: list[dict[str, Any]] = []
    for memory, overlap in scored_candidates[:max_scored]:
        memory_text = str(memory.get("text", ""))
        kwargs: dict[str, Any] = {}
        if use_idf and active_idf_profile is not None:
            kwargs.update(
                {
                    "mode": "auto",
                    "idf_profile": active_idf_profile,
                    "weights": dict(IDF_ACTIVE_WEIGHTS),
                }
            )
        if alias_map:
            kwargs["alias_map"] = dict(alias_map)
        try:
            breakdown = salience.signal_score(expanded_text, memory_text, **kwargs)
        except TypeError:
            kwargs.pop("alias_map", None)
            breakdown = salience.signal_score(expanded_text, memory_text, **kwargs)
        base_score = max(0.0, min(1.0, float(getattr(breakdown, "final", 0.0))))
        alias_concept_score, alias_concepts = _alias_concept_score_for_memory(memory, alias_runtime)
        score = max(0.0, min(1.0, base_score + alias_concept_score))
        triggered = score >= threshold
        matches.append(
            {
                "memory_id": str(memory.get("id", "")),
                "kind": str(memory.get("kind", "")),
                **_memory_pack_metadata(memory),
                "text_preview": memory_text[:200],
                "score": round(score, 3),
                "base_score": round(base_score, 3),
                "alias_concept_score": round(alias_concept_score, 3),
                "alias_concepts": alias_concepts,
                "triggered": triggered,
                "margin": round(score - threshold, 3),
                "shingle_overlap": round(overlap, 4),
                "candidate_source": candidate_source,
                "breakdown": salience_breakdown_payload(breakdown),
            }
        )
    matches.sort(key=lambda item: (float(item["score"]), str(item["memory_id"])), reverse=True)
    top_matches = matches[:limit]

    warnings: list[str] = []
    if not top_matches:
        warnings.append("No visible memories available for salience comparison.")
    if store_backend() == "sqlite" and use_fts and not fts_available:
        warnings.append("SQLite FTS5 unavailable; salience_check used bounded signature fallback.")

    anchors = [
        str(memory.get("text", ""))
        for memory, _overlap in scored_candidates
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
    idf_used = any(bool((item.get("breakdown") or {}).get("idf_used")) for item in top_matches)
    top_breakdown = (top_matches[0].get("breakdown") if top_matches else {}) if top_matches else {}
    alias_diag = _alias_diagnostics_payload(alias_runtime, top_matches)
    explanation = (
        f"{triggered_count}/{len(top_matches)} top matches met threshold {threshold:.2f}."
        if top_matches
        else f"No matches available at threshold {threshold:.2f}."
    )
    structured: dict[str, Any] = {
        "available": True,
        "triggered": triggered_count > 0,
        "threshold": threshold,
        "candidate_source": candidate_source,
        "fts_available": fts_available,
        "fts_used": fts_used,
        "candidate_limit": candidate_limit,
        "max_scored": max_scored,
        "candidates_considered": len(candidates),
        "scored_count": len(scored_candidates[:max_scored]),
        "matches": top_matches,
        "idf_status": str(idf_selection.get("status", "cold")),
        "idf_used": idf_used,
        "idf_scope_used": str(idf_selection.get("scope", "none")) if idf_used else "none",
        "idf_profile_status": str(idf_selection.get("status", "cold")),
        "score_breakdown": {
            "cosine": float((top_breakdown or {}).get("cosine", 0.0)),
            "jaccard": float((top_breakdown or {}).get("jaccard", 0.0)),
            "idf_cosine": float((top_breakdown or {}).get("idf_cosine", 0.0)),
            "idf_jaccard": float((top_breakdown or {}).get("idf_jaccard", 0.0)),
        },
        "score_weights": dict((top_breakdown or {}).get("weights", {})) if isinstance(top_breakdown, dict) else {},
        "idf": {
            "mode": str(idf_state.get("mode", idf_mode())),
            "available": bool(idf_state.get("available", False)),
            "scope": str(idf_selection.get("scope", "project")),
            "name": str(idf_selection.get("name", "default")),
            "status": str(idf_selection.get("status", "cold")),
            "active": bool(idf_selection.get("active", False)),
        },
        **alias_diag,
        "warnings": warnings,
        "explanation": explanation,
    }
    if max_anchor_drift is not None:
        structured["anchor_drift"] = round(max_anchor_drift, 3)
    lines = [
        f"Salience check threshold: {threshold:.2f}",
        f"Candidate source: {candidate_source}",
        f"Scored candidates: {structured['scored_count']}",
        f"IDF: status={structured['idf_status']} used={'yes' if structured['idf_used'] else 'no'} scope={structured['idf_scope_used']} mode={structured['idf']['mode']}",
        f"Aliases: used={'yes' if structured['aliases_used'] else 'no'} concepts={len(structured['alias_concepts_matched'])} expansions={int(structured['alias_candidate_expansion_count'])}",
        f"Triggered matches: {triggered_count}/{len(top_matches)}",
        explanation,
    ]
    if warnings:
        lines.append("Warnings: " + "; ".join(warnings))
    top_score = max((float(item.get("score") or 0.0) for item in top_matches), default=0.0)
    event_matches = [{"id": item.get("memory_id"), "score": item.get("score", 0.0)} for item in top_matches]
    miss = triggered_count == 0
    append_query_log(
        "mnemo_salience_check",
        args,
        event_matches,
        query_text=text,
        domain=normalize_optional_string(args.get("domain")),
        result_count=triggered_count,
        top_score=top_score,
        success=0 if miss else 1,
        include_in_salience=True if miss else None,
        summary=(
            f"mnemo_salience_check: threshold={threshold:.2f} triggered={triggered_count} "
            f"scored={len(top_matches)} top_score={top_score:.3f}"
        ),
        action="mnemo_salience_check",
        extra={
            "threshold": threshold,
            "scored_count": len(top_matches),
            "candidate_source": candidate_source,
            "aliases_used": bool(alias_diag.get("aliases_used")),
            "alias_candidate_expansion_count": int(alias_diag.get("alias_candidate_expansion_count", 0)),
        },
    )
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
        namespace = normalize_memory_namespace(args.get("namespace"), DEFAULT_MEMORY_NAMESPACE)
        origin = normalize_memory_origin(args.get("origin"), DEFAULT_MEMORY_ORIGIN)
        touched_files = normalize_touched_files(args.get("touched_files"))
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

            sig = _build_memory_signature(text)
            new_content_hash = sig["content_hash"]
            new_normalized_hash = sig["normalized_hash"]
            new_shingle_hashes = json.loads(sig["shingle_hashes_json"])
            new_token_count = sig["token_count"]

            near_duplicate_of: list[str] = []
            shingle_overlap_threshold = 0.30
            similarity_threshold = float(os.environ.get("MNEMO_CONSOLIDATE_THRESHOLD", "0.7"))

            candidates = duplicate_candidates(memories, kind, supersedes_id)

            # Pre-compute signatures for candidates that don't have them stored (e.g. JSON mode or pre-backfill)
            _cand_sigs: dict[str, dict[str, Any]] = {}
            for memory in candidates:
                if not memory.get("content_hash") or not memory.get("normalized_hash"):
                    _cand_sigs[str(memory.get("id", ""))] = _build_memory_signature(str(memory.get("text", "")))

            def _cand_content_hash(m: dict[str, Any]) -> str | None:
                stored = m.get("content_hash")
                if stored:
                    return stored
                return _cand_sigs.get(str(m.get("id", "")), {}).get("content_hash")

            def _cand_normalized_hash(m: dict[str, Any]) -> str | None:
                stored = m.get("normalized_hash")
                if stored:
                    return stored
                return _cand_sigs.get(str(m.get("id", "")), {}).get("normalized_hash")

            def _cand_shingles(m: dict[str, Any]) -> list[str]:
                raw = m.get("shingle_hashes_json")
                if raw:
                    try:
                        return json.loads(raw)
                    except Exception:
                        pass
                cached = _cand_sigs.get(str(m.get("id", "")), {}).get("shingle_hashes_json")
                if cached:
                    try:
                        return json.loads(cached)
                    except Exception:
                        pass
                return []

            def _cand_token_count(m: dict[str, Any]) -> int:
                tc = m.get("token_count")
                if tc is not None:
                    return int(tc)
                return _cand_sigs.get(str(m.get("id", "")), {}).get("token_count") or 0

            # Step 2: exact content_hash short-circuit
            for memory in candidates:
                if _cand_content_hash(memory) == new_content_hash:
                    structured = {
                        "recorded": False,
                        "duplicate": True,
                        "duplicate_type": "content_hash",
                        "existing_id": str(memory.get("id")),
                        "memory": memory,
                        "memory_file": str(memory_path()),
                    }
                    return text_result(
                        f"Duplicate {kind} memory (content_hash) already exists as {memory.get('id')}.",
                        structured,
                    )

            # Step 3: normalized_hash short-circuit
            for memory in candidates:
                if _cand_normalized_hash(memory) == new_normalized_hash:
                    structured = {
                        "recorded": False,
                        "duplicate": True,
                        "duplicate_type": "normalized_hash",
                        "existing_id": str(memory.get("id")),
                        "memory": memory,
                        "memory_file": str(memory_path()),
                    }
                    return text_result(
                        f"Duplicate {kind} memory (normalized_hash) already exists as {memory.get('id')}.",
                        structured,
                    )

            # Step 4: signature-based near-duplicate detection
            if new_token_count >= 5 and new_shingle_hashes:
                salience, _reason = load_optional_agent_salience()
                for memory in candidates:
                    cand_tc = _cand_token_count(memory)
                    cand_shingles = _cand_shingles(memory)
                    if cand_tc < 5 or not cand_shingles:
                        continue
                    overlap = _signature_overlap(new_shingle_hashes, cand_shingles)
                    if overlap < shingle_overlap_threshold:
                        continue
                    if salience is not None:
                        breakdown = salience.signal_score(text, str(memory.get("text", "")))
                        sim = max(0.0, min(1.0, float(getattr(breakdown, "final", 0.0))))
                    else:
                        sim = _jaccard_similarity_fallback(
                            set(_normalize_for_signature(text)),
                            set(_normalize_for_signature(str(memory.get("text", "")))),
                        )
                    if sim >= similarity_threshold:
                        near_duplicate_of.append(str(memory.get("id")))

            ok_cap, cap_error = enforce_size_cap(store)
            if not ok_cap:
                return tool_error(cap_error or "memory cap reached")
            memories = store.setdefault("memories", [])

            git_ctx = capture_git_context(_git_repo_root())
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
                touched_files=touched_files,
                parent_id=parent_id,
                source_run_id=source_run_id,
                git_sha=normalize_optional_string(git_ctx.get("sha")),
                git_branch=normalize_optional_string(git_ctx.get("branch")),
                git_dirty=normalize_git_dirty(git_ctx.get("dirty")),
                namespace=namespace,
                origin=origin,
                metadata=metadata,
            )
            memories.append(memory)
            if old is not None:
                old["superseded_by"] = memory["id"]
            save_store(store)
            append_event_log("create", memory["id"], {"kind": kind, "supersedes": supersedes_id})
            if old is not None:
                append_event_log("supersede", str(old.get("id")), {"superseded_by": memory["id"]})
            _refresh_idf_profiles_safely(trigger="write")

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
            if "namespace" in args:
                memory["namespace"] = normalize_memory_namespace(args.get("namespace"), DEFAULT_MEMORY_NAMESPACE)
                changed.append("namespace")
            if "origin" in args:
                memory["origin"] = normalize_memory_origin(args.get("origin"), DEFAULT_MEMORY_ORIGIN)
                changed.append("origin")
            if "metadata" in args and args.get("metadata") is not None:
                memory["metadata"] = normalize_metadata(args.get("metadata"))
                changed.append("metadata")
            merge_link_fields(memory)
            memory["updated_at"] = now_iso()
            save_store(store)
            if changed:
                append_event_log("update", memory_id, {"changed": changed})
                _refresh_idf_profiles_safely(trigger="write")
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
            _refresh_idf_profiles_safely(trigger="write")
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


def pack_landing_dir() -> Path:
    configured = os.environ.get("MNEMO_PACK_LANDING_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (state_dir() / "packs" / "inbox").resolve()


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
        domain = normalize_optional_string(args.get("domain"))
        language = _normalize_alias_language(args.get("language"))
        alias_runtime = _expand_query_with_aliases(query, domain=domain, language=language)
        limit = int(args.get("limit", 8))
        limit = max(1, min(limit, 20))
        search_args = dict(args)
        search_args["query"] = query
        search_args["limit"] = limit
        search_args["_alias_runtime"] = alias_runtime
        search_args["_expanded_query"] = str(alias_runtime.get("expanded_query") or query)
        phase_label, phase = resolve_phase(search_args, query)
        matches = search_rank(search_args, phase)
        alias_diag = _alias_diagnostics_payload(alias_runtime, matches)
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
                **alias_diag,
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
            **alias_diag,
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




def _memory_candidate_sort_key(memory: dict[str, Any]) -> tuple[Any, ...]:
    memory_id = str(memory.get("id", ""))
    match = re.search(r"(\d+)$", memory_id)
    if match:
        return (memory_id[: match.start()], int(match.group(1)))
    return (str(memory.get("created_at") or ""), memory_id)

def build_consolidation_clusters(
    store: dict[str, Any],
    threshold: float,
    *,
    max_candidates_per_memory: int = 100,
    min_token_count: int = 5,
    shingle_overlap_threshold: float = 0.30,
    use_fts: bool = True,
) -> dict[str, Any]:
    memories = [
        memory
        for memory in store.get("memories", [])
        if not match_is_deleted(memory) and not memory.get("superseded_by") and not bool(memory.get("pinned"))
    ]
    by_kind: dict[str, list[dict[str, Any]]] = {}
    by_id = {str(memory.get("id", "")): memory for memory in memories}
    for memory in memories:
        by_kind.setdefault(str(memory.get("kind", "note")), []).append(memory)

    exact_clusters: list[dict[str, Any]] = []
    near_clusters: list[dict[str, Any]] = []
    seen_pairs: set[frozenset] = set()
    skipped_unsafe = 0
    candidates_examined = 0
    similarity_calls = 0
    fts_globally_available = bool(use_fts and store_backend() == "sqlite" and _sqlite_fts_flag())
    fts_available = fts_globally_available
    candidate_source_counts: dict[str, int] = {"fts5": 0, "signature": 0, "fallback": 0}

    salience, _reason = load_optional_agent_salience()

    for kind, kind_memories in by_kind.items():
        kind_memories.sort(key=_memory_candidate_sort_key)

        _hash_sig_cache: dict[str, dict[str, Any]] = {}
        def _get_hash_sig(m: dict[str, Any]) -> dict[str, Any]:
            mid = str(m.get("id", ""))
            if mid not in _hash_sig_cache:
                _hash_sig_cache[mid] = _build_memory_signature(str(m.get("text", "")))
            return _hash_sig_cache[mid]

        by_content_hash: dict[str, list[str]] = {}
        by_normalized_hash: dict[str, list[str]] = {}
        for m in kind_memories:
            mid = str(m.get("id", ""))
            fly = _get_hash_sig(m)
            ch = m.get("content_hash") or fly.get("content_hash")
            nh = m.get("normalized_hash") or fly.get("normalized_hash")
            if ch:
                by_content_hash.setdefault(ch, []).append(mid)
            if nh:
                by_normalized_hash.setdefault(nh, []).append(mid)

        for duplicate_type, buckets in (("content_hash", by_content_hash), ("normalized_hash", by_normalized_hash)):
            for _hash, ids in buckets.items():
                if len(ids) < 2:
                    continue
                pair = frozenset(ids[:2])
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                survivor = max(ids, key=lambda mid: str(by_id.get(mid, {}).get("created_at") or ""))
                to_retire = [mid for mid in ids if mid != survivor]
                exact_clusters.append({
                    "kind": kind,
                    "size": len(ids),
                    "ids": ids,
                    "survivor": survivor,
                    "to_retire": to_retire,
                    "duplicate_type": duplicate_type,
                    "candidate_source": "exact_hash",
                })

        # Only suppress exact duplicates that would be retired. Keep the survivor
        # eligible so near-duplicates can still cluster with the canonical record.
        exact_ids: set[str] = {mid for cl in exact_clusters for mid in cl.get("to_retire", [])}

        preloaded_token_counts: dict[str, int] = {}
        preloaded_shingle_sets: dict[str, frozenset] = {}
        preloaded_shingles_empty: set[str] = set()
        fly_sigs: dict[str, dict[str, Any]] = {}
        token_sets: dict[str, set[str]] = {}
        top_terms_by_id: dict[str, set[str]] = {}

        for pm in kind_memories:
            mid = str(pm.get("id", ""))
            raw_sh = pm.get("shingle_hashes_json")
            if raw_sh:
                lst = _load_json_string_list(raw_sh)
                if lst:
                    preloaded_shingle_sets[mid] = frozenset(lst)
                else:
                    preloaded_shingles_empty.add(mid)
            raw_tc = pm.get("token_count")
            if raw_tc is not None:
                try:
                    preloaded_token_counts[mid] = int(raw_tc)
                except Exception:
                    pass
            terms = set(_memory_top_terms(pm))
            if terms:
                top_terms_by_id[mid] = terms

        def _get_sig(m: dict[str, Any]) -> dict[str, Any]:
            mid = str(m.get("id", ""))
            if mid not in fly_sigs:
                fly_sigs[mid] = _build_memory_signature(str(m.get("text", "")))
            return fly_sigs[mid]

        def _get_shingle_set(m: dict[str, Any]) -> frozenset | None:
            mid = str(m.get("id", ""))
            if mid in preloaded_shingle_sets:
                return preloaded_shingle_sets[mid]
            if mid in preloaded_shingles_empty:
                return None
            lst = _load_json_string_list(_get_sig(m).get("shingle_hashes_json"))
            if lst:
                fs = frozenset(lst)
                preloaded_shingle_sets[mid] = fs
                return fs
            preloaded_shingles_empty.add(mid)
            return None

        def _get_token_count(m: dict[str, Any]) -> int:
            mid = str(m.get("id", ""))
            if mid in preloaded_token_counts:
                return preloaded_token_counts[mid]
            tc = int(_get_sig(m).get("token_count") or 0)
            preloaded_token_counts[mid] = tc
            return tc

        def _get_token_set(m: dict[str, Any]) -> set[str]:
            mid = str(m.get("id", ""))
            if mid not in token_sets:
                token_sets[mid] = set(_normalize_for_signature(str(m.get("text", ""))))
            return token_sets[mid]

        eligible: list[tuple[str, dict[str, Any], frozenset]] = []
        for m in kind_memories:
            mid = str(m.get("id", ""))
            if mid in exact_ids:
                continue
            fs = _get_shingle_set(m)
            tc = _get_token_count(m)
            if fs is None or tc < min_token_count:
                skipped_unsafe += 1
                continue
            eligible.append((mid, m, fs))

        eligible_by_id = {mid: (m, fs) for mid, m, fs in eligible}
        eligible_index = {mid: idx for idx, (mid, _m, _fs) in enumerate(eligible)}
        # Build a shingle-hash inverted index. This is much cheaper and more
        # selective than comparing every pair or expanding common top terms.
        large_store_window_mode = len(eligible) > 5_000
        shingle_to_ids: dict[str, list[str]] = {}
        if not large_store_window_mode:
            bucket_cap = max(max_candidates_per_memory, 100)
            for mid, _m, fs in eligible:
                for shingle_hash in fs:
                    bucket = shingle_to_ids.setdefault(str(shingle_hash), [])
                    # Keep buckets bounded to avoid pathological common-shingle fanout.
                    if len(bucket) < bucket_cap:
                        bucket.append(mid)
        # FTS candidate lookup is useful on small stores, but per-row FTS queries are
        # intentionally disabled on large stores because that becomes expensive on
        # local machines. Large-store default relies on bounded neighbor windows.
        fts_available = bool(fts_globally_available and len(eligible) <= 500)

        def _fast_shingle_overlap(fs_a: frozenset, fs_b: frozenset) -> float:
            inter = len(fs_a & fs_b)
            if inter == 0:
                return 0.0
            return inter / (len(fs_a) + len(fs_b) - inter)

        def _candidate_ids_for(mid: str, m: dict[str, Any], idx: int) -> tuple[list[str], str]:
            if large_store_window_mode:
                # Large stores must stay cheap on local machines. Exact hashes are
                # handled globally; use a narrow deterministic neighborhood window
                # over stable id/created order for near-duplicate candidates.
                large_radius = min(max_candidates_per_memory, 3)
                start = max(0, idx - large_radius)
                end_idx = min(len(eligible), idx + large_radius + 1)
                window = [cid for cid, _cm, _fs in eligible[start:idx] + eligible[idx + 1:end_idx] if cid != mid]
                return window[:large_radius], "fallback"

            ids: list[str] = []
            if fts_available:
                fts_ids = _sqlite_fts_candidate_ids_for_memory(
                    m,
                    limit=max_candidates_per_memory * 3,
                    exclude_pinned=True,
                )
                for cid in fts_ids:
                    if cid == mid or cid not in eligible_by_id:
                        continue
                    cand, _cand_fs = eligible_by_id[cid]
                    if not _metadata_compatible_for_duplicate(m, cand):
                        continue
                    if cid not in ids:
                        ids.append(cid)
                    if len(ids) >= max_candidates_per_memory:
                        break
                if ids:
                    return ids, "fts5"

            # Signature fallback: candidates sharing min-K shingle hashes.
            scored_ids: dict[str, int] = {}
            for shingle_hash in m_shingle_set:
                for cid in shingle_to_ids.get(str(shingle_hash), []):
                    if cid == mid or cid not in eligible_by_id:
                        continue
                    cand, _cand_fs = eligible_by_id[cid]
                    if not _metadata_compatible_for_duplicate(m, cand):
                        continue
                    scored_ids[cid] = scored_ids.get(cid, 0) + 1
            if scored_ids:
                ordered = sorted(
                    scored_ids.keys(),
                    key=lambda cid: (scored_ids[cid], str(eligible_by_id[cid][0].get("created_at") or ""), cid),
                    reverse=True,
                )
                return ordered[:max_candidates_per_memory], "signature"

            start = max(0, idx - max_candidates_per_memory)
            end_idx = min(len(eligible), idx + max_candidates_per_memory + 1)
            window = [cid for cid, _cm, _fs in eligible[start:idx] + eligible[idx + 1:end_idx] if cid != mid]
            return window[:max_candidates_per_memory], "fallback"

        for idx, (mid, m, m_shingle_set) in enumerate(eligible):
            candidate_ids, source = _candidate_ids_for(mid, m, idx)
            candidate_source_counts[source] = candidate_source_counts.get(source, 0) + 1
            candidates_examined += len(candidate_ids)

            for cand_id in candidate_ids:
                cand, cand_shingle_set = eligible_by_id[cand_id]
                pair = frozenset([mid, cand_id])
                if pair in seen_pairs:
                    continue
                overlap = _fast_shingle_overlap(m_shingle_set, cand_shingle_set)
                if overlap < shingle_overlap_threshold:
                    continue

                seen_pairs.add(pair)
                similarity_calls += 1

                if salience is not None:
                    breakdown = salience.signal_score(str(m.get("text", "")), str(cand.get("text", "")))
                    sim = max(0.0, min(1.0, float(getattr(breakdown, "final", 0.0))))
                else:
                    sim = _jaccard_similarity_fallback(_get_token_set(m), _get_token_set(cand))

                if sim >= threshold:
                    survivor = max([mid, cand_id], key=lambda x: str(by_id.get(x, {}).get("created_at") or ""))
                    to_retire = [x for x in [mid, cand_id] if x != survivor]
                    near_clusters.append({
                        "kind": kind,
                        "size": 2,
                        "ids": [mid, cand_id],
                        "survivor": survivor,
                        "to_retire": to_retire,
                        "duplicate_type": "near_duplicate",
                        "candidate_source": source,
                        "shingle_overlap": round(overlap, 4),
                        "similarity": round(sim, 4),
                    })

    return {
        "clusters": exact_clusters + near_clusters,
        "skipped_unsafe": skipped_unsafe,
        "candidates_examined": candidates_examined,
        "similarity_calls": similarity_calls,
        "candidate_source_counts": candidate_source_counts,
        "fts_available": fts_available,
    }

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
    max_cands = int(args.get("max_candidates_per_memory") or 100)
    min_tok = int(args.get("min_token_count") or 5)
    shingle_thresh = float(args.get("shingle_overlap_threshold") or 0.30)
    use_fts = parse_bool(args.get("use_fts"), default=True)

    result = build_consolidation_clusters(
        load_store(),
        threshold,
        max_candidates_per_memory=max_cands,
        min_token_count=min_tok,
        shingle_overlap_threshold=shingle_thresh,
        use_fts=use_fts,
    )
    clusters = result["clusters"]

    if dry_run:
        structured = {
            "action": "consolidate",
            "applied": False,
            "threshold": threshold,
            "clusters": clusters,
            "skipped_unsafe": result["skipped_unsafe"],
            "candidates_examined": result["candidates_examined"],
            "similarity_calls": result["similarity_calls"],
            "candidate_source_counts": result.get("candidate_source_counts", {}),
            "fts_available": result.get("fts_available", False),
        }
        return text_result(render_consolidation_text(clusters, threshold, False, 0), structured)

    retired = 0
    try:
        with MemoryFileLock(store_lock_path()):
            store = load_store()
            result2 = build_consolidation_clusters(
                store,
                threshold,
                max_candidates_per_memory=max_cands,
                min_token_count=min_tok,
                shingle_overlap_threshold=shingle_thresh,
                use_fts=use_fts,
            )
            clusters = result2["clusters"]
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
                _refresh_idf_profiles_safely(trigger="maintenance")
    except LockTimeout as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")

    structured = {
        "action": "consolidate",
        "applied": True,
        "threshold": threshold,
        "clusters": clusters,
        "skipped_unsafe": result2.get("skipped_unsafe", 0),
        "candidates_examined": result2.get("candidates_examined", 0),
        "similarity_calls": result2.get("similarity_calls", 0),
        "candidate_source_counts": result2.get("candidate_source_counts", {}),
        "fts_available": result2.get("fts_available", False),
    }
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

    if imported and not dry_run:
        _refresh_idf_profiles_safely(trigger="maintenance")

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


def _backfill_signatures_maintenance(args: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    """Backfill signature columns for rows with missing or outdated signatures."""
    if store_backend() != "sqlite":
        return tool_error("backfill_signatures requires SQLite backend")
    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            rows = conn.execute(
                """SELECT id, text FROM memories
                   WHERE deleted=0
                   AND (
                       signature_version IS NULL OR signature_version != ?
                       OR normalizer_version IS NULL OR normalizer_version != ?
                       OR content_hash IS NULL OR normalized_hash IS NULL
                       OR shingle_hashes_json IS NULL
                   )""",
                (SIGNATURE_VERSION, NORMALIZER_VERSION),
            ).fetchall()
            count_missing = len(rows)
            total_active = conn.execute("SELECT COUNT(*) FROM memories WHERE deleted=0").fetchone()[0]
            if dry_run:
                return text_result(
                    f"backfill_signatures dry_run: {count_missing} of {total_active} active memories have missing/outdated signatures.",
                    {
                        "action": "backfill_signatures",
                        "dry_run": True,
                        "count_missing": count_missing,
                        "count_total": total_active,
                        "ratio": round(count_missing / total_active, 4) if total_active else 0.0,
                    },
                )
            # Batch update in chunks of 500
            updated = 0
            BATCH_SIZE = 500
            for i in range(0, len(rows), BATCH_SIZE):
                batch = rows[i : i + BATCH_SIZE]
                updates = []
                for row in batch:
                    memory_id = str(row["id"])
                    text_val = str(row["text"] or "")
                    sig = _build_memory_signature(text_val)
                    updates.append((
                        sig["content_hash"],
                        sig["normalized_hash"],
                        sig["token_count"],
                        sig["unique_token_count"],
                        sig["top_terms_json"],
                        sig["shingle_hashes_json"],
                        sig["signature_version"],
                        sig["normalizer_version"],
                        sig["signature_updated_at"],
                        memory_id,
                    ))
                conn.executemany(
                    """UPDATE memories SET
                        content_hash=?, normalized_hash=?, token_count=?,
                        unique_token_count=?, top_terms_json=?, shingle_hashes_json=?,
                        signature_version=?, normalizer_version=?, signature_updated_at=?
                       WHERE id=?""",
                    updates,
                )
                updated += len(batch)
        if updated:
            _refresh_idf_profiles_safely(trigger="maintenance")
        return text_result(
            f"backfill_signatures: updated {updated} memories.",
            {
                "action": "backfill_signatures",
                "dry_run": False,
                "updated_count": updated,
                "count_total": total_active,
            },
        )
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")


def _consolidate_full_maintenance(args: dict[str, Any]) -> dict[str, Any]:
    """O(n^2) full-scan consolidation. Requires confirm_full_scan: true."""
    if not parse_bool(args.get("confirm_full_scan"), default=False):
        store = load_store()
        active = [m for m in store.get("memories", []) if is_active(m) and not m.get("pinned")]
        n = len(active)
        estimated_pairs = n * (n - 1) // 2
        return {
            "content": [{"type": "text", "text": "Error: full_scan_confirmation_required"}],
            "isError": True,
            "structuredContent": {
                "error": "full_scan_confirmation_required",
                "message": "consolidate_full is O(n^2) and can be expensive on large stores. Pass confirm_full_scan: true to proceed.",
                "estimated_pair_count": estimated_pairs,
            },
        }

    dry_run = parse_bool(args.get("dry_run"), default=True)
    threshold = consolidate_threshold(args.get("threshold") if "threshold" in args else None)
    store = load_store()

    active = [m for m in store.get("memories", []) if is_active(m) and not m.get("pinned")]
    n = len(active)
    estimated_pairs = n * (n - 1) // 2
    warning_msg = f"consolidate_full is O(n^2). Examining {estimated_pairs} pairs across {n} active memories."

    if dry_run:
        return text_result(
            warning_msg + " dry_run=true; no changes applied.",
            {
                "action": "consolidate_full",
                "dry_run": True,
                "estimated_pair_count": estimated_pairs,
                "active_memory_count": n,
                "warning": warning_msg,
                "threshold": threshold,
            },
        )

    # Full all-pairs scan
    clusters: list[dict[str, Any]] = []
    seen_pairs: set[frozenset[str]] = set()
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for m in active:
        k = str(m.get("kind", "note"))
        by_kind.setdefault(k, []).append(m)

    salience, _ = load_optional_agent_salience()
    for kind, kind_memories in by_kind.items():
        tokens = [set(_normalize_for_signature(str(m.get("text", "")))) for m in kind_memories]
        for i in range(len(kind_memories)):
            for j in range(i + 1, len(kind_memories)):
                mid_i = str(kind_memories[i].get("id", ""))
                mid_j = str(kind_memories[j].get("id", ""))
                pair = frozenset([mid_i, mid_j])
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                if salience is not None:
                    breakdown = salience.signal_score(
                        str(kind_memories[i].get("text", "")),
                        str(kind_memories[j].get("text", "")),
                    )
                    sim = max(0.0, min(1.0, float(getattr(breakdown, "final", 0.0))))
                else:
                    sim = _jaccard_similarity_fallback(tokens[i], tokens[j])
                if sim >= threshold:
                    survivor = max(
                        [mid_i, mid_j],
                        key=lambda x: str(kind_memories[i if x == mid_i else j].get("created_at") or ""),
                    )
                    to_retire = [x for x in [mid_i, mid_j] if x != survivor]
                    clusters.append({
                        "kind": kind,
                        "size": 2,
                        "ids": [mid_i, mid_j],
                        "survivor": survivor,
                        "to_retire": to_retire,
                        "similarity": round(sim, 4),
                    })

    retired = 0
    try:
        with MemoryFileLock(store_lock_path()):
            store2 = load_store()
            by_id = {str(m.get("id")): m for m in store2.get("memories", [])}
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
                save_store(store2)
                _refresh_idf_profiles_safely(trigger="maintenance")
    except LockTimeout as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")

    return text_result(
        warning_msg + f" Applied {len(clusters)} cluster(s), {retired} retired.",
        {
            "action": "consolidate_full",
            "dry_run": False,
            "applied": True,
            "clusters": clusters,
            "clusters_found": len(clusters),
            "retired": retired,
            "estimated_pair_count": estimated_pairs,
            "warning": warning_msg,
            "threshold": threshold,
        },
    )


def _safe_int(value: Any, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _safe_float(value: Any, default: float, *, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _normalize_alias_term(text: Any) -> str:
    value = normalize_optional_string(text)
    if not value:
        return ""
    return normalize_text(value)


def _normalize_alias_language(language: Any) -> str:
    value = normalize_optional_string(language)
    if not value:
        return DEFAULT_ALIAS_LANGUAGE
    return value.lower()


def _load_active_alias_terms(domain: str | None = None, language: str | None = None) -> list[dict[str, Any]]:
    if store_backend() != "sqlite":
        return []
    wanted_domain = normalize_optional_string(domain)
    wanted_language = _normalize_alias_language(language)
    sql = (
        "SELECT "
        "c.concept_id AS concept_id, "
        "c.canonical AS canonical, "
        "c.domain AS concept_domain, "
        "c.language AS concept_language, "
        "c.weight AS concept_weight, "
        "t.alias_id AS alias_id, "
        "t.term AS term, "
        "t.normalized_term AS normalized_term, "
        "t.domain AS term_domain, "
        "t.language AS term_language, "
        "t.weight AS term_weight "
        "FROM alias_terms t "
        "JOIN alias_concepts c ON c.concept_id = t.concept_id "
        "WHERE c.status = 'active' AND t.status = 'active' "
        "AND COALESCE(NULLIF(c.language, ''), ?) = ? "
        "AND COALESCE(NULLIF(t.language, ''), ?) = ?"
    )
    params: list[Any] = [DEFAULT_ALIAS_LANGUAGE, wanted_language, DEFAULT_ALIAS_LANGUAGE, wanted_language]
    if wanted_domain is not None:
        sql += " AND (COALESCE(NULLIF(c.domain, ''), '') = '' OR c.domain = ?)"
        params.append(wanted_domain)
        sql += " AND (COALESCE(NULLIF(t.domain, ''), '') = '' OR t.domain = ?)"
        params.append(wanted_domain)
    sql += " ORDER BY c.canonical, t.term"
    with _sqlite_session() as conn:
        _sqlite_ensure_schema(conn)
        _sqlite_bootstrap_if_needed(conn)
        rows = conn.execute(sql, tuple(params)).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "concept_id": str(row["concept_id"]),
                "canonical": str(row["canonical"] or ""),
                "concept_domain": normalize_optional_string(row["concept_domain"]),
                "concept_language": _normalize_alias_language(row["concept_language"]),
                "concept_weight": float(row["concept_weight"] or 1.0),
                "alias_id": str(row["alias_id"] or ""),
                "term": str(row["term"] or ""),
                "normalized_term": _normalize_alias_term(row["normalized_term"] or row["term"]),
                "term_domain": normalize_optional_string(row["term_domain"]),
                "term_language": _normalize_alias_language(row["term_language"]),
                "term_weight": float(row["term_weight"] or 1.0),
            }
        )
    return out


def _build_alias_map_from_rows(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    alias_map_sets: dict[str, set[str]] = {}
    for row in rows:
        canonical = normalize_optional_string(row.get("canonical"))
        if not canonical:
            continue
        bucket = alias_map_sets.setdefault(canonical, set())
        bucket.add(canonical)
        term = normalize_optional_string(row.get("term"))
        if term:
            bucket.add(term)
    return {canonical: sorted(list(terms), key=lambda item: item.lower()) for canonical, terms in alias_map_sets.items()}


def _build_alias_map_for_agent_salience(domain: str | None = None, language: str | None = None) -> dict[str, list[str]]:
    rows = _load_active_alias_terms(domain=domain, language=language)
    return _build_alias_map_from_rows(rows)


def _normalized_term_in_text(normalized_text: str, normalized_term: str) -> bool:
    if not normalized_text or not normalized_term:
        return False
    return f" {normalized_term} " in f" {normalized_text} "


def _match_alias_concepts(
    text: str,
    domain: str | None = None,
    language: str | None = None,
    *,
    alias_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized_text = _normalize_alias_term(text)
    rows = alias_rows if alias_rows is not None else _load_active_alias_terms(domain=domain, language=language)
    if not normalized_text or not rows:
        return {
            "concept_ids": [],
            "concepts": [],
            "terms": [],
            "concept_terms": {},
        }

    concept_ids: list[str] = []
    concepts: list[str] = []
    terms: list[str] = []
    concept_terms: dict[str, list[str]] = {}
    concept_seen: set[str] = set()
    term_seen: set[str] = set()
    for row in rows:
        concept_id = str(row.get("concept_id") or "").strip()
        canonical = normalize_optional_string(row.get("canonical")) or concept_id
        term_value = normalize_optional_string(row.get("term"))
        normalized_term = _normalize_alias_term(row.get("normalized_term") or term_value)
        canonical_term = _normalize_alias_term(canonical)
        matched_term = None
        if normalized_term and _normalized_term_in_text(normalized_text, normalized_term):
            matched_term = term_value or normalized_term
        elif canonical_term and _normalized_term_in_text(normalized_text, canonical_term):
            matched_term = canonical
        if matched_term is None:
            continue
        if concept_id and concept_id not in concept_seen:
            concept_seen.add(concept_id)
            concept_ids.append(concept_id)
            concepts.append(canonical)
        norm_match = _normalize_alias_term(matched_term)
        if norm_match and norm_match not in term_seen:
            term_seen.add(norm_match)
            terms.append(matched_term)
        if concept_id:
            bucket = concept_terms.setdefault(concept_id, [])
            if matched_term not in bucket:
                bucket.append(matched_term)
    return {
        "concept_ids": concept_ids,
        "concepts": concepts,
        "terms": terms,
        "concept_terms": concept_terms,
    }


def _expand_query_with_aliases(
    query_text: str,
    domain: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    wanted_language = _normalize_alias_language(language)
    query_value = str(query_text or "").strip()
    rows = _load_active_alias_terms(domain=domain, language=wanted_language)
    alias_map = _build_alias_map_from_rows(rows)
    matched = _match_alias_concepts(query_value, domain=domain, language=wanted_language, alias_rows=rows)
    query_concept_ids = [str(item) for item in matched.get("concept_ids", []) if str(item).strip()]
    normalized_query = _normalize_alias_term(query_value)

    concept_terms: dict[str, list[str]] = {}
    concept_names: dict[str, str] = {}
    concept_weights: dict[str, float] = {}
    for row in rows:
        concept_id = str(row.get("concept_id") or "").strip()
        if not concept_id:
            continue
        canonical = normalize_optional_string(row.get("canonical")) or concept_id
        concept_names[concept_id] = canonical
        concept_weights[concept_id] = max(
            concept_weights.get(concept_id, 0.0),
            float(row.get("concept_weight") or 1.0),
            float(row.get("term_weight") or 1.0),
        )
        bucket = concept_terms.setdefault(concept_id, [])
        if canonical not in bucket:
            bucket.append(canonical)
        term = normalize_optional_string(row.get("term"))
        if term and term not in bucket:
            bucket.append(term)

    expansion_terms: list[str] = []
    seen_norm_terms: set[str] = set()
    for concept_id in query_concept_ids:
        for term in concept_terms.get(concept_id, []):
            norm = _normalize_alias_term(term)
            if not norm:
                continue
            if _normalized_term_in_text(normalized_query, norm):
                continue
            if norm in seen_norm_terms:
                continue
            seen_norm_terms.add(norm)
            expansion_terms.append(term)
    expanded_query = query_value
    if expansion_terms:
        expanded_query = (query_value + " " + " ".join(expansion_terms)).strip()
    return {
        "available": store_backend() == "sqlite",
        "domain": normalize_optional_string(domain),
        "language": wanted_language,
        "query_text": query_value,
        "normalized_query": normalized_query,
        "expanded_query": expanded_query,
        "alias_map": alias_map,
        "alias_rows": rows,
        "query_concept_ids": query_concept_ids,
        "query_concepts": [concept_names.get(concept_id, concept_id) for concept_id in query_concept_ids],
        "query_terms_matched": [str(item) for item in matched.get("terms", []) if str(item).strip()],
        "concept_terms": concept_terms,
        "concept_names": concept_names,
        "concept_weights": concept_weights,
        "expansion_terms": expansion_terms,
        "alias_candidate_expansion_count": len(expansion_terms),
        "aliases_used": bool(query_concept_ids),
    }


def _alias_concept_score_for_memory(memory: dict[str, Any], alias_runtime: dict[str, Any] | None) -> tuple[float, list[str]]:
    runtime = alias_runtime if isinstance(alias_runtime, dict) else {}
    concept_ids = [str(item) for item in runtime.get("query_concept_ids", []) if str(item).strip()]
    if not concept_ids:
        return 0.0, []
    concept_terms = runtime.get("concept_terms")
    if not isinstance(concept_terms, dict) or not concept_terms:
        return 0.0, []
    normalized_text = _normalize_alias_term(str(memory.get("text", "")))
    if not normalized_text:
        return 0.0, []
    matched_concepts: list[str] = []
    for concept_id in concept_ids:
        terms = concept_terms.get(concept_id, [])
        if not isinstance(terms, list):
            continue
        for term in terms:
            if _normalized_term_in_text(normalized_text, _normalize_alias_term(term)):
                matched_concepts.append(concept_id)
                break
    if not matched_concepts:
        return 0.0, []
    weights = runtime.get("concept_weights")
    avg_weight = 1.0
    if isinstance(weights, dict):
        weight_values = [float(weights.get(concept_id, 1.0)) for concept_id in matched_concepts]
        if weight_values:
            avg_weight = sum(weight_values) / float(len(weight_values))
    score = ALIAS_CONCEPT_BASE_BOOST + (0.02 * max(0, len(matched_concepts) - 1))
    score *= max(0.5, min(1.5, avg_weight))
    bounded_score = max(0.0, min(ALIAS_CONCEPT_MAX_BOOST, score))
    unique_concepts = sorted(set(matched_concepts))
    return bounded_score, unique_concepts


def _alias_diagnostics_payload(alias_runtime: dict[str, Any] | None, matches: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    runtime = alias_runtime if isinstance(alias_runtime, dict) else {}
    concepts = [str(item) for item in runtime.get("query_concepts", []) if str(item).strip()]
    terms = [str(item) for item in runtime.get("query_terms_matched", []) if str(item).strip()]
    expansion_count = int(runtime.get("alias_candidate_expansion_count") or 0)
    alias_concept_score = 0.0
    if isinstance(matches, list):
        for item in matches:
            if not isinstance(item, dict):
                continue
            alias_concept_score = max(alias_concept_score, float(item.get("alias_concept_score") or 0.0))
    return {
        "aliases_used": bool(runtime.get("aliases_used", False)),
        "alias_concepts_matched": concepts,
        "alias_terms_matched": terms,
        "alias_candidate_expansion_count": expansion_count,
        "alias_concept_score": round(alias_concept_score, 3),
    }


def _event_time_within_window(record: dict[str, Any], cutoff: datetime) -> bool:
    stamp = str(record.get("ts") or record.get("created_at") or "").strip()
    if not stamp:
        return False
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when >= cutoff
    except ValueError:
        return stamp >= cutoff.isoformat().replace("+00:00", "Z")


def _load_recent_alias_source_events(
    *,
    window_days: int,
    domain: str | None,
    include_hints: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, window_days))
    misses: list[dict[str, Any]] = []
    hints: list[dict[str, Any]] = []
    wanted_domain = normalize_optional_string(domain)
    if store_backend() == "sqlite":
        cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            _sqlite_bootstrap_if_needed(conn)
            miss_actions = sorted(MISS_EVENT_ACTIONS)
            placeholders = ",".join("?" for _ in miss_actions)
            miss_sql = (
                "SELECT * FROM events WHERE COALESCE(ts, created_at) >= ? "
                "AND success = 0 AND include_in_salience = 1 "
                "AND TRIM(COALESCE(query_text, '')) != '' "
                f"AND COALESCE(action, event_type) IN ({placeholders})"
            )
            miss_params: list[Any] = [cutoff_iso, *miss_actions]
            if wanted_domain is not None:
                miss_sql += " AND domain = ?"
                miss_params.append(wanted_domain)
            miss_sql += " ORDER BY COALESCE(ts, created_at) DESC, rowid DESC LIMIT ?"
            miss_params.append(MAX_ALIAS_PROPOSAL_EVENT_SCAN)
            miss_rows = conn.execute(miss_sql, miss_params).fetchall()
            misses = [_sqlite_event_row_to_record(row) for row in miss_rows]

            if include_hints:
                hint_sql = (
                    "SELECT * FROM events WHERE COALESCE(ts, created_at) >= ? "
                    "AND COALESCE(action, event_type) = 'alias_hint'"
                )
                hint_params: list[Any] = [cutoff_iso]
                if wanted_domain is not None:
                    hint_sql += " AND domain = ?"
                    hint_params.append(wanted_domain)
                hint_sql += " ORDER BY COALESCE(ts, created_at) DESC, rowid DESC LIMIT ?"
                hint_params.append(MAX_ALIAS_PROPOSAL_EVENT_SCAN)
                hint_rows = conn.execute(hint_sql, hint_params).fetchall()
                hints = [_sqlite_event_row_to_record(row) for row in hint_rows]
        return misses, hints

    rows = _legacy_event_rows(include_archive=False)
    for row in rows:
        action = str(row.get("action") or row.get("event_type") or "").strip()
        if not _event_time_within_window(row, cutoff):
            continue
        row_domain = normalize_optional_string(row.get("domain"))
        if wanted_domain is not None and row_domain != wanted_domain:
            continue
        if action in MISS_EVENT_ACTIONS:
            if _event_int_value(row.get("success")) != 0:
                continue
            if _event_int_value(row.get("include_in_salience")) != 1:
                continue
            if not normalize_optional_string(row.get("query_text")):
                continue
            misses.append(row)
        elif include_hints and action == "alias_hint":
            hints.append(row)
    misses = misses[:MAX_ALIAS_PROPOSAL_EVENT_SCAN]
    hints = hints[:MAX_ALIAS_PROPOSAL_EVENT_SCAN]
    return misses, hints


def _cluster_miss_events(miss_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    exact: dict[str, dict[str, Any]] = {}
    for event in miss_events:
        query = normalize_optional_string(event.get("query_text"))
        if not query:
            continue
        key = normalize_text(query)
        cluster = exact.setdefault(
            key,
            {
                "norm_key": key,
                "miss_events": [],
                "hints": [],
                "query_counts": {},
                "domain_counts": {},
                "representative": query,
                "rep_shingles": _build_min_k_shingle_hashes(_normalize_for_signature(query)),
                "rep_tokens": set(_normalize_for_signature(query)),
            },
        )
        cluster["miss_events"].append(event)
        cluster["query_counts"][query] = int(cluster["query_counts"].get(query, 0)) + 1
        domain = normalize_optional_string(event.get("domain"))
        if domain:
            cluster["domain_counts"][domain] = int(cluster["domain_counts"].get(domain, 0)) + 1

    ordered = sorted(
        exact.values(),
        key=lambda item: (-len(item["miss_events"]), str(item["representative"]).lower(), str(item["norm_key"])),
    )
    merged: list[dict[str, Any]] = []
    for cluster in ordered:
        best_index = -1
        best_overlap = 0.0
        for index, existing in enumerate(merged):
            overlap = _signature_overlap(cluster["rep_shingles"], existing["rep_shingles"])
            if overlap >= ALIAS_CLUSTER_SHINGLE_OVERLAP_THRESHOLD and overlap > best_overlap:
                best_overlap = overlap
                best_index = index
        if best_index < 0:
            merged.append(cluster)
            continue
        target = merged[best_index]
        target["miss_events"].extend(cluster["miss_events"])
        for query, count in cluster["query_counts"].items():
            target["query_counts"][query] = int(target["query_counts"].get(query, 0)) + int(count)
        for domain, count in cluster["domain_counts"].items():
            target["domain_counts"][domain] = int(target["domain_counts"].get(domain, 0)) + int(count)
        if len(cluster["miss_events"]) > len(target["miss_events"]):
            target["representative"] = cluster["representative"]
            target["rep_shingles"] = cluster["rep_shingles"]
            target["rep_tokens"] = cluster["rep_tokens"]
    for cluster in merged:
        top_queries = sorted(
            cluster["query_counts"].items(),
            key=lambda item: (-int(item[1]), str(item[0]).lower()),
        )
        if top_queries:
            cluster["representative"] = str(top_queries[0][0])
            cluster["rep_shingles"] = _build_min_k_shingle_hashes(_normalize_for_signature(cluster["representative"]))
            cluster["rep_tokens"] = set(_normalize_for_signature(cluster["representative"]))
    return merged


def _hint_text_for_matching(hint: dict[str, Any]) -> str:
    payload = _event_payload_from_record(hint)
    for key in ("original_query", "candidate_alias", "query_text", "query", "successful_query", "canonical"):
        value = normalize_optional_string(payload.get(key))
        if value:
            return value
        value = normalize_optional_string(hint.get(key))
        if value:
            return value
    return ""


def _hint_confidence(hint: dict[str, Any]) -> str:
    payload = _event_payload_from_record(hint)
    value = normalize_optional_string(payload.get("confidence"))
    if value is None:
        value = normalize_optional_string(hint.get("confidence"))
    normalized = (value or "medium").strip().lower()
    return normalized if normalized in {"low", "medium", "high"} else "medium"


def _attach_alias_hints(
    clusters: list[dict[str, Any]],
    hints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out = list(clusters)
    for hint in hints:
        text = _hint_text_for_matching(hint)
        if not text:
            continue
        normalized = normalize_text(text)
        hint_tokens = set(_normalize_for_signature(text))
        best_index = -1
        best_score = 0.0
        for index, cluster in enumerate(out):
            score = 0.0
            if normalized == normalize_text(str(cluster.get("representative") or "")):
                score = 1.0
            else:
                cluster_tokens = cluster.get("rep_tokens") if isinstance(cluster.get("rep_tokens"), set) else set()
                score = _jaccard_similarity_fallback(hint_tokens, cluster_tokens)
            if score > best_score:
                best_score = score
                best_index = index
        if best_index >= 0 and best_score >= 0.20:
            out[best_index]["hints"].append(hint)
            hint_domain = normalize_optional_string(hint.get("domain")) or normalize_optional_string(
                _event_payload_from_record(hint).get("domain")
            )
            if hint_domain:
                out[best_index]["domain_counts"][hint_domain] = int(
                    out[best_index]["domain_counts"].get(hint_domain, 0)
                ) + 1
            continue
        hint_domain = normalize_optional_string(hint.get("domain")) or normalize_optional_string(
            _event_payload_from_record(hint).get("domain")
        )
        domain_counts = {hint_domain: 1} if hint_domain else {}
        out.append(
            {
                "norm_key": normalized,
                "miss_events": [],
                "hints": [hint],
                "query_counts": {text: 1},
                "domain_counts": domain_counts,
                "representative": text,
                "rep_shingles": _build_min_k_shingle_hashes(_normalize_for_signature(text)),
                "rep_tokens": hint_tokens,
            }
        )
    return out


def _event_payload_from_record(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("data")
    if isinstance(payload, dict):
        return dict(payload)
    raw = record.get("data_json")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return dict(parsed)
        except json.JSONDecodeError:
            return {}
    return {}


def _alias_candidate_memories(
    query_text: str,
    *,
    domain: str | None,
    max_candidates_per_cluster: int,
    min_loose_score: float,
) -> list[tuple[float, dict[str, Any]]]:
    if not query_text.strip():
        return []
    candidate_limit = max(50, min(500, max_candidates_per_cluster * 25))
    args: dict[str, Any] = {}
    if domain is not None:
        args["domain"] = domain
    candidates: list[dict[str, Any]] = []
    if store_backend() == "sqlite" and _sqlite_fts_flag():
        candidates = _sqlite_fts_candidate_memories(
            args,
            query_text,
            include_deleted=False,
            include_superseded=False,
            limit=candidate_limit,
        )[:candidate_limit]
    if not candidates:
        store = load_store()
        visible = [
            memory
            for memory in store.get("memories", [])
            if isinstance(memory, dict) and visible_memory(memory, False, False)
        ]
        if domain is not None:
            visible = [memory for memory in visible if normalize_optional_string(memory.get("domain")) in {domain, None}]
        wanted_terms = set(_build_top_terms(_normalize_for_signature(query_text), DEFAULT_TOP_TERMS))
        ranked: list[tuple[int, str, str, dict[str, Any]]] = []
        for memory in visible:
            mem_terms = set(_memory_top_terms(memory))
            overlap = len(wanted_terms & mem_terms) if wanted_terms and mem_terms else 0
            if overlap > 0:
                ranked.append((overlap, str(memory.get("created_at") or ""), str(memory.get("id") or ""), memory))
        ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        candidates = [item[3] for item in ranked[:candidate_limit]]
        if not candidates:
            candidates = visible[-candidate_limit:]

    query_tokens = set(_normalize_for_signature(query_text))
    query_shingles = _build_min_k_shingle_hashes(_normalize_for_signature(query_text))
    scored: list[tuple[float, dict[str, Any]]] = []
    for memory in candidates:
        mem_terms = set(_memory_top_terms(memory, max_terms=64))
        token_overlap = _jaccard_similarity_fallback(query_tokens, mem_terms) if query_tokens and mem_terms else 0.0
        mem_shingles = _load_json_string_list(memory.get("shingle_hashes_json"))
        if not mem_shingles:
            mem_shingles = _build_min_k_shingle_hashes(_normalize_for_signature(str(memory.get("text", ""))))
        shingle_overlap = _signature_overlap(query_shingles, mem_shingles) if query_shingles and mem_shingles else 0.0
        loose = max(token_overlap, shingle_overlap)
        if loose < min_loose_score:
            continue
        scored.append((loose, memory))
    scored.sort(key=lambda item: (item[0], str(item[1].get("created_at", "")), str(item[1].get("id", ""))), reverse=True)
    return scored[:max_candidates_per_cluster]


def _idf_quantile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * quantile))))
    return float(ordered[position])


def _alias_idf_evidence(alias_text: str, idf_profile: dict[str, Any]) -> tuple[list[str], list[str], float]:
    tokens = []
    seen: set[str] = set()
    for token in _normalize_for_signature(alias_text):
        if len(token) < 2 or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    idf_raw = idf_profile.get("idf")
    idf_map: dict[str, float] = {}
    if isinstance(idf_raw, dict):
        for key, value in idf_raw.items():
            try:
                idf_map[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
    idf_values = list(idf_map.values())
    if not tokens:
        return [], [], 0.0
    if not idf_values:
        return [], tokens, 0.0
    low_cutoff = _idf_quantile(idf_values, 0.25)
    high_cutoff = _idf_quantile(idf_values, 0.75)
    unknown = max(0.0, low_cutoff * 0.80)
    idf_terms: list[str] = []
    penalized_terms: list[str] = []
    normalized_scores: list[float] = []
    denom = max(1e-6, high_cutoff - low_cutoff)
    for token in tokens:
        weight = idf_map.get(token, unknown)
        normalized_scores.append(max(0.0, min(1.0, (weight - low_cutoff) / denom)))
        if token in idf_map and weight >= high_cutoff:
            idf_terms.append(token)
        if token not in idf_map or weight <= low_cutoff:
            penalized_terms.append(token)
    idf_strength = sum(normalized_scores) / len(normalized_scores) if normalized_scores else 0.0
    return idf_terms, penalized_terms, idf_strength


def _proposal_domain(cluster: dict[str, Any], explicit_domain: str | None) -> str | None:
    if explicit_domain is not None:
        return explicit_domain
    counts = cluster.get("domain_counts", {})
    if not isinstance(counts, dict) or not counts:
        return None
    ordered = sorted(counts.items(), key=lambda item: (-int(item[1]), str(item[0])))
    return normalize_optional_string(ordered[0][0])


def _cluster_candidate_alias(cluster: dict[str, Any]) -> str:
    hint_counts: dict[str, int] = {}
    for hint in cluster.get("hints", []):
        payload = _event_payload_from_record(hint)
        candidate = normalize_optional_string(payload.get("candidate_alias")) or normalize_optional_string(hint.get("candidate_alias"))
        if candidate:
            hint_counts[candidate] = int(hint_counts.get(candidate, 0)) + 1
    if hint_counts:
        ordered = sorted(hint_counts.items(), key=lambda item: (-item[1], str(item[0]).lower()))
        return str(ordered[0][0])
    query_counts = cluster.get("query_counts", {})
    if isinstance(query_counts, dict) and query_counts:
        ordered = sorted(query_counts.items(), key=lambda item: (-int(item[1]), str(item[0]).lower()))
        return str(ordered[0][0])
    return str(cluster.get("representative") or "")


def _cluster_canonical(cluster: dict[str, Any], loose_candidates: list[tuple[float, dict[str, Any]]]) -> str:
    canonical_counts: dict[str, int] = {}
    for hint in cluster.get("hints", []):
        payload = _event_payload_from_record(hint)
        canonical = normalize_optional_string(payload.get("canonical")) or normalize_optional_string(hint.get("canonical"))
        if canonical:
            canonical_counts[canonical] = int(canonical_counts.get(canonical, 0)) + 1
    if canonical_counts:
        ordered = sorted(canonical_counts.items(), key=lambda item: (-item[1], str(item[0]).lower()))
        return str(ordered[0][0])
    if loose_candidates:
        top = loose_candidates[0][1]
        metadata = top.get("metadata") if isinstance(top.get("metadata"), dict) else {}
        title = normalize_optional_string(metadata.get("title")) if isinstance(metadata, dict) else None
        if title:
            return title
        return memory_preview(top, max_chars=96)
    return str(cluster.get("representative") or "")


def _alias_event_id(record: dict[str, Any]) -> str | None:
    event_id = normalize_optional_string(record.get("event_id"))
    if event_id:
        return event_id
    event_id = normalize_optional_string(record.get("id"))
    return event_id


def _proposal_source_events(cluster: dict[str, Any]) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for event in cluster.get("miss_events", []):
        if not isinstance(event, dict):
            continue
        event_id = _alias_event_id(event)
        if event_id:
            links.append({"event_id": event_id, "relation": "miss"})
    for event in cluster.get("hints", []):
        if not isinstance(event, dict):
            continue
        event_id = _alias_event_id(event)
        if event_id:
            links.append({"event_id": event_id, "relation": "hint"})
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in links:
        key = (str(item.get("event_id") or ""), str(item.get("relation") or "evidence"))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        deduped.append({"event_id": key[0], "relation": key[1]})
    return deduped


def _proposal_canonical_text(canonical: str, candidate_alias: str) -> str:
    canonical_value = canonical.strip()
    if canonical_value:
        return canonical_value
    return candidate_alias.strip()


def _proposal_language(args: dict[str, Any]) -> str:
    return _normalize_alias_language(args.get("language"))


def _persist_alias_proposals(proposals: list[dict[str, Any]]) -> tuple[int, int]:
    if not proposals:
        return 0, 0
    if store_backend() != "sqlite":
        raise RuntimeError("propose_aliases persistence requires sqlite backend")
    persisted = 0
    links_written = 0
    now = now_iso()
    with _sqlite_session() as conn:
        _sqlite_ensure_schema(conn)
        _sqlite_bootstrap_if_needed(conn)
        for proposal in proposals:
            proposal_id = str(proposal.get("proposal_id") or "").strip()
            if not proposal_id:
                continue
            domain = normalize_optional_string(proposal.get("domain"))
            language = _normalize_alias_language(proposal.get("language"))
            canonical = normalize_optional_string(proposal.get("canonical"))
            candidate_alias = normalize_optional_string(proposal.get("candidate_alias"))
            normalized_alias = _normalize_alias_term(candidate_alias)
            if not candidate_alias or not normalized_alias:
                continue
            score = float(proposal.get("score") or 0.0)
            recommendation = normalize_optional_string(proposal.get("recommendation")) or "review"
            evidence = proposal.get("evidence")
            evidence_json = json.dumps(evidence if isinstance(evidence, dict) else {}, ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO alias_proposals(
                    proposal_id, domain, language, canonical, candidate_alias, normalized_alias,
                    score, status, recommendation, evidence_json, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    domain=excluded.domain,
                    language=excluded.language,
                    canonical=excluded.canonical,
                    candidate_alias=excluded.candidate_alias,
                    normalized_alias=excluded.normalized_alias,
                    score=excluded.score,
                    recommendation=excluded.recommendation,
                    evidence_json=excluded.evidence_json,
                    status=CASE
                        WHEN alias_proposals.status IN ('approved', 'rejected') THEN alias_proposals.status
                        ELSE 'pending'
                    END,
                    updated_at=excluded.updated_at
                """,
                (
                    proposal_id,
                    domain,
                    language,
                    canonical,
                    candidate_alias,
                    normalized_alias,
                    score,
                    recommendation,
                    evidence_json,
                    now,
                    now,
                ),
            )
            persisted += 1
            source_events = proposal.get("source_events")
            if not isinstance(source_events, list):
                continue
            for item in source_events:
                if not isinstance(item, dict):
                    continue
                event_id = normalize_optional_string(item.get("event_id"))
                relation = normalize_optional_string(item.get("relation")) or "evidence"
                if not event_id:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO alias_proposal_events(proposal_id, event_id, relation) VALUES(?, ?, ?)",
                    (proposal_id, event_id, relation),
                )
                links_written += 1
    return persisted, links_written


def _alias_concept_row(conn: sqlite3.Connection, concept_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM alias_concepts WHERE concept_id = ?", (concept_id,)).fetchone()


def _alias_term_row(conn: sqlite3.Connection, alias_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM alias_terms WHERE alias_id = ?", (alias_id,)).fetchone()


def _alias_row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {str(key): row[key] for key in row.keys()}


def _alias_concept_id(canonical: str, domain: str | None, language: str) -> str:
    base = f"{language}|{domain or '-'}|{_normalize_alias_term(canonical)}"
    return "alias-concept-" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def _find_existing_alias_term(
    conn: sqlite3.Connection,
    *,
    concept_id: str,
    normalized_term: str,
    domain: str | None,
    language: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM alias_terms
        WHERE concept_id = ?
          AND normalized_term = ?
          AND COALESCE(NULLIF(domain, ''), '') = COALESCE(NULLIF(?, ''), '')
          AND COALESCE(NULLIF(language, ''), ?) = ?
        LIMIT 1
        """,
        (concept_id, normalized_term, domain, DEFAULT_ALIAS_LANGUAGE, language),
    ).fetchone()


def _list_alias_proposals_maintenance(args: dict[str, Any]) -> dict[str, Any]:
    status = normalize_optional_string(args.get("status")) or "pending"
    domain = normalize_optional_string(args.get("domain"))
    limit = _safe_int(args.get("limit"), DEFAULT_ALIAS_PROPOSAL_LIST_LIMIT, minimum=1, maximum=500)
    if store_backend() != "sqlite":
        return tool_error("list_alias_proposals requires sqlite backend")
    with _sqlite_session() as conn:
        _sqlite_ensure_schema(conn)
        _sqlite_bootstrap_if_needed(conn)
        clauses = ["status = ?"]
        params: list[Any] = [status]
        if domain is not None:
            clauses.append("domain = ?")
            params.append(domain)
        sql = "SELECT * FROM alias_proposals WHERE " + " AND ".join(clauses) + " ORDER BY score DESC, created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, tuple(params)).fetchall()
    proposals: list[dict[str, Any]] = []
    for row in rows:
        item = {str(key): row[key] for key in row.keys()}
        evidence_raw = item.get("evidence_json")
        if isinstance(evidence_raw, str):
            try:
                item["evidence"] = json.loads(evidence_raw)
            except json.JSONDecodeError:
                item["evidence"] = {"raw": evidence_raw}
        proposals.append(item)
    return text_result(
        f"list_alias_proposals: status={status} domain={domain or '-'} count={len(proposals)}",
        {
            "action": "list_alias_proposals",
            "status": status,
            "domain": domain,
            "proposals": proposals,
            "count": len(proposals),
        },
    )


def _approve_alias_maintenance(args: dict[str, Any]) -> dict[str, Any]:
    if store_backend() != "sqlite":
        return tool_error("approve_alias requires sqlite backend")
    proposal_id = normalize_optional_string(args.get("proposal_id"))
    concept_id = normalize_optional_string(args.get("concept_id"))
    canonical = normalize_optional_string(args.get("canonical"))
    candidate_alias = normalize_optional_string(args.get("candidate_alias"))
    domain = normalize_optional_string(args.get("domain"))
    language = _normalize_alias_language(args.get("language"))
    approved_by = normalize_optional_string(args.get("approved_by"))
    notes = normalize_optional_string(args.get("notes"))
    now = now_iso()

    proposal_row: sqlite3.Row | None = None
    with _sqlite_session() as conn:
        _sqlite_ensure_schema(conn)
        _sqlite_bootstrap_if_needed(conn)
        if proposal_id:
            proposal_row = conn.execute(
                "SELECT * FROM alias_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if proposal_row is None:
                return tool_error(f"proposal not found: {proposal_id}")
            if canonical is None:
                canonical = normalize_optional_string(proposal_row["canonical"])
            if candidate_alias is None:
                candidate_alias = normalize_optional_string(proposal_row["candidate_alias"])
            if domain is None:
                domain = normalize_optional_string(proposal_row["domain"])
            if "language" not in args:
                language = _normalize_alias_language(proposal_row["language"])
        if not candidate_alias:
            return tool_error("candidate_alias is required when proposal_id is not supplied")
        normalized_alias = _normalize_alias_term(candidate_alias)
        if not normalized_alias:
            return tool_error("candidate_alias must contain at least one non-space token")
        default_weight = 1.0
        if proposal_row is not None:
            default_weight = float(proposal_row["score"] or 1.0)
        weight = _safe_float(args.get("weight"), default_weight, minimum=0.0, maximum=5.0)

        if concept_id:
            existing_concept = _alias_concept_row(conn, concept_id)
        else:
            existing_concept = None
        if existing_concept is None:
            if not canonical:
                return tool_error("canonical is required when concept_id does not resolve an existing concept")
            canonical_value = _proposal_canonical_text(canonical, candidate_alias)
            concept_id = concept_id or _alias_concept_id(canonical_value, domain, language)
            existing_concept = _alias_concept_row(conn, concept_id)
            if existing_concept is None:
                conn.execute(
                    """
                    INSERT INTO alias_concepts(
                        concept_id, canonical, domain, language, status, weight,
                        created_at, updated_at, source, notes
                    ) VALUES(?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
                    """,
                    (
                        concept_id,
                        canonical_value,
                        domain,
                        language,
                        weight if weight > 0 else 1.0,
                        now,
                        now,
                        f"proposal:{proposal_id}" if proposal_id else "manual_approval",
                        notes,
                    ),
                )
            elif canonical:
                conn.execute(
                    """
                    UPDATE alias_concepts
                    SET canonical = ?, domain = COALESCE(?, domain), language = ?, status = 'active',
                        weight = CASE WHEN ? > 0 THEN ? ELSE weight END,
                        updated_at = ?, notes = COALESCE(?, notes)
                    WHERE concept_id = ?
                    """,
                    (canonical, domain, language, weight, weight, now, notes, concept_id),
                )
        else:
            concept_id = str(existing_concept["concept_id"])
            if canonical:
                conn.execute(
                    """
                    UPDATE alias_concepts
                    SET canonical = ?, domain = COALESCE(?, domain), language = ?, status = 'active',
                        weight = CASE WHEN ? > 0 THEN ? ELSE weight END,
                        updated_at = ?, notes = COALESCE(?, notes)
                    WHERE concept_id = ?
                    """,
                    (canonical, domain, language, weight, weight, now, notes, concept_id),
                )

        existing_alias = _find_existing_alias_term(
            conn,
            concept_id=concept_id,
            normalized_term=normalized_alias,
            domain=domain,
            language=language,
        )
        if existing_alias is None:
            alias_id = "alias-term-" + hashlib.sha1(
                f"{concept_id}|{domain or '-'}|{language}|{normalized_alias}".encode("utf-8")
            ).hexdigest()[:16]
            conn.execute(
                """
                INSERT INTO alias_terms(
                    alias_id, concept_id, term, normalized_term, language, domain, status, weight,
                    source, approved_by, approved_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
                """,
                (
                    alias_id,
                    concept_id,
                    candidate_alias,
                    normalized_alias,
                    language,
                    domain,
                    weight if weight > 0 else 1.0,
                    f"proposal:{proposal_id}" if proposal_id else "manual_approval",
                    approved_by,
                    now,
                    now,
                    now,
                ),
            )
        else:
            alias_id = str(existing_alias["alias_id"])
            conn.execute(
                """
                UPDATE alias_terms
                SET term = ?,
                    language = ?,
                    domain = COALESCE(?, domain),
                    status = 'active',
                    weight = CASE WHEN ? > 0 THEN ? ELSE weight END,
                    source = ?,
                    approved_by = COALESCE(?, approved_by),
                    approved_at = COALESCE(approved_at, ?),
                    updated_at = ?
                WHERE alias_id = ?
                """,
                (
                    candidate_alias,
                    language,
                    domain,
                    weight,
                    weight,
                    f"proposal:{proposal_id}" if proposal_id else "manual_approval",
                    approved_by,
                    now,
                    now,
                    alias_id,
                ),
            )
        if proposal_id:
            conn.execute(
                "UPDATE alias_proposals SET status = 'approved', updated_at = ? WHERE proposal_id = ?",
                (now, proposal_id),
            )
        concept_row = _alias_concept_row(conn, concept_id)
        alias_row = _alias_term_row(conn, alias_id)
        proposal_out = None
        if proposal_id:
            proposal_out = conn.execute(
                "SELECT * FROM alias_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
    return text_result(
        f"approve_alias: concept_id={concept_id} alias_term={candidate_alias}",
        {
            "action": "approve_alias",
            "concept": _alias_row_to_dict(concept_row),
            "alias": _alias_row_to_dict(alias_row),
            "proposal": _alias_row_to_dict(proposal_out),
        },
    )


def _reject_alias_proposal_maintenance(args: dict[str, Any]) -> dict[str, Any]:
    if store_backend() != "sqlite":
        return tool_error("reject_alias_proposal requires sqlite backend")
    proposal_id = normalize_optional_string(args.get("proposal_id"))
    if not proposal_id:
        return tool_error("proposal_id is required")
    reason = normalize_optional_string(args.get("reason"))
    now = now_iso()
    with _sqlite_session() as conn:
        _sqlite_ensure_schema(conn)
        _sqlite_bootstrap_if_needed(conn)
        exists = conn.execute(
            "SELECT proposal_id FROM alias_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if exists is None:
            return tool_error(f"proposal not found: {proposal_id}")
        conn.execute(
            """
            UPDATE alias_proposals
            SET status = 'rejected',
                recommendation = COALESCE(?, recommendation),
                updated_at = ?
            WHERE proposal_id = ?
            """,
            (reason, now, proposal_id),
        )
        row = conn.execute("SELECT * FROM alias_proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
    return text_result(
        f"reject_alias_proposal: proposal_id={proposal_id}",
        {
            "action": "reject_alias_proposal",
            "proposal": _alias_row_to_dict(row),
        },
    )


def _list_aliases_maintenance(args: dict[str, Any]) -> dict[str, Any]:
    if store_backend() != "sqlite":
        return tool_error("list_aliases requires sqlite backend")
    domain = normalize_optional_string(args.get("domain"))
    language = normalize_optional_string(args.get("language"))
    status = normalize_optional_string(args.get("status")) or "active"
    concept_id = normalize_optional_string(args.get("concept_id"))
    limit = _safe_int(args.get("limit"), DEFAULT_ALIAS_LIST_LIMIT, minimum=1, maximum=2000)
    with _sqlite_session() as conn:
        _sqlite_ensure_schema(conn)
        _sqlite_bootstrap_if_needed(conn)
        clauses = ["v.status = ?", "c.status = 'active'"]
        params: list[Any] = [status]
        if domain is not None:
            clauses.append("(COALESCE(NULLIF(v.domain, ''), '') = '' OR v.domain = ?)")
            params.append(domain)
        if language is not None:
            normalized_language = _normalize_alias_language(language)
            clauses.append("COALESCE(NULLIF(v.language, ''), ?) = ?")
            params.extend([DEFAULT_ALIAS_LANGUAGE, normalized_language])
        if concept_id is not None:
            clauses.append("v.concept_id = ?")
            params.append(concept_id)
        sql = (
            "SELECT "
            "v.domain AS domain, v.language AS language, v.concept_id AS concept_id, v.canonical AS canonical, "
            "t.alias_id AS alias_id, v.alias AS alias, v.normalized_term AS normalized_term, "
            "v.status AS status, v.weight AS weight, v.approved_at AS approved_at, v.approved_by AS approved_by, "
            "v.source AS source, v.updated_at AS updated_at "
            "FROM v_alias_vocabulary v "
            "JOIN alias_concepts c ON c.concept_id = v.concept_id "
            "JOIN alias_terms t ON t.concept_id = v.concept_id "
            "  AND t.normalized_term = v.normalized_term "
            "  AND t.term = v.alias "
            "WHERE " + " AND ".join(clauses) + " "
            "ORDER BY v.domain, v.language, v.concept_id, v.alias LIMIT ?"
        )
        params.append(limit)
        rows = conn.execute(sql, tuple(params)).fetchall()
    aliases = [{str(key): row[key] for key in row.keys()} for row in rows]
    return text_result(
        f"list_aliases: status={status} domain={domain or '-'} count={len(aliases)}",
        {
            "action": "list_aliases",
            "domain": domain,
            "language": language,
            "status": status,
            "aliases": aliases,
            "count": len(aliases),
        },
    )


def _disable_alias_maintenance(args: dict[str, Any]) -> dict[str, Any]:
    if store_backend() != "sqlite":
        return tool_error("disable_alias requires sqlite backend")
    alias_id = normalize_optional_string(args.get("alias_id"))
    concept_id = normalize_optional_string(args.get("concept_id"))
    term = normalize_optional_string(args.get("term"))
    reason = normalize_optional_string(args.get("reason"))
    now = now_iso()
    with _sqlite_session() as conn:
        _sqlite_ensure_schema(conn)
        _sqlite_bootstrap_if_needed(conn)
        row: sqlite3.Row | None = None
        if alias_id:
            row = conn.execute("SELECT * FROM alias_terms WHERE alias_id = ?", (alias_id,)).fetchone()
        elif concept_id and term:
            row = conn.execute(
                "SELECT * FROM alias_terms WHERE concept_id = ? AND normalized_term = ? LIMIT 1",
                (concept_id, _normalize_alias_term(term)),
            ).fetchone()
        else:
            return tool_error("disable_alias requires alias_id or concept_id + term")
        if row is None:
            return tool_error("alias term not found")
        conn.execute(
            "UPDATE alias_terms SET status = 'disabled', updated_at = ?, source = COALESCE(?, source) WHERE alias_id = ?",
            (now, reason, str(row["alias_id"])),
        )
        out_row = conn.execute("SELECT * FROM alias_terms WHERE alias_id = ?", (str(row["alias_id"]),)).fetchone()
    return text_result(
        f"disable_alias: alias_id={str(row['alias_id'])}",
        {
            "action": "disable_alias",
            "alias": _alias_row_to_dict(out_row),
        },
    )


def _disable_alias_concept_maintenance(args: dict[str, Any]) -> dict[str, Any]:
    if store_backend() != "sqlite":
        return tool_error("disable_alias_concept requires sqlite backend")
    concept_id = normalize_optional_string(args.get("concept_id"))
    if not concept_id:
        return tool_error("concept_id is required")
    reason = normalize_optional_string(args.get("reason"))
    now = now_iso()
    with _sqlite_session() as conn:
        _sqlite_ensure_schema(conn)
        _sqlite_bootstrap_if_needed(conn)
        row = _alias_concept_row(conn, concept_id)
        if row is None:
            return tool_error(f"alias concept not found: {concept_id}")
        conn.execute(
            "UPDATE alias_concepts SET status = 'disabled', updated_at = ?, notes = COALESCE(?, notes) WHERE concept_id = ?",
            (now, reason, concept_id),
        )
        out_row = _alias_concept_row(conn, concept_id)
    return text_result(
        f"disable_alias_concept: concept_id={concept_id}",
        {
            "action": "disable_alias_concept",
            "concept": _alias_row_to_dict(out_row),
        },
    )


def _propose_aliases_maintenance(args: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    window_days = _safe_int(args.get("window_days"), DEFAULT_ALIAS_PROPOSAL_WINDOW_DAYS, minimum=1, maximum=365)
    domain = normalize_optional_string(args.get("domain"))
    language = _proposal_language(args)
    min_recurrence = _safe_int(args.get("min_recurrence"), DEFAULT_ALIAS_PROPOSAL_MIN_RECURRENCE, minimum=1, maximum=100)
    limit = _safe_int(args.get("limit"), DEFAULT_ALIAS_PROPOSAL_LIMIT, minimum=1, maximum=100)
    include_hints = parse_bool(args.get("include_hints"), default=True)
    min_loose_score = _safe_float(args.get("min_loose_score"), DEFAULT_ALIAS_PROPOSAL_MIN_LOOSE_SCORE, minimum=0.0, maximum=1.0)
    max_candidates_per_cluster = _safe_int(
        args.get("max_candidates_per_cluster"),
        DEFAULT_ALIAS_PROPOSAL_MAX_CANDIDATES_PER_CLUSTER,
        minimum=1,
        maximum=20,
    )
    warnings: list[str] = []
    if not dry_run and store_backend() != "sqlite":
        return tool_error("propose_aliases persistence requires sqlite backend when dry_run=false")

    try:
        misses, hints = _load_recent_alias_source_events(
            window_days=window_days,
            domain=domain,
            include_hints=include_hints,
        )
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")
    if not misses and not hints:
        structured = {
            "action": "propose_aliases",
            "status": "no_misses",
            "window_days": window_days,
            "domain": domain,
            "language": language,
            "miss_event_count": 0,
            "hint_event_count": 0,
            "cluster_count": 0,
            "idf": {"used": False, "scope": "none", "status": "unavailable"},
            "proposals": [],
            "dry_run": dry_run,
            "persisted_count": 0,
            "proposal_event_links_written": 0,
            "warnings": warnings,
        }
        return text_result("propose_aliases: misses=0 clusters=0 proposals=0 idf=unavailable", structured)

    clusters = _cluster_miss_events(misses)
    if include_hints and hints:
        clusters = _attach_alias_hints(clusters, hints)
    clusters.sort(
        key=lambda item: (-len(item.get("miss_events", [])), -len(item.get("hints", [])), str(item.get("representative", "")).lower())
    )

    idf_state = _ensure_idf_profiles(trigger="maintenance")
    idf_selection = _resolve_idf_profile_for_memory_or_query(domain=domain, idf_state=idf_state)
    idf_used = bool(idf_selection.get("active")) and isinstance(idf_selection.get("profile"), dict)
    idf_scope = str(idf_selection.get("scope", "none")) if idf_used else "none"
    idf_status = str(idf_selection.get("status", "cold"))
    if not idf_used:
        warnings.append(
            "IDF profile is not active (cold/disabled/unavailable); alias proposals are withheld until IDF is ready."
        )
        structured = {
            "action": "propose_aliases",
            "status": "idf_cold",
            "window_days": window_days,
            "domain": domain,
            "language": language,
            "miss_event_count": len(misses),
            "hint_event_count": len(hints),
            "cluster_count": len(clusters),
            "idf": {"used": False, "scope": "none", "status": idf_status},
            "proposals": [],
            "dry_run": dry_run,
            "persisted_count": 0,
            "proposal_event_links_written": 0,
            "warnings": warnings,
        }
        return text_result(
            f"propose_aliases: misses={len(misses)} clusters={len(clusters)} proposals=0 idf={idf_status}",
            structured,
        )

    idf_profile = dict(idf_selection.get("profile") or {})
    proposals: list[dict[str, Any]] = []
    for cluster in clusters:
        recurrence = len(cluster.get("miss_events", []))
        strong_hint = any(_hint_confidence(hint) == "high" for hint in cluster.get("hints", []))
        if recurrence < min_recurrence and not strong_hint:
            continue
        candidate_alias = _cluster_candidate_alias(cluster).strip()
        if not candidate_alias:
            continue
        idf_terms, penalized_terms, idf_strength = _alias_idf_evidence(candidate_alias, idf_profile)
        if not idf_terms:
            continue
        cluster_domain = _proposal_domain(cluster, domain)
        try:
            loose_candidates = _alias_candidate_memories(
                str(cluster.get("representative") or candidate_alias),
                domain=cluster_domain,
                max_candidates_per_cluster=max_candidates_per_cluster,
                min_loose_score=min_loose_score,
            )
        except Exception:
            continue
        if not loose_candidates:
            continue
        canonical = _cluster_canonical(cluster, loose_candidates).strip()
        if not canonical:
            continue
        hint_count = len(cluster.get("hints", []))
        hint_bonus = min(0.35, (0.18 * hint_count) + (0.22 if strong_hint else 0.0))
        loose_top = float(loose_candidates[0][0])
        domain_consistent = 0.0
        if cluster_domain is not None:
            domain_consistent = 1.0 if any(
                normalize_optional_string(memory.get("domain")) in {cluster_domain, None}
                for _score, memory in loose_candidates
            ) else 0.0
        generic_penalty = min(0.30, 0.06 * len(penalized_terms))
        recurrence_score = min(1.0, float(recurrence) / float(max(1, min_recurrence)))
        final_score = (
            (0.34 * recurrence_score)
            + (0.28 * loose_top)
            + (0.22 * idf_strength)
            + (0.08 * domain_consistent)
            + hint_bonus
            - generic_penalty
        )
        bounded_score = max(0.0, min(1.0, final_score))
        miss_queries = sorted(
            (str(query) for query in cluster.get("query_counts", {}).keys()),
            key=lambda item: (-int(cluster.get("query_counts", {}).get(item, 0)), item.lower()),
        )[:6]
        hint_target_ids: list[str] = []
        for hint in cluster.get("hints", []):
            payload = _event_payload_from_record(hint)
            for memory_id in normalize_linked_ids(payload.get("target_memory_ids", [])):
                if memory_id not in hint_target_ids:
                    hint_target_ids.append(memory_id)
        target_memory_ids = list(hint_target_ids)
        for _loose, memory in loose_candidates:
            memory_id = str(memory.get("id") or "").strip()
            if memory_id and memory_id not in target_memory_ids:
                target_memory_ids.append(memory_id)
        proposal_id = "alias-prop-" + hashlib.sha1(
            f"{cluster_domain or '-'}|{canonical}|{candidate_alias}".encode("utf-8")
        ).hexdigest()[:12]
        proposals.append(
            {
                "proposal_id": proposal_id,
                "domain": cluster_domain,
                "language": language,
                "canonical": canonical,
                "candidate_alias": candidate_alias,
                "normalized_alias": _normalize_alias_term(candidate_alias),
                "score": round(bounded_score, 3),
                "recommendation": "review",
                "source_events": _proposal_source_events(cluster),
                "evidence": {
                    "recurrence": recurrence,
                    "miss_queries": miss_queries,
                    "hint_count": hint_count,
                    "target_memory_ids": target_memory_ids[:12],
                    "target_memory_previews": [memory_preview(memory, max_chars=120) for _score, memory in loose_candidates[:3]],
                    "loose_scores": [round(float(score), 3) for score, _memory in loose_candidates],
                    "idf_terms": idf_terms[:8],
                    "penalized_terms": penalized_terms[:8],
                },
            }
        )
        if len(proposals) >= limit:
            break

    proposals.sort(key=lambda item: (float(item.get("score", 0.0)), str(item.get("proposal_id", ""))), reverse=True)
    status = "ok" if proposals else "no_proposals"
    persisted_count = 0
    proposal_event_links_written = 0
    persisted_error: str | None = None
    if not dry_run and proposals:
        try:
            persisted_count, proposal_event_links_written = _persist_alias_proposals(proposals[:limit])
        except Exception as exc:
            persisted_error = f"{type(exc).__name__}: {exc}"
    idf_payload = {"used": idf_used, "scope": idf_scope, "status": idf_status}
    structured = {
        "action": "propose_aliases",
        "status": status,
        "window_days": window_days,
        "domain": domain,
        "language": language,
        "miss_event_count": len(misses),
        "hint_event_count": len(hints),
        "cluster_count": len(clusters),
        "idf": idf_payload,
        "dry_run": dry_run,
        "persisted_count": persisted_count,
        "proposal_event_links_written": proposal_event_links_written,
        "proposals": proposals[:limit],
        "warnings": warnings,
    }
    if persisted_error:
        structured["status"] = "persist_error"
        structured["persist_error"] = persisted_error
        return text_result(
            f"propose_aliases: misses={len(misses)} clusters={len(clusters)} proposals={len(proposals[:limit])} "
            f"idf={idf_status} persist_error={persisted_error}",
            structured,
        )
    top_lines = [
        f"{item['candidate_alias']} -> {item['canonical']} ({float(item['score']):.2f})"
        for item in proposals[:3]
    ]
    top_summary = "; ".join(top_lines) if top_lines else "none"
    return text_result(
        f"propose_aliases: misses={len(misses)} clusters={len(clusters)} proposals={len(proposals[:limit])} "
        f"idf={idf_status} dry_run={'yes' if dry_run else 'no'} persisted={persisted_count} top={top_summary}",
        structured,
    )


def memory_maintenance(args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action", "")).strip().lower()
    valid_actions = {
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
    }
    if action not in valid_actions:
        return tool_error(f"action must be one of: {', '.join(sorted(valid_actions))}")
    dry_run = parse_bool(args.get("dry_run"), default=True)
    if action == "compact_logs":
        return _compact_logs_maintenance(args, dry_run)
    if action == "import_json":
        return _import_json_maintenance(args, dry_run)
    if action == "backfill_signatures":
        return _backfill_signatures_maintenance(args, dry_run)
    if action == "propose_aliases":
        return _propose_aliases_maintenance(args, dry_run)
    if action == "list_alias_proposals":
        return _list_alias_proposals_maintenance(args)
    if action == "approve_alias":
        return _approve_alias_maintenance(args)
    if action == "reject_alias_proposal":
        return _reject_alias_proposal_maintenance(args)
    if action == "list_aliases":
        return _list_aliases_maintenance(args)
    if action == "disable_alias":
        return _disable_alias_maintenance(args)
    if action == "disable_alias_concept":
        return _disable_alias_concept_maintenance(args)
    if action == "consolidate_full":
        return _consolidate_full_maintenance(args)
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
    namespaces: list[str],
    origins: list[str] | None,
    salience_module: Any | None,
    idf_profile: dict[str, Any] | None = None,
    alias_runtime: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    visible = [
        memory
        for memory in store.get("memories", [])
        if isinstance(memory, dict)
        and visible_memory(memory, False, False)
        and _memory_in_scope(memory, namespaces, origins)
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
        scored_blocks = select_memories_by_query(
            block_candidates,
            query,
            max_blocks * 2,
            salience_module,
            idf_profile,
            alias_runtime,
        )
        blocks_by_id: dict[str, dict[str, Any]] = {}
        for memory in linked_blocks:
            blocks_by_id[str(memory.get("id"))] = memory
        for _, memory in scored_blocks:
            if len(blocks_by_id) >= max_blocks:
                break
            blocks_by_id.setdefault(str(memory.get("id")), memory)
        context_blocks = list(blocks_by_id.values())[:max_blocks]
        block_scores = {str(memory.get("id")): float(score) for score, memory in scored_blocks}

        hippocampus = [memory for memory in visible if str(memory.get("kind", "")) == "hippocampus_entry"]
        scored_hippocampus = select_memories_by_query(
            hippocampus,
            query,
            max_hippocampus,
            salience_module,
            idf_profile,
            alias_runtime,
        )
        hippocampus_entries = [memory for _, memory in scored_hippocampus]
        hippocampus_scores = {str(memory.get("id")): float(score) for score, memory in scored_hippocampus}

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
        feedback_scores: dict[str, float] = {}
        if query:
            for memory in feedback:
                feedback_scores[str(memory.get("id"))] = rank_against_query(
                    memory,
                    query,
                    salience_module,
                    idf_profile,
                    alias_runtime,
                )

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
            "context_blocks": [
                memory_bundle_item(memory, max_chars=1200, score=block_scores.get(str(memory.get("id"))))
                for memory in context_blocks
            ],
            "hippocampus_entries": [
                memory_bundle_item(memory, max_chars=900, score=hippocampus_scores.get(str(memory.get("id"))))
                for memory in hippocampus_entries
            ],
            "agent_feedback": [
                memory_bundle_item(memory, max_chars=600, score=feedback_scores.get(str(memory.get("id"))))
                for memory in feedback
            ],
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
        score += rank_against_query(memory, task, salience_module, idf_profile, alias_runtime) if task else 0.0
        if task and isinstance(alias_runtime, dict):
            alias_boost, alias_concepts = _alias_concept_score_for_memory(memory, alias_runtime)
            if alias_boost > 0.0:
                enriched = dict(memory)
                enriched["_alias_concept_score"] = alias_boost
                enriched["_alias_concepts"] = alias_concepts
                memory = enriched
        if score > 0:
            scored_feedback.append((score, memory))
    scored_feedback.sort(key=lambda item: (item[0], str(item[1].get("created_at", ""))), reverse=True)
    feedback = [memory for _, memory in scored_feedback[:max_feedback]]
    feedback_scores = {str(memory.get("id")): float(score) for score, memory in scored_feedback[:max_feedback]}

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
    scored_hippocampus = select_memories_by_query(
        hippocampus_candidates,
        task,
        max_hippocampus,
        salience_module,
        idf_profile,
        alias_runtime,
    )
    hippocampus_entries = [memory for _, memory in scored_hippocampus]

    block_candidates = [memory for memory in visible if str(memory.get("kind", "")) == "context_block"]
    if domain:
        block_candidates = [memory for memory in block_candidates if str(memory.get("domain", "")).strip() in {"", domain}]
    scored_blocks = select_memories_by_query(
        block_candidates,
        task,
        max_blocks,
        salience_module,
        idf_profile,
        alias_runtime,
    )
    context_blocks = [memory for _, memory in scored_blocks]
    block_scores = {str(memory.get("id")): float(score) for score, memory in scored_blocks}
    hippocampus_scores = {str(memory.get("id")): float(score) for score, memory in scored_hippocampus}

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
        "agent_feedback": [
            memory_bundle_item(memory, max_chars=600, score=feedback_scores.get(str(memory.get("id"))))
            for memory in feedback
        ],
        "hippocampus_entries": [
            memory_bundle_item(memory, max_chars=900, score=hippocampus_scores.get(str(memory.get("id"))))
            for memory in hippocampus_entries
        ],
        "context_blocks": [
            memory_bundle_item(memory, max_chars=1200, score=block_scores.get(str(memory.get("id"))))
            for memory in context_blocks
        ],
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


def _recall_event_metrics(structured: dict[str, Any], *, query_present: bool, threshold: float) -> dict[str, Any]:
    relevant_sections = ("context_blocks", "hippocampus_entries", "agent_feedback", "recent_logs", "pinned")
    result_count = 0
    scores: list[float] = []
    event_matches: list[dict[str, Any]] = []
    for key in relevant_sections:
        section = structured.get(key)
        if not isinstance(section, list):
            continue
        for item in section:
            if not isinstance(item, dict):
                continue
            result_count += 1
            memory_id = normalize_optional_string(item.get("id"))
            score = _event_float_value(item.get("score"))
            if score is not None:
                bounded_score = max(0.0, min(1.0, float(score)))
                scores.append(bounded_score)
                if memory_id:
                    event_matches.append({"id": memory_id, "score": bounded_score})
            elif memory_id:
                event_matches.append({"id": memory_id, "score": 0.0})
    top_score = max(scores) if scores else 0.0
    if query_present:
        if scores:
            success = 1 if any(score >= threshold for score in scores) else 0
        else:
            success = 1 if result_count > 0 else 0
    else:
        success = 1 if result_count > 0 else 0
    return {
        "result_count": result_count,
        "top_score": top_score,
        "success": success,
        "scores": scores,
        "event_matches": event_matches,
    }


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
    query_text = query or task
    language = _normalize_alias_language(args.get("language"))
    alias_runtime = _expand_query_with_aliases(query_text, domain=domain, language=language)
    try:
        if store_backend() == "sqlite":
            with _sqlite_session() as conn:
                _sqlite_ensure_schema(conn)
                _sqlite_bootstrap_if_needed(conn)
                resolved_namespaces, resolved_origins = resolve_namespace_origin_filters(args, conn)
        else:
            resolved_namespaces, resolved_origins = resolve_namespace_origin_filters(args, None)
    except Exception as exc:
        return tool_error(str(exc))

    salience, _ = load_optional_agent_salience()
    recall_idf_profile: dict[str, Any] | None = None
    idf_choice: dict[str, Any] = {
        "scope": "none",
        "status": "not_requested",
        "active": False,
    }
    if salience is not None:
        try:
            idf_state = _ensure_idf_profiles(trigger="salience_check")
            idf_choice = _resolve_idf_profile_for_memory_or_query(domain=domain, idf_state=idf_state)
            if bool(idf_choice.get("active")) and isinstance(idf_choice.get("profile"), dict):
                recall_idf_profile = dict(idf_choice["profile"])
        except Exception:
            recall_idf_profile = None
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
        namespaces=list(resolved_namespaces),
        origins=resolved_origins,
        salience_module=salience,
        idf_profile=recall_idf_profile,
        alias_runtime=alias_runtime,
    )
    structured = _apply_recall_output_caps(structured)
    structured["namespaces"] = list(resolved_namespaces)
    structured["origins"] = list(resolved_origins) if resolved_origins is not None else None
    structured["idf_used"] = bool(recall_idf_profile)
    structured["idf_scope_used"] = str(idf_choice.get("scope", "none")) if recall_idf_profile else "none"
    structured["idf_profile_status"] = str(idf_choice.get("status", "not_requested"))
    structured["score_weights"] = dict(IDF_ACTIVE_WEIGHTS) if recall_idf_profile else {}
    alias_items: list[dict[str, Any]] = []
    for key in ("context_blocks", "hippocampus_entries", "agent_feedback", "recent_logs", "pinned"):
        section = structured.get(key)
        if isinstance(section, list):
            alias_items.extend(item for item in section if isinstance(item, dict))
    structured.update(_alias_diagnostics_payload(alias_runtime, alias_items))
    recall_metrics = _recall_event_metrics(
        structured,
        query_present=bool(str(query_text).strip()),
        threshold=miss_top_score_threshold(),
    )
    miss = int(recall_metrics["success"]) == 0
    append_query_log(
        "mnemo_recall",
        args,
        recall_metrics["event_matches"],
        query_text=str(query_text).strip() or None,
        domain=domain,
        result_count=int(recall_metrics["result_count"]),
        top_score=float(recall_metrics["top_score"]),
        success=int(recall_metrics["success"]),
        include_in_salience=True if miss else None,
        summary=(
            f"mnemo_recall: mode={mode} result_count={int(recall_metrics['result_count'])} "
            f"top_score={float(recall_metrics['top_score']):.3f} success={int(recall_metrics['success'])}"
        ),
        action="mnemo_recall",
        extra={
            "mode": mode,
            "query_present": bool(str(query_text).strip()),
            "score_threshold": miss_top_score_threshold(),
            "score_count": len(recall_metrics["scores"]),
        },
    )
    return text_result(summary, structured)


def memory_alias_hint(args: dict[str, Any]) -> dict[str, Any]:
    domain = normalize_optional_string(args.get("domain"))
    canonical = normalize_optional_string(args.get("canonical"))
    candidate_alias = normalize_optional_string(args.get("candidate_alias"))
    original_query = normalize_optional_string(args.get("original_query"))
    successful_query = normalize_optional_string(args.get("successful_query"))
    evidence = normalize_optional_string(args.get("evidence"))
    confidence = (normalize_optional_string(args.get("confidence")) or "medium").lower()
    if confidence not in {"low", "medium", "high"}:
        return tool_error("confidence must be one of: low, medium, high")
    if not candidate_alias and not original_query:
        return tool_error("candidate_alias or original_query is required")
    if not canonical and not successful_query:
        return tool_error("canonical or successful_query is required")

    hint_query = original_query or candidate_alias or successful_query or canonical or ""
    try:
        target_memory_ids = normalize_linked_ids(args.get("target_memory_ids", []))
    except ValueError as exc:
        return tool_error(str(exc))
    include_in_salience = parse_bool(args.get("include_in_salience"), default=True)
    payload = {
        "action": "alias_hint",
        "domain": domain,
        "canonical": canonical,
        "candidate_alias": candidate_alias,
        "original_query": original_query,
        "successful_query": successful_query,
        "target_memory_ids": target_memory_ids,
        "evidence": evidence,
        "confidence": confidence,
        "include_in_salience": include_in_salience,
    }
    append_query_log(
        "alias_hint",
        args,
        [],
        query_text=hint_query or None,
        domain=domain,
        result_count=0,
        top_score=0.0,
        success=1,
        include_in_salience=include_in_salience,
        summary=(
            f"alias_hint: candidate={candidate_alias or '-'} canonical={canonical or successful_query or '-'} "
            f"confidence={confidence}"
        ),
        action="alias_hint",
        extra=payload,
    )
    return text_result(
        "Recorded alias_hint event.",
        {
            "action": "alias_hint",
            "domain": domain,
            "canonical": canonical,
            "candidate_alias": candidate_alias,
            "original_query": original_query,
            "successful_query": successful_query,
            "target_memory_ids": target_memory_ids,
            "evidence": evidence,
            "confidence": confidence,
            "include_in_salience": include_in_salience,
        },
    )


def topic_add(args: dict[str, Any]) -> dict[str, Any]:
    if store_backend() != "sqlite":
        return tool_error("topic actions require sqlite backend")
    memory_id = str(args.get("memory_id", "")).strip()
    if not memory_id:
        return tool_error("memory_id is required")
    try:
        topic = normalize_topic(args.get("topic"))
    except ValueError as exc:
        return tool_error(str(exc))
    source = normalize_optional_string(args.get("source")) or "agent"
    created_at = now_iso()
    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            _sqlite_bootstrap_if_needed(conn)
            exists = conn.execute("SELECT 1 FROM memories WHERE id = ? LIMIT 1", (memory_id,)).fetchone()
            if exists is None:
                return tool_error(f"memory not found: {memory_id}")
            before = int(conn.total_changes)
            conn.execute(
                """
                INSERT OR IGNORE INTO memory_topics(memory_id, topic, created_at, source)
                VALUES(?, ?, ?, ?)
                """,
                (memory_id, topic, created_at, source),
            )
            inserted = int(conn.total_changes) > before
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")
    return text_result(
        f"Topic {'added' if inserted else 'already present'} for {memory_id}: {topic}",
        {
            "ok": True,
            "memory_id": memory_id,
            "topic": topic,
            "source": source,
            "inserted": bool(inserted),
        },
    )


def topic_remove(args: dict[str, Any]) -> dict[str, Any]:
    if store_backend() != "sqlite":
        return tool_error("topic actions require sqlite backend")
    memory_id = str(args.get("memory_id", "")).strip()
    if not memory_id:
        return tool_error("memory_id is required")
    try:
        topic = normalize_topic(args.get("topic"))
    except ValueError as exc:
        return tool_error(str(exc))
    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            _sqlite_bootstrap_if_needed(conn)
            before = int(conn.total_changes)
            conn.execute("DELETE FROM memory_topics WHERE memory_id = ? AND topic = ?", (memory_id, topic))
            removed = int(conn.total_changes) - before
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")
    return text_result(
        f"Removed {removed} topic row(s) for {memory_id}.",
        {
            "ok": True,
            "memory_id": memory_id,
            "topic": topic,
            "removed": int(max(0, removed)),
        },
    )


def topic_list(args: dict[str, Any]) -> dict[str, Any]:
    if store_backend() != "sqlite":
        return tool_error("topic actions require sqlite backend")
    scope = str(args.get("scope", "all")).strip().lower() or "all"
    if scope not in {"all", "memory"}:
        return tool_error("scope must be one of: all, memory")
    memory_id = str(args.get("memory_id", "")).strip()
    if scope == "memory" and not memory_id:
        return tool_error("memory_id is required when scope=memory")
    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            _sqlite_bootstrap_if_needed(conn)
            if scope == "all":
                rows = conn.execute(
                    """
                    SELECT topic, COUNT(*) AS count
                    FROM memory_topics
                    GROUP BY topic
                    ORDER BY count DESC, topic ASC
                    """
                ).fetchall()
                topics = [{"topic": str(row["topic"]), "count": int(row["count"])} for row in rows]
            else:
                rows = conn.execute(
                    """
                    SELECT topic, created_at, source
                    FROM memory_topics
                    WHERE memory_id = ?
                    ORDER BY topic ASC
                    """,
                    (memory_id,),
                ).fetchall()
                topics = [
                    {
                        "topic": str(row["topic"]),
                        "created_at": normalize_optional_string(row["created_at"]),
                        "source": normalize_optional_string(row["source"]),
                    }
                    for row in rows
                ]
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")
    if scope == "all":
        lines = [f"Topics ({len(topics)}):"]
        lines.extend(f"- {item['topic']}: {item['count']}" for item in topics[:50])
        return text_result("\n".join(lines), {"ok": True, "scope": "all", "topics": topics, "count": len(topics)})
    lines = [f"Topics for {memory_id} ({len(topics)}):"]
    lines.extend(f"- {item['topic']}" for item in topics[:50])
    return text_result(
        "\n".join(lines),
        {
            "ok": True,
            "scope": "memory",
            "memory_id": memory_id,
            "topics": topics,
            "count": len(topics),
        },
    )


PACK_REDACTION_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "private_key_header",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        "[REDACTED:private_key_header]",
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
        "[REDACTED:jwt]",
    ),
    (
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
        "[REDACTED:aws_access_key]",
    ),
    (
        "email",
        re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"),
        "[REDACTED:email]",
    ),
    (
        "user_path",
        re.compile(r"(?:/home/[^/\s]+/[^\s]*|/Users/[^/\s]+/[^\s]*|[A-Za-z]:\\Users\\[^\\/\s]+\\[^\s]*)"),
        "[REDACTED:user_path]",
    ),
    (
        "ipv4",
        re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
        "[REDACTED:ipv4]",
    ),
)


def _pack_kind_policy_warnings(args: dict[str, Any], kinds: list[str]) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    if "kinds" in args and args.get("kinds") is not None:
        for preview_only_kind in PACK_PREVIEW_POLICY_WARNING_KINDS:
            if preview_only_kind in kinds:
                warnings.append(
                    {
                        "code": "kind_preview_only",
                        "message": f"{preview_only_kind} rows are previewable but not exportable in v1 policy.",
                    }
                )
    return warnings


def _pack_parse_common_filters(args: dict[str, Any]) -> dict[str, Any]:
    topics = normalize_optional_string_list(args.get("topics"), "topics") or []
    kinds_raw = normalize_optional_string_list(args.get("kinds"), "kinds")
    if kinds_raw is None:
        kinds = list(PACK_PREVIEW_DEFAULT_KINDS)
    else:
        if not kinds_raw:
            raise ValueError("kinds must contain at least one value")
        seen_kinds: set[str] = set()
        kinds = []
        for item in kinds_raw:
            value = str(item).strip().lower()
            if not value or value in seen_kinds:
                continue
            seen_kinds.add(value)
            kinds.append(value)
        if not kinds:
            raise ValueError("kinds must contain at least one value")
    memory_ids = normalize_optional_string_list(args.get("memory_ids"), "memory_ids") or []
    seen_memory_ids: set[str] = set()
    deduped_memory_ids: list[str] = []
    for memory_id in memory_ids:
        value = str(memory_id).strip()
        if not value or value in seen_memory_ids:
            continue
        seen_memory_ids.add(value)
        deduped_memory_ids.append(value)
    group_id = normalize_optional_string(args.get("group_id"))
    scope = normalize_choice(
        args.get("scope"),
        "scope",
        ("core", "core_plus_related", "full_tree"),
        default="core_plus_related",
        strict=True,
    ) or "core_plus_related"
    if group_id is not None and topics:
        raise PackSelectorError("ambiguous_selector", "group_id cannot be combined with topics")
    if group_id is not None and deduped_memory_ids:
        raise PackSelectorError("ambiguous_selector", "group_id cannot be combined with memory_ids")
    raw_touched_paths = normalize_optional_string_list(args.get("touched_paths"), "touched_paths") or []
    touched_paths = normalize_touched_paths(args.get("touched_paths")) or []
    created_after = normalize_iso_utc_timestamp(args.get("created_after"), "created_after")
    created_before = normalize_iso_utc_timestamp(args.get("created_before"), "created_before")
    if created_after and created_before and created_after > created_before:
        raise ValueError("created_after must be <= created_before")
    limit = _safe_int(args.get("limit"), 100, minimum=1, maximum=1000)
    return {
        "topics": topics,
        "kinds": kinds,
        "memory_ids": deduped_memory_ids,
        "group_id": group_id,
        "scope": scope,
        "raw_touched_paths": raw_touched_paths,
        "touched_paths": touched_paths,
        "created_after": created_after,
        "created_before": created_before,
        "limit": limit,
    }


def _pack_selection_context(
    conn: sqlite3.Connection,
    args: dict[str, Any],
    parsed_filters: dict[str, Any],
    warnings: list[dict[str, str]],
) -> dict[str, Any]:
    resolved_namespaces, resolved_origins = resolve_namespace_origin_filters(args, conn)
    topics: list[str] = list(parsed_filters["topics"])
    kinds: list[str] = list(parsed_filters["kinds"])
    memory_ids_input: list[str] = list(parsed_filters.get("memory_ids", []))
    group_id = normalize_optional_string(parsed_filters.get("group_id"))
    scope = normalize_optional_string(parsed_filters.get("scope")) or "core_plus_related"
    raw_touched_paths: list[str] = list(parsed_filters["raw_touched_paths"])
    touched_paths: list[str] = list(parsed_filters["touched_paths"])
    created_after = normalize_optional_string(parsed_filters.get("created_after"))
    created_before = normalize_optional_string(parsed_filters.get("created_before"))
    limit = int(parsed_filters["limit"])

    if group_id is not None:
        catalog = _memory_group_build_catalog(conn, args, [])
        group_selection = _memory_group_resolve_selection(catalog, group_id, scope)
        row_map = dict(group_selection["row_map"])
        ordered_ids = [
            memory_id
            for memory_id in group_selection["ordered_ids"]
            if memory_id in row_map and str(row_map[memory_id].get("kind", "")) in kinds
        ]
        selected_ids = list(ordered_ids[:limit])
        return {
            "topics": [],
            "kinds": kinds,
            "memory_ids": [],
            "resolved_memory_ids": selected_ids,
            "group_id": group_id,
            "scope": scope,
            "raw_touched_paths": [],
            "touched_paths": [],
            "created_after": None,
            "created_before": None,
            "limit": limit,
            "resolved_namespaces": list(group_selection["resolved_namespaces"]),
            "resolved_origins": group_selection["resolved_origins"],
            "where_sql": "m.id IN ({})".format(",".join("?" for _ in selected_ids)) if selected_ids else "1 = 0",
            "sql_params": selected_ids,
            "created_column": None,
            "order_sql": "m.id ASC",
            "total_rows": len(ordered_ids),
            "row_ids": selected_ids,
            "selected_rows": [dict(row_map[memory_id]) for memory_id in selected_ids],
            "limited": bool(len(ordered_ids) > limit),
            "selector_mode": "group_id",
        }

    column_rows = conn.execute("PRAGMA table_info(memories)").fetchall()
    memory_columns = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1]).strip()
        for row in column_rows
    }
    created_column = "created_at" if "created_at" in memory_columns else None
    if created_column is None and (created_after is not None or created_before is not None):
        warnings.append(
            {
                "code": "date_filter_unavailable",
                "message": "created_at timestamp column unavailable; created_after/created_before were ignored.",
            }
        )
        created_after = None
        created_before = None

    clauses = [
        "m.deleted = 0",
        "(m.superseded_by IS NULL OR m.superseded_by = '')",
    ]
    sql_params: list[Any] = []

    namespace_placeholders = ",".join("?" for _ in resolved_namespaces)
    clauses.append(f"m.namespace IN ({namespace_placeholders})")
    sql_params.extend(resolved_namespaces)

    if resolved_origins:
        origin_placeholders = ",".join("?" for _ in resolved_origins)
        clauses.append(f"m.origin IN ({origin_placeholders})")
        sql_params.extend(resolved_origins)

    if kinds:
        kind_placeholders = ",".join("?" for _ in kinds)
        clauses.append(f"m.kind IN ({kind_placeholders})")
        sql_params.extend(kinds)

    known_memory_ids: list[str] = []
    if memory_ids_input:
        rows = conn.execute(
            f"SELECT id FROM memories WHERE id IN ({','.join('?' for _ in memory_ids_input)}) ORDER BY id ASC",
            tuple(memory_ids_input),
        ).fetchall()
        known_memory_ids = [str(row["id"] if isinstance(row, sqlite3.Row) else row[0]) for row in rows]
        unknown_memory_ids = [memory_id for memory_id in memory_ids_input if memory_id not in set(known_memory_ids)]
        if unknown_memory_ids:
            warnings.append(
                {
                    "code": "unknown_memory_ids_ignored",
                    "message": f"{len(unknown_memory_ids)} memory_ids were not found and were ignored.",
                }
            )
        if known_memory_ids:
            memory_id_placeholders = ",".join("?" for _ in known_memory_ids)
            clauses.append(f"m.id IN ({memory_id_placeholders})")
            sql_params.extend(known_memory_ids)
        else:
            clauses.append("1 = 0")

    if topics:
        topic_placeholders = ",".join("?" for _ in topics)
        clauses.append(
            "EXISTS ("
            "SELECT 1 FROM memory_topics mt "
            "WHERE mt.memory_id = m.id "
            f"AND mt.topic IN ({topic_placeholders})"
            ")"
        )
        sql_params.extend(topics)

    if touched_paths:
        path_clauses: list[str] = []
        if raw_touched_paths:
            raw_placeholders = ",".join("?" for _ in raw_touched_paths)
            path_clauses.append(f"mf_path.path IN ({raw_placeholders})")
        normalized_placeholders = ",".join("?" for _ in touched_paths)
        normalized_expr = "REPLACE(REPLACE(mf_path.path, char(92), '/'), './', '')"
        path_clauses.append(f"{normalized_expr} IN ({normalized_placeholders})")
        clauses.append(
            "EXISTS ("
            "SELECT 1 FROM memory_files mf_path "
            "WHERE mf_path.memory_id = m.id "
            "AND mf_path.memory_table = m.kind "
            f"AND ({' OR '.join(path_clauses)})"
            ")"
        )
        sql_params.extend(raw_touched_paths)
        sql_params.extend(touched_paths)

    if created_column is not None and created_after is not None:
        clauses.append(f"m.{created_column} >= ?")
        sql_params.append(created_after)
    if created_column is not None and created_before is not None:
        clauses.append(f"m.{created_column} <= ?")
        sql_params.append(created_before)

    where_sql = " AND ".join(clauses)
    order_sql = f"COALESCE(m.{created_column}, '') DESC, m.id ASC" if created_column else "m.id ASC"

    total_rows = int(conn.execute(f"SELECT COUNT(*) FROM memories m WHERE {where_sql}", tuple(sql_params)).fetchone()[0])
    row_id_rows = conn.execute(
        f"SELECT m.id FROM memories m WHERE {where_sql} ORDER BY {order_sql} LIMIT ?",
        tuple(sql_params + [limit]),
    ).fetchall()
    row_ids = [str(row["id"] if isinstance(row, sqlite3.Row) else row[0]) for row in row_id_rows]

    selected_rows_raw = conn.execute(
        "SELECT m.id, m.kind, m.text, m.title, m.preview, m.created_at, m.updated_at, "
        "m.namespace, m.origin, m.import_freshness, m.git_sha, m.git_branch, m.git_dirty "
        f"FROM memories m WHERE {where_sql} ORDER BY {order_sql} LIMIT ?",
        tuple(sql_params + [limit]),
    ).fetchall()
    selected_rows = [
        {
            "id": str(row["id"] if isinstance(row, sqlite3.Row) else row[0]),
            "kind": str(row["kind"] if isinstance(row, sqlite3.Row) else row[1]),
            "text": str(row["text"] if isinstance(row, sqlite3.Row) else row[2]),
            "title": normalize_optional_string(row["title"] if isinstance(row, sqlite3.Row) else row[3]),
            "preview": normalize_optional_string(row["preview"] if isinstance(row, sqlite3.Row) else row[4]),
            "created_at": normalize_optional_string(row["created_at"] if isinstance(row, sqlite3.Row) else row[5]),
            "updated_at": normalize_optional_string(row["updated_at"] if isinstance(row, sqlite3.Row) else row[6]),
            "namespace": str(row["namespace"] if isinstance(row, sqlite3.Row) else row[7]),
            "origin": str(row["origin"] if isinstance(row, sqlite3.Row) else row[8]),
            "import_freshness": normalize_optional_string(
                row["import_freshness"] if isinstance(row, sqlite3.Row) else row[9]
            ),
            "git_sha": normalize_optional_string(row["git_sha"] if isinstance(row, sqlite3.Row) else row[10]),
            "git_branch": normalize_optional_string(row["git_branch"] if isinstance(row, sqlite3.Row) else row[11]),
            "git_dirty": normalize_git_dirty(row["git_dirty"] if isinstance(row, sqlite3.Row) else row[12]),
        }
        for row in selected_rows_raw
    ]

    return {
        "topics": topics,
        "kinds": kinds,
        "memory_ids": memory_ids_input,
        "resolved_memory_ids": known_memory_ids,
        "group_id": group_id,
        "scope": scope,
        "raw_touched_paths": raw_touched_paths,
        "touched_paths": touched_paths,
        "created_after": created_after,
        "created_before": created_before,
        "limit": limit,
        "resolved_namespaces": resolved_namespaces,
        "resolved_origins": resolved_origins,
        "where_sql": where_sql,
        "sql_params": sql_params,
        "created_column": created_column,
        "order_sql": order_sql,
        "total_rows": total_rows,
        "row_ids": row_ids,
        "selected_rows": selected_rows,
        "limited": bool(total_rows > limit),
        "selector_mode": "memory_ids" if memory_ids_input else "topics" if topics else "filters",
    }


def _pack_redaction_source_text(row: dict[str, Any]) -> str:
    parts: list[str] = []
    for field in PACK_REDACTION_TEXT_FIELDS:
        if field not in row:
            continue
        value_raw = row.get(field)
        if value_raw is None:
            continue
        value = str(value_raw)
        if value:
            parts.append(value)
    return "\n".join(parts)


def _pack_redaction_apply(text: str) -> tuple[str, dict[str, int], int]:
    source = str(text)
    if not source:
        return source, {}, 0

    consumed = [False] * len(source)
    replacements: list[tuple[int, int, str, str]] = []
    counts: dict[str, int] = {}
    for category, pattern, replacement in PACK_REDACTION_RULES:
        for match in pattern.finditer(source):
            start, end = match.span()
            if end <= start:
                continue
            if any(consumed[idx] for idx in range(start, end)):
                continue
            for idx in range(start, end):
                consumed[idx] = True
            replacements.append((start, end, replacement, category))
            counts[category] = int(counts.get(category, 0) + 1)

    if not replacements:
        return source, {}, 0

    replacements.sort(key=lambda item: (item[0], item[1]))
    chunks: list[str] = []
    cursor = 0
    for start, end, replacement, _category in replacements:
        if start < cursor:
            continue
        chunks.append(source[cursor:start])
        chunks.append(replacement)
        cursor = end
    chunks.append(source[cursor:])
    redacted = "".join(chunks)
    total = int(sum(counts.values()))
    return redacted, counts, total


def _pack_row_redaction(row: dict[str, Any]) -> dict[str, Any]:
    text_fields: dict[str, str] = {}
    by_category: dict[str, int] = {}
    total_matches = 0
    for field in PACK_REDACTION_TEXT_FIELDS:
        if field not in row:
            continue
        raw_value = row.get(field)
        if raw_value is None:
            continue
        redacted_value, field_counts, field_total = _pack_redaction_apply(str(raw_value))
        text_fields[field] = redacted_value
        total_matches += int(field_total)
        for category_name in PACK_REDACTION_RULE_ORDER:
            hit_count = int(field_counts.get(category_name, 0))
            if hit_count <= 0:
                continue
            by_category[category_name] = int(by_category.get(category_name, 0) + hit_count)
    categories = [name for name in PACK_REDACTION_RULE_ORDER if int(by_category.get(name, 0)) > 0]
    return {
        "text_fields": text_fields,
        "by_category": by_category,
        "categories": categories,
        "total_matches": int(total_matches),
        "redacted_preview": collapsed_preview_text(
            "\n".join(text_fields.get(name, "") for name in PACK_REDACTION_TEXT_FIELDS if name in text_fields),
            max_chars=300,
        ),
    }


def _pack_baseline_warning() -> dict[str, str]:
    return {
        "code": "redaction_ruleset_baseline_only",
        "message": PACK_BASELINE_WARNING_MESSAGE,
    }


def _pack_make_pack_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"pack_{stamp}_{secrets.token_hex(4)}"


def _pack_make_promotion_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"promotion_{stamp}_{secrets.token_hex(4)}"


def _normalize_signer_id(raw_signer_id: Any) -> str:
    signer_id = normalize_optional_string(raw_signer_id)
    if signer_id is None:
        raise ValueError("signer_id is required")
    if not PACK_SIGNER_ID_RE.match(signer_id):
        raise ValueError("signer_id must match ^[A-Za-z0-9._:-]{3,128}$")
    return signer_id


def _normalize_signer_trust_level(raw_trust_level: Any, *, default: str = "trusted") -> str:
    trust_level = normalize_optional_string(raw_trust_level)
    if trust_level is None:
        trust_level = default
    trust_level = str(trust_level).strip().lower()
    if trust_level not in {"trusted", "blocked"}:
        raise ValueError("trust_level must be trusted or blocked")
    return trust_level


def _normalize_signer_status(raw_status: Any) -> str:
    status = normalize_optional_string(raw_status)
    if status is None:
        return "active"
    status = str(status).strip().lower()
    if status not in {"active", "disabled"}:
        raise ValueError("status must be active or disabled")
    return status


def _sanitize_pack_name(raw_name: Any) -> str:
    value = normalize_optional_string(raw_name)
    if value is None:
        raise ValueError("pack_name is required")
    value = value.strip()
    if not value:
        raise ValueError("pack_name is required")
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    if not sanitized:
        raise ValueError("pack_name is empty after sanitization")
    if sanitized.startswith("."):
        raise ValueError("pack_name cannot start with '.'")
    sanitized = sanitized[:64]
    if not sanitized:
        raise ValueError("pack_name is empty after sanitization")
    if sanitized.upper() in PACK_RESERVED_BASENAMES:
        raise ValueError(f"pack_name '{sanitized}' is reserved on Windows")
    return sanitized


def _pack_output_dir(raw_output_dir: Any) -> Path:
    configured = normalize_optional_string(raw_output_dir)
    if configured is None:
        resolved = (state_dir() / "packs" / "exports").resolve()
    else:
        resolved = Path(configured).expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _pack_output_path(output_dir: Path, sanitized_pack_name: str, pack_id: str) -> Path:
    filename = f"{sanitized_pack_name}_{pack_id}{PACK_FILE_EXTENSION}"
    final_path = (output_dir / filename).resolve()
    if final_path.parent != output_dir.resolve():
        raise ValueError("unsafe export output path")
    return final_path


def _pack_landing_matches(path: Path, *, include_legacy_zip: bool) -> bool:
    suffix = path.suffix.lower()
    if suffix == PACK_FILE_EXTENSION:
        return True
    if include_legacy_zip and suffix == PACK_LEGACY_FILE_EXTENSION:
        return True
    return False


def pack_landing_list(args: dict[str, Any]) -> dict[str, Any]:
    include_legacy_zip = parse_bool(args.get("include_legacy_zip"), default=False)
    limit = _safe_int(args.get("limit"), 20, minimum=1, maximum=200)
    landing_dir = pack_landing_dir()
    warnings: list[dict[str, str]] = []
    packs: list[dict[str, Any]] = []

    if not landing_dir.exists():
        warnings.append(
            {
                "code": "landing_dir_missing",
                "message": f"landing folder does not exist yet: {landing_dir}",
            }
        )
    elif not landing_dir.is_dir():
        return tool_error_code("landing_dir_not_directory", f"landing folder is not a directory: {landing_dir}")
    else:
        try:
            candidates = [path for path in landing_dir.iterdir() if path.is_file() and _pack_landing_matches(path, include_legacy_zip=include_legacy_zip)]
        except Exception as exc:
            return tool_error_code("landing_dir_read_failed", f"{type(exc).__name__}: {exc}")
        candidates.sort(key=lambda path: (-int(path.stat().st_mtime_ns), path.name.lower(), path.name))
        limited = len(candidates) > limit
        for path in candidates[:limit]:
            stat = path.stat()
            packs.append(
                {
                    "filename": path.name,
                    "path": str(path.resolve()),
                    "size_bytes": int(stat.st_size),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "suffix": path.suffix.lower(),
                    "legacy_zip": path.suffix.lower() == PACK_LEGACY_FILE_EXTENSION,
                }
            )
        structured = {
            "action": "pack_landing_list",
            "status": "ok",
            "landing_dir": str(landing_dir),
            "landing_dir_exists": True,
            "include_legacy_zip": include_legacy_zip,
            "total": len(candidates),
            "limited": limited,
            "limit": limit,
            "packs": packs,
            "warnings": warnings,
        }
        lines = [
            f"Landing packs: {len(candidates)} (limited={str(limited).lower()}, limit={limit})",
            f"Landing dir: {landing_dir}",
        ]
        return text_result("\n".join(lines), structured)

    structured = {
        "action": "pack_landing_list",
        "status": "ok",
        "landing_dir": str(landing_dir),
        "landing_dir_exists": False,
        "include_legacy_zip": include_legacy_zip,
        "total": 0,
        "limited": False,
        "limit": limit,
        "packs": [],
        "warnings": warnings,
    }
    lines = [
        "Landing packs: 0 (limited=false, limit={0})".format(limit),
        f"Landing dir: {landing_dir}",
    ]
    return text_result("\n".join(lines), structured)


def _pack_suffix_flags(pack_path: Path) -> tuple[bool, bool]:
    ext = pack_path.suffix.lower()
    return ext == PACK_LEGACY_FILE_EXTENSION, ext not in {PACK_FILE_EXTENSION, PACK_LEGACY_FILE_EXTENSION}


def _signer_row_payload(row: sqlite3.Row | tuple[Any, ...] | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        getter = row.__getitem__
    elif isinstance(row, dict):
        getter = row.__getitem__
    else:
        raise ValueError("invalid signer row")
    return {
        "signer_id": str(getter("signer_id")),
        "label": normalize_optional_string(getter("label")),
        "trust_level": str(getter("trust_level")),
        "signature_algorithm": str(getter("signature_algorithm")),
        "secret_fingerprint": normalize_optional_string(getter("secret_fingerprint")),
        "public_key": normalize_optional_string(getter("public_key")),
        "created_at": str(getter("created_at")),
        "updated_at": str(getter("updated_at")),
        "status": str(getter("status")),
        "notes": normalize_optional_string(getter("notes")),
    }


def signer_add(args: dict[str, Any]) -> dict[str, Any]:
    if store_backend() != "sqlite":
        return tool_error("signer_add requires sqlite backend")
    try:
        signer_id = _normalize_signer_id(args.get("signer_id"))
        label = normalize_optional_string(args.get("label"))
        trust_level = _normalize_signer_trust_level(args.get("trust_level"), default="trusted")
        signature_algorithm = normalize_optional_string(args.get("signature_algorithm")) or PACK_SIGNATURE_ALGORITHM_HMAC_LOCAL
        signature_algorithm = signature_algorithm.strip().lower()
        if signature_algorithm != PACK_SIGNATURE_ALGORITHM_HMAC_LOCAL:
            return tool_error_code(
                "unsupported_signature_algorithm",
                f"unsupported signature_algorithm: {signature_algorithm}",
            )
        secret_value = _validate_secret_length(args.get("secret"), field_name="secret")
        notes = normalize_optional_string(args.get("notes"))
    except ValueError as exc:
        message = str(exc)
        if "at least" in message and "secret" in message:
            return tool_error_code("secret_too_short", message)
        return tool_error(message)

    now_value = now_iso()
    secret_fingerprint = _secret_fingerprint(secret_value)
    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            existing = conn.execute(
                "SELECT 1 FROM trusted_signers WHERE signer_id = ?",
                (signer_id,),
            ).fetchone()
            if existing is not None:
                return tool_error_code("signer_already_exists", f"signer {signer_id} already exists")
            conn.execute(
                """
                INSERT INTO trusted_signers(
                    signer_id, label, trust_level, signature_algorithm, secret_fingerprint,
                    public_key, created_at, updated_at, status, notes
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signer_id,
                    label,
                    trust_level,
                    signature_algorithm,
                    secret_fingerprint,
                    None,
                    now_value,
                    now_value,
                    "active",
                    notes,
                ),
            )
    except sqlite3.IntegrityError as exc:
        return tool_error_code("signer_add_integrity_error", str(exc))
    except Exception as exc:
        return tool_error_code("signer_add_failed", f"{type(exc).__name__}: {exc}")

    structured = {
        "action": "signer_add",
        "status": "ok",
        "signer": {
            "signer_id": signer_id,
            "label": label,
            "trust_level": trust_level,
            "signature_algorithm": signature_algorithm,
            "secret_fingerprint": secret_fingerprint,
            "status": "active",
            "created_at": now_value,
        },
        "warnings": [_pack_local_hmac_warning()],
    }
    return text_result(f"Signer added: {signer_id}", structured)


def signer_list(args: dict[str, Any]) -> dict[str, Any]:
    if store_backend() != "sqlite":
        return tool_error("signer_list requires sqlite backend")
    try:
        limit = _safe_int(args.get("limit"), 100, minimum=1, maximum=500)
        status_filter = normalize_optional_string(args.get("status"))
        if status_filter is not None:
            status_filter = _normalize_signer_status(status_filter)
        trust_level_filter = normalize_optional_string(args.get("trust_level"))
        if trust_level_filter is not None:
            trust_level_filter = _normalize_signer_trust_level(trust_level_filter, default="")
    except ValueError as exc:
        return tool_error(str(exc))

    clauses: list[str] = []
    params: list[Any] = []
    if status_filter is not None:
        clauses.append("status = ?")
        params.append(status_filter)
    if trust_level_filter is not None:
        clauses.append("trust_level = ?")
        params.append(trust_level_filter)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            total = int(conn.execute(f"SELECT COUNT(*) FROM trusted_signers {where_sql}", tuple(params)).fetchone()[0])
            rows = conn.execute(
                f"""
                SELECT signer_id, label, trust_level, signature_algorithm, secret_fingerprint,
                       public_key, created_at, updated_at, status, notes
                FROM trusted_signers
                {where_sql}
                ORDER BY created_at DESC, signer_id ASC
                LIMIT ?
                """,
                tuple(params + [limit]),
            ).fetchall()
    except Exception as exc:
        return tool_error_code("signer_list_failed", f"{type(exc).__name__}: {exc}")

    signers = [_signer_row_payload(row) for row in rows]
    structured = {
        "action": "signer_list",
        "status": "ok",
        "total": total,
        "limited": bool(total > limit),
        "signers": signers,
    }
    return text_result(f"Signers listed: {len(signers)} of {total}", structured)


def _signer_set_status(args: dict[str, Any], *, action_name: str, next_status: str) -> dict[str, Any]:
    if store_backend() != "sqlite":
        return tool_error(f"{action_name} requires sqlite backend")
    try:
        signer_id = _normalize_signer_id(args.get("signer_id"))
    except ValueError as exc:
        return tool_error(str(exc))
    now_value = now_iso()
    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            updated = conn.execute(
                "UPDATE trusted_signers SET status = ?, updated_at = ? WHERE signer_id = ?",
                (next_status, now_value, signer_id),
            )
            if int(updated.rowcount) <= 0:
                return tool_error_code("signer_not_found", f"signer {signer_id} was not found")
    except Exception as exc:
        return tool_error_code(f"{action_name}_failed", f"{type(exc).__name__}: {exc}")
    structured = {
        "action": action_name,
        "status": "ok",
        "signer_id": signer_id,
        "signer_status": next_status,
    }
    return text_result(f"Signer {signer_id} status set to {next_status}.", structured)


def signer_disable(args: dict[str, Any]) -> dict[str, Any]:
    return _signer_set_status(args, action_name="signer_disable", next_status="disabled")


def signer_enable(args: dict[str, Any]) -> dict[str, Any]:
    return _signer_set_status(args, action_name="signer_enable", next_status="active")


def _trusted_signer_lookup_readonly(signer_id: str) -> dict[str, Any] | None:
    if store_backend() != "sqlite":
        return None
    db_path = sqlite_path()
    if not db_path.exists():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT signer_id, trust_level, status, signature_algorithm, secret_fingerprint
            FROM trusted_signers
            WHERE signer_id = ?
            """,
            (signer_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "signer_id": str(row["signer_id"]),
            "trust_level": str(row["trust_level"]),
            "status": str(row["status"]),
            "signature_algorithm": str(row["signature_algorithm"]),
            "secret_fingerprint": normalize_optional_string(row["secret_fingerprint"]),
        }
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _pack_json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _pack_jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    lines = [json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for row in rows]
    if not lines:
        return b""
    return ("\n".join(lines) + "\n").encode("utf-8")


def _pack_content_hash(
    member_bytes: dict[str, bytes],
    covered_members: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    if covered_members is None:
        resolved_members = sorted(PACK_CONTENT_HASH_COVERED_MEMBERS)
    else:
        resolved_members = sorted(str(name) for name in covered_members)
    member_hashes: dict[str, str] = {}
    for member_name in resolved_members:
        if member_name not in member_bytes:
            raise ValueError(f"missing content hash member: {member_name}")
        member_hashes[member_name] = hashlib.sha256(member_bytes[member_name]).hexdigest()
    canonical = "".join(f"{member_name}\t{member_hashes[member_name]}\n" for member_name in resolved_members)
    value = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "algorithm": "sha256",
        "value": value,
        "covered_members": resolved_members,
    }


def _pack_signing_payload_v1(manifest: dict[str, Any]) -> bytes:
    signature = manifest.get("signature", {})
    if not isinstance(signature, dict):
        raise ValueError("manifest.signature must be an object")
    content_hash = manifest.get("content_hash", {})
    if not isinstance(content_hash, dict):
        raise ValueError("manifest.content_hash must be an object")
    payload_obj = {
        "signature_payload_version": PACK_SIGNATURE_PAYLOAD_VERSION_V1,
        "pack_id": str(manifest.get("pack_id", "")),
        "pack_schema_version": int(manifest.get("pack_schema_version", 0)),
        "content_hash": str(content_hash.get("value", "")),
        "redaction_ruleset_version": str(manifest.get("redaction_ruleset_version", "")),
        "signer_id": str(signature.get("signer_id", "")),
        "signature_algorithm": str(signature.get("signature_algorithm", "")),
        "secret_fingerprint": str(signature.get("secret_fingerprint", "")),
    }
    return json.dumps(payload_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _pack_sign_hmac_v1(manifest: dict[str, Any], secret: str) -> str:
    payload = _pack_signing_payload_v1(manifest)
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _pack_local_hmac_warning() -> dict[str, str]:
    return {"code": "local_hmac_not_public_key", "message": PACK_LOCAL_HMAC_WARNING_MESSAGE}


def _pack_validate_zip(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path, "r") as archive:
        members = set(archive.namelist())
        for required_member in PACK_REQUIRED_MEMBERS:
            if required_member not in members:
                raise ValueError(f"missing zip member: {required_member}")

        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"invalid manifest.json: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ValueError("manifest.json must be an object")

        content_hash = manifest.get("content_hash", {})
        if not isinstance(content_hash, dict):
            raise ValueError("manifest.content_hash must be an object")
        covered_members = content_hash.get("covered_members")
        if not isinstance(covered_members, list) or not all(isinstance(item, str) for item in covered_members):
            raise ValueError("manifest.content_hash.covered_members must be a non-empty list")
        if sorted(str(item) for item in covered_members) != sorted(PACK_CONTENT_HASH_COVERED_MEMBERS):
            raise ValueError("manifest.content_hash.covered_members must match canonical covered members")
        if str(content_hash.get("algorithm", "")) != "sha256":
            raise ValueError("manifest.content_hash.algorithm must be sha256")

        member_bytes = {name: archive.read(name) for name in PACK_CONTENT_HASH_COVERED_MEMBERS}
        recomputed = _pack_content_hash(member_bytes, covered_members=list(PACK_CONTENT_HASH_COVERED_MEMBERS))
        if str(content_hash.get("value", "")) != str(recomputed.get("value", "")):
            raise ValueError("manifest content hash mismatch")

        signed = manifest.get("signed")
        if not isinstance(signed, bool):
            raise ValueError("manifest.signed must be boolean")
        if signed:
            signature_block = manifest.get("signature", {})
            if not isinstance(signature_block, dict):
                raise ValueError("manifest.signature must be an object for signed packs")
            signature_member = str(signature_block.get("signature_member", "") or "")
            if signature_member != PACK_SIGNATURE_MEMBER:
                raise ValueError(f"manifest.signature.signature_member must be {PACK_SIGNATURE_MEMBER}")
            if signature_member not in members:
                raise ValueError("missing signature/signature.json member")
            try:
                signature_payload = json.loads(archive.read(signature_member).decode("utf-8"))
            except Exception as exc:
                raise ValueError(f"invalid signature member {signature_member}: {exc}") from exc
            if not isinstance(signature_payload, dict):
                raise ValueError("signature/signature.json must be an object")
            required_signature_fields = (
                "signature_schema_version",
                "signature_algorithm",
                "signature_payload_version",
                "signer_id",
                "secret_fingerprint",
                "signed_at",
                "signature_value",
            )
            missing_signature_fields = [field for field in required_signature_fields if field not in signature_payload]
            if missing_signature_fields:
                raise ValueError(f"signature/signature.json missing fields: {missing_signature_fields}")
        else:
            unsigned_reason = str(manifest.get("unsigned_reason", "") or "")
            if unsigned_reason not in {
                PACK_UNSIGNED_REASON_SIGNING_NOT_IMPLEMENTED,
                PACK_UNSIGNED_REASON_OPERATOR,
            }:
                raise ValueError("manifest.unsigned_reason is invalid for unsigned pack")

        rows_blob = archive.read("content/memories.jsonl").decode("utf-8")
        exported_rows = 0
        for line in rows_blob.splitlines():
            line = line.strip()
            if not line:
                continue
            parsed = json.loads(line)
            if not isinstance(parsed, dict):
                raise ValueError("content/memories.jsonl rows must be objects")
            exported_rows += 1

        selection_payload = manifest.get("selection", {})
        if not isinstance(selection_payload, dict):
            raise ValueError("manifest.selection must be an object")
        manifest_exported_rows = int(selection_payload.get("exported_rows", 0))
        if exported_rows != manifest_exported_rows:
            raise ValueError("manifest selection.exported_rows mismatch")
        return manifest


class PackSnapshotError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)


class PackSelectorError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)


def _load_pack_snapshot(pack_path: Path) -> dict[str, Any]:
    # Snapshot invariant:
    # Validation and import must operate on the exact same bytes. The caller
    # must not re-read pack_path after this snapshot is created.
    try:
        zip_bytes = pack_path.read_bytes()
    except Exception as exc:
        raise PackSnapshotError("pack_read_failed", f"{type(exc).__name__}: {exc}") from exc
    received_zip_sha256 = hashlib.sha256(zip_bytes).hexdigest()

    normalized_infos: dict[str, zipfile.ZipInfo] = {}
    total_uncompressed = 0
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as archive:
            infos = archive.infolist()
            if len(infos) > PACK_INSPECT_MAX_MEMBERS:
                raise PackSnapshotError(
                    "zip_member_count_limit",
                    f"ZIP has {len(infos)} members; max allowed is {PACK_INSPECT_MAX_MEMBERS}.",
                )
            for info in infos:
                if int(info.flag_bits) & 0x1:
                    raise PackSnapshotError("encrypted_member", f"ZIP member is encrypted: {info.filename}")
                member_size = int(info.file_size)
                if member_size > PACK_INSPECT_MAX_MEMBER_SIZE:
                    raise PackSnapshotError(
                        "member_size_limit",
                        f"ZIP member exceeds max size ({PACK_INSPECT_MAX_MEMBER_SIZE} bytes): {info.filename}",
                    )
                total_uncompressed += member_size
                if total_uncompressed > PACK_INSPECT_MAX_TOTAL_SIZE:
                    raise PackSnapshotError(
                        "zip_total_size_limit",
                        f"ZIP uncompressed total exceeds max size ({PACK_INSPECT_MAX_TOTAL_SIZE} bytes).",
                    )
                try:
                    normalized_name, is_dir = _pack_inspect_validate_member_name(str(info.filename))
                except ValueError as exc:
                    raise PackSnapshotError("unsafe_member_name", f"{info.filename}: {exc}") from exc
                if is_dir:
                    continue
                if normalized_name in normalized_infos:
                    raise PackSnapshotError(
                        "duplicate_member",
                        f"duplicate ZIP member after normalization: {normalized_name}",
                    )
                normalized_infos[normalized_name] = info
            required_set = set(PACK_INSPECT_REQUIRED_MEMBERS)
            present_set = set(normalized_infos)
            missing = sorted(required_set - present_set)
            if missing:
                raise PackSnapshotError("missing_required_member", f"missing required members: {missing}")

            member_bytes = {
                member_name: archive.read(normalized_infos[member_name])
                for member_name in sorted(normalized_infos)
            }
            required_member_bytes = {
                member_name: member_bytes[member_name]
                for member_name in PACK_INSPECT_REQUIRED_MEMBERS
            }
    except zipfile.BadZipFile as exc:
        raise PackSnapshotError("bad_zip", f"Bad ZIP file: {exc}") from exc
    except zipfile.LargeZipFile as exc:
        raise PackSnapshotError("zip64_not_supported", f"ZIP too large for configured limits: {exc}") from exc

    return {
        "pack_path": pack_path,
        "zip_bytes": zip_bytes,
        "received_zip_sha256": received_zip_sha256,
        "safe_zip_members": True,
        "present_members": sorted(normalized_infos),
        "member_bytes": member_bytes,
        "required_member_bytes": required_member_bytes,
    }


def _pack_inspect_default() -> dict[str, Any]:
    return {
        "action": "pack_inspect",
        "status": "invalid",
        "pack": {
            "pack_id": "",
            "pack_name": "",
            "pack_schema_version": None,
            "created_at": "",
            "mnemo_version": "",
            "signed": False,
            "unsigned_reason": "",
        },
        "signature": {
            "present": False,
            "verified": False,
            "signature_algorithm": None,
            "signer_id": None,
            "signer_status": None,
            "trust_level": None,
            "trust_classification": "unsigned",
            "secret_fingerprint": None,
        },
        "content_hash": {
            "algorithm": "sha256",
            "manifest_value": "",
            "recomputed_value": "",
            "valid": False,
            "covered_members": [],
            "canonical_covered_members": list(PACK_CONTENT_HASH_COVERED_MEMBERS),
        },
        "counts": {
            "rows": 0,
            "by_kind": {},
            "by_namespace": {},
            "by_origin": {},
            "topics": 0,
            "referenced_files": 0,
            "redaction_total_matches": 0,
            "redaction_affected_rows": 0,
        },
        "validation": {
            "required_members_present": False,
            "json_members_parse": False,
            "jsonl_rows_parse": False,
            "row_count_matches_manifest": False,
            "no_source_memory_ids": False,
            "redaction_metadata_valid": False,
            "content_hash_valid": False,
            "covered_members_valid": False,
            "safe_zip_members": False,
            "supported_schema": True,
            "supported_signature_state": True,
            "signature_valid": True,
        },
        "trusted_import_available": False,
        "import_recommendation": "reject",
        "warnings": [],
        "errors": [],
        "samples": [],
    }


def _pack_inspect_warning(payload: dict[str, Any], code: str, message: str) -> None:
    warnings = payload.get("warnings", [])
    if not isinstance(warnings, list):
        warnings = []
        payload["warnings"] = warnings
    key = f"{code}:{message}"
    existing = {
        f"{str(item.get('code', ''))}:{str(item.get('message', ''))}"
        for item in warnings
        if isinstance(item, dict)
    }
    if key in existing:
        return
    warnings.append({"code": str(code), "message": str(message)})


def _pack_inspect_add_suffix_warning(
    payload: dict[str, Any], *, legacy_zip_suffix_warning: bool, nonstandard_suffix_warning: bool
) -> None:
    if legacy_zip_suffix_warning:
        _pack_inspect_warning(
            payload,
            "legacy_zip_suffix",
            "Pack path ends with .zip; this legacy suffix remains supported for compatibility, but .mem is preferred.",
        )
    elif nonstandard_suffix_warning:
        _pack_inspect_warning(
            payload,
            "nonstandard_pack_suffix",
            "Pack path does not end with .mem or .zip but will be inspected because it opened as a valid ZIP.",
        )


def _pack_inspect_error(payload: dict[str, Any], code: str, message: str) -> None:
    errors = payload.get("errors", [])
    if not isinstance(errors, list):
        errors = []
        payload["errors"] = errors
    errors.append({"code": str(code), "message": str(message)})


def _pack_inspect_collapse(text: Any, max_chars: int = 200) -> str:
    return collapsed_preview_text(text, max_chars=max_chars)


def _pack_inspect_validate_member_name(raw_name: str) -> tuple[str, bool]:
    if "\x00" in raw_name:
        raise ValueError("member name contains NUL byte")
    if any(ord(ch) < 0x20 for ch in raw_name):
        raise ValueError("member name contains control characters")
    normalized = str(raw_name).replace("\\", "/")
    if not normalized:
        raise ValueError("member name is empty")
    is_dir = normalized.endswith("/")
    normalized = normalized.rstrip("/") if is_dir else normalized
    if not normalized:
        raise ValueError("member name is empty")
    if normalized.startswith("/"):
        raise ValueError("member path is absolute")
    if ":" in normalized:
        raise ValueError("member path contains ':'")
    segments = normalized.split("/")
    if not segments:
        raise ValueError("member path is empty")
    for segment in segments:
        if segment in {"", ".", ".."}:
            raise ValueError("member path contains invalid traversal segment")
    return normalized, is_dir


def _pack_inspect_rows_from_jsonl(blob: bytes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    lines = blob.decode("utf-8").splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parsed = json.loads(line)
        if not isinstance(parsed, dict):
            raise ValueError("JSONL row is not an object")
        rows.append(parsed)
    return rows


def _pack_inspect_parse_json(blob: bytes, field: str) -> dict[str, Any]:
    parsed = json.loads(blob.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{field} must be a JSON object")
    return parsed


def _pack_inspect_sample_rows(rows: list[dict[str, Any]], sample_limit: int) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for row in rows[:sample_limit]:
        row_id = str(row.get("row_id_in_pack", ""))
        kind_name = str(row.get("kind", ""))
        topics_raw = row.get("topics", [])
        topics = [str(item) for item in topics_raw] if isinstance(topics_raw, list) else []
        text_fields = row.get("text_fields", {})
        preview_source = ""
        if isinstance(text_fields, dict):
            preferred_keys = ["text", "title"] + sorted(
                key
                for key in text_fields
                if str(key) not in {"text", "title"}
            )
            for key in preferred_keys:
                if key not in text_fields:
                    continue
                value = text_fields.get(key)
                if value is None:
                    continue
                preview_source = str(value)
                if preview_source:
                    break
        samples.append(
            {
                "row_id_in_pack": row_id,
                "kind": kind_name,
                "topics": topics,
                "text_preview": _pack_inspect_collapse(preview_source, max_chars=200),
            }
        )
    return samples


def _pack_inspect_finalize(payload: dict[str, Any], *, include_samples: bool, sample_limit: int) -> dict[str, Any]:
    validation = payload.get("validation", {})
    if not isinstance(validation, dict):
        validation = {}
        payload["validation"] = validation

    signature_payload = payload.get("signature", {})
    if not isinstance(signature_payload, dict):
        signature_payload = {}
    trusted_import_available = bool(
        signature_payload.get("present") is True
        and signature_payload.get("verified") is True
        and str(signature_payload.get("trust_classification", "")) == "trusted_signer"
        and str(signature_payload.get("signer_status", "")) == "active"
        and str(signature_payload.get("trust_level", "")) == "trusted"
    )
    payload["trusted_import_available"] = bool(trusted_import_available)

    supported_schema = bool(validation.get("supported_schema", False))
    supported_signature_state = bool(validation.get("supported_signature_state", False))
    if not supported_schema or not supported_signature_state:
        payload["status"] = "unsupported"
        payload["import_recommendation"] = "reject"
        payload["samples"] = []
        return payload

    validation_keys = [
        "required_members_present",
        "json_members_parse",
        "jsonl_rows_parse",
        "row_count_matches_manifest",
        "no_source_memory_ids",
        "redaction_metadata_valid",
        "content_hash_valid",
        "covered_members_valid",
        "safe_zip_members",
        "supported_schema",
        "supported_signature_state",
        "signature_valid",
    ]
    all_valid = all(bool(validation.get(key, False)) for key in validation_keys)
    payload["status"] = "valid" if all_valid else "invalid"

    pack_payload = payload.get("pack", {})
    signed = bool(pack_payload.get("signed", False)) if isinstance(pack_payload, dict) else False
    payload["import_recommendation"] = "quarantine_only" if payload["status"] == "valid" else "reject"

    if payload["status"] != "valid" or not include_samples:
        payload["samples"] = []
    else:
        rows = payload.get("_rows_for_samples", [])
        payload["samples"] = _pack_inspect_sample_rows(rows if isinstance(rows, list) else [], sample_limit)

    signature_verified = bool(signature_payload.get("verified", False)) if isinstance(signature_payload, dict) else False
    if isinstance(pack_payload, dict) and (not signed):
        if str(pack_payload.get("unsigned_reason", "") or "") in {
            PACK_UNSIGNED_REASON_SIGNING_NOT_IMPLEMENTED,
            PACK_UNSIGNED_REASON_OPERATOR,
        }:
            _pack_inspect_warning(
                payload,
                "unsigned_pack",
                "This pack is unsigned and can only be imported into quarantine in a later import phase.",
            )
    if payload["status"] == "valid" and signed and not signature_verified:
        _pack_inspect_warning(
            payload,
            "signature_not_verified",
            "Pack signature was not verified because no verification_secret was provided.",
        )
    if payload["status"] != "valid":
        payload["trusted_import_available"] = False
    payload.pop("_rows_for_samples", None)
    return payload


def _pack_inspect_text(payload: dict[str, Any], fallback_name: str) -> str:
    pack_payload = payload.get("pack", {})
    pack_id = ""
    pack_name = ""
    if isinstance(pack_payload, dict):
        pack_id = str(pack_payload.get("pack_id", "") or "")
        pack_name = str(pack_payload.get("pack_name", "") or "")
    status_value = str(payload.get("status", "invalid"))
    hash_valid = bool((payload.get("content_hash", {}) if isinstance(payload.get("content_hash"), dict) else {}).get("valid", False))
    recommendation = str(payload.get("import_recommendation", "reject"))
    rows_count = int((payload.get("counts", {}) if isinstance(payload.get("counts"), dict) else {}).get("rows", 0))
    redaction_total = int(
        (payload.get("counts", {}) if isinstance(payload.get("counts"), dict) else {}).get("redaction_total_matches", 0)
    )
    signature_payload = payload.get("signature", {}) if isinstance(payload.get("signature"), dict) else {}
    trust_classification = str(signature_payload.get("trust_classification", "unsigned"))
    warning_text = "; ".join(
        f"{item.get('code')}: {item.get('message')}"
        for item in payload.get("warnings", [])
        if isinstance(item, dict)
    ) or "none"
    error_text = "; ".join(
        f"{item.get('code')}: {item.get('message')}"
        for item in payload.get("errors", [])
        if isinstance(item, dict)
    ) or "none"
    lines = [
        f"Pack inspect: {pack_id or fallback_name} ({pack_name or 'unknown'})",
        f"Status: {status_value}",
        f"Content hash valid: {str(hash_valid).lower()}",
        f"Rows: {rows_count}",
        f"Redaction total matches: {redaction_total}",
        f"Signature classification: {trust_classification}",
        f"Recommendation: {recommendation}",
        f"Warnings: {warning_text}",
        f"Errors: {error_text}",
    ]
    return "\n".join(lines)


def _inspect_pack_snapshot(
    snapshot: dict[str, Any],
    *,
    include_samples: bool,
    sample_limit: int,
    verification_secret: str | None = None,
    legacy_zip_suffix_warning: bool = False,
    nonstandard_suffix_warning: bool = False,
) -> dict[str, Any]:
    payload = _pack_inspect_default()
    _pack_inspect_add_suffix_warning(
        payload,
        legacy_zip_suffix_warning=legacy_zip_suffix_warning,
        nonstandard_suffix_warning=nonstandard_suffix_warning,
    )

    raw_member_bytes = snapshot.get("required_member_bytes", {})
    if not isinstance(raw_member_bytes, dict):
        raw_member_bytes = {}
    all_member_bytes = snapshot.get("member_bytes", {})
    if not isinstance(all_member_bytes, dict):
        all_member_bytes = dict(raw_member_bytes)
    present_members_raw = snapshot.get("present_members", [])
    present_set = {str(item) for item in present_members_raw} if isinstance(present_members_raw, list) else set()
    payload["validation"]["safe_zip_members"] = bool(snapshot.get("safe_zip_members", False))

    required_set = set(PACK_INSPECT_REQUIRED_MEMBERS)
    missing = sorted(required_set - present_set)
    if missing:
        payload["validation"]["required_members_present"] = False
        _pack_inspect_error(payload, "missing_required_member", f"missing required members: {missing}")
        return _pack_inspect_finalize(payload, include_samples=include_samples, sample_limit=sample_limit)
    payload["validation"]["required_members_present"] = True

    for member_name in sorted(present_set - required_set):
        if member_name in PACK_INSPECT_KNOWN_EXTRA_MEMBERS:
            continue
        _pack_inspect_warning(payload, "unknown_extra_member", f"Unknown extra member: {member_name}")

    try:
        manifest = _pack_inspect_parse_json(raw_member_bytes["manifest.json"], "manifest.json")
        payload["validation"]["json_members_parse"] = True
    except Exception as exc:
        _pack_inspect_error(payload, "manifest_parse_error", f"manifest.json parse failed: {exc}")
        payload["validation"]["json_members_parse"] = False
        return _pack_inspect_finalize(payload, include_samples=include_samples, sample_limit=sample_limit)

    pack_payload = payload["pack"]
    pack_payload["pack_id"] = str(manifest.get("pack_id", "") or "")
    pack_payload["pack_name"] = str(manifest.get("pack_name", "") or "")
    pack_payload["pack_schema_version"] = manifest.get("pack_schema_version")
    pack_payload["created_at"] = str(manifest.get("created_at", "") or "")
    pack_payload["mnemo_version"] = str(manifest.get("mnemo_version", "") or "")
    pack_payload["signed"] = bool(manifest.get("signed", False))
    pack_payload["unsigned_reason"] = str(manifest.get("unsigned_reason", "") or "")

    manifest_required_fields = [
        "pack_schema_version",
        "pack_id",
        "pack_name",
        "created_at",
        "mnemo_version",
        "signed",
        "content_hash",
        "redaction_ruleset_version",
        "redaction_rules_applied",
        "selection",
        "counts",
        "files",
        "redaction",
    ]
    missing_manifest_fields = [name for name in manifest_required_fields if name not in manifest]
    if bool(manifest.get("signed", False)) is False and "unsigned_reason" not in manifest:
        missing_manifest_fields.append("unsigned_reason")
    if bool(manifest.get("signed", False)) is True and "signature" not in manifest:
        missing_manifest_fields.append("signature")
    if missing_manifest_fields:
        _pack_inspect_error(payload, "manifest_missing_field", f"manifest missing required fields: {missing_manifest_fields}")
        return _pack_inspect_finalize(payload, include_samples=include_samples, sample_limit=sample_limit)

    schema_version = manifest.get("pack_schema_version")
    try:
        schema_version_int = int(schema_version)
    except Exception:
        schema_version_int = -1
    if schema_version_int != 1:
        payload["validation"]["supported_schema"] = False
        _pack_inspect_error(payload, "unsupported_schema_version", f"Unsupported pack_schema_version: {schema_version}")
        return _pack_inspect_finalize(payload, include_samples=include_samples, sample_limit=sample_limit)

    pack_id = str(manifest.get("pack_id", "") or "")
    if not PACK_INSPECT_PACK_ID_RE.match(pack_id):
        _pack_inspect_error(payload, "invalid_pack_id", f"Invalid pack_id format: {pack_id}")
        payload["validation"]["json_members_parse"] = False
    created_at = str(manifest.get("created_at", "") or "")
    if not PACK_INSPECT_UTC_TS_RE.match(created_at):
        _pack_inspect_error(payload, "invalid_manifest_created_at", "manifest.created_at is not ISO-8601 UTC")
        payload["validation"]["json_members_parse"] = False

    if str(manifest.get("mnemo_version", "") or "") != SERVER_VERSION:
        _pack_inspect_warning(
            payload,
            "mnemo_version_differs",
            f"Pack mnemo_version {manifest.get('mnemo_version')} differs from inspector version {SERVER_VERSION}.",
        )

    signature_info = payload.get("signature", {})
    if not isinstance(signature_info, dict):
        signature_info = {}
        payload["signature"] = signature_info

    signed_value = manifest.get("signed", False)
    if not isinstance(signed_value, bool):
        _pack_inspect_error(payload, "invalid_signed_field", "manifest.signed must be boolean")
        payload["validation"]["supported_signature_state"] = False
        signature_info["trust_classification"] = "unsupported_signature"
        return _pack_inspect_finalize(payload, include_samples=include_samples, sample_limit=sample_limit)

    if not bool(signed_value):
        signature_info["present"] = False
        signature_info["verified"] = False
        signature_info["signature_algorithm"] = None
        signature_info["signer_id"] = None
        signature_info["signer_status"] = None
        signature_info["trust_level"] = None
        signature_info["trust_classification"] = "unsigned"
        signature_info["secret_fingerprint"] = None
        if verification_secret is not None:
            _pack_inspect_warning(
                payload,
                "verification_secret_unused_for_unsigned_pack",
                "verification_secret was provided but this pack is unsigned.",
            )
        unsigned_reason = str(manifest.get("unsigned_reason", "") or "")
        if unsigned_reason not in {PACK_UNSIGNED_REASON_SIGNING_NOT_IMPLEMENTED, PACK_UNSIGNED_REASON_OPERATOR}:
            _pack_inspect_error(
                payload,
                "invalid_unsigned_reason",
                "manifest.unsigned_reason must be signing_not_implemented or operator_chose_unsigned",
            )
            payload["validation"]["json_members_parse"] = False
            payload["validation"]["signature_valid"] = False
    else:
        signature_info["present"] = True
        manifest_signature = manifest.get("signature", {})
        if not isinstance(manifest_signature, dict):
            _pack_inspect_error(payload, "invalid_signature", "manifest.signature must be an object for signed packs")
            signature_info["trust_classification"] = "invalid_signature"
            payload["validation"]["signature_valid"] = False
        else:
            signature_member = str(manifest_signature.get("signature_member", "") or "")
            signature_algorithm = str(manifest_signature.get("signature_algorithm", "") or "")
            signature_payload_version = str(manifest_signature.get("signature_payload_version", "") or "")
            signer_id = str(manifest_signature.get("signer_id", "") or "")
            secret_fingerprint = str(manifest_signature.get("secret_fingerprint", "") or "")
            signature_info["signature_algorithm"] = signature_algorithm or None
            signature_info["signer_id"] = signer_id or None
            signature_info["secret_fingerprint"] = secret_fingerprint or None
            signature_info["trust_classification"] = "signature_not_verified"
            if signature_algorithm == PACK_SIGNATURE_ALGORITHM_HMAC_LOCAL:
                _pack_inspect_warning(payload, "local_hmac_not_public_key", PACK_LOCAL_HMAC_WARNING_MESSAGE)

            if signature_member != PACK_SIGNATURE_MEMBER:
                payload["validation"]["supported_signature_state"] = False
                signature_info["trust_classification"] = "unsupported_signature"
                _pack_inspect_error(
                    payload,
                    "unsupported_signature",
                    f"manifest.signature.signature_member must be {PACK_SIGNATURE_MEMBER}",
                )
                return _pack_inspect_finalize(payload, include_samples=include_samples, sample_limit=sample_limit)
            if signature_algorithm != PACK_SIGNATURE_ALGORITHM_HMAC_LOCAL:
                payload["validation"]["supported_signature_state"] = False
                signature_info["trust_classification"] = "unsupported_signature"
                _pack_inspect_error(
                    payload,
                    "unsupported_signature",
                    f"unsupported signature algorithm: {signature_algorithm}",
                )
                return _pack_inspect_finalize(payload, include_samples=include_samples, sample_limit=sample_limit)
            if signature_payload_version != PACK_SIGNATURE_PAYLOAD_VERSION_V1:
                payload["validation"]["supported_signature_state"] = False
                signature_info["trust_classification"] = "unsupported_signature"
                _pack_inspect_error(
                    payload,
                    "unsupported_signature",
                    f"unsupported signature payload version: {signature_payload_version}",
                )
                return _pack_inspect_finalize(payload, include_samples=include_samples, sample_limit=sample_limit)
            if not PACK_SIGNER_ID_RE.match(signer_id):
                _pack_inspect_error(payload, "invalid_signature", f"invalid signer_id format: {signer_id}")
                payload["validation"]["signature_valid"] = False
                signature_info["trust_classification"] = "invalid_signature"

            signature_blob = all_member_bytes.get(PACK_SIGNATURE_MEMBER)
            if not isinstance(signature_blob, (bytes, bytearray)):
                _pack_inspect_error(payload, "invalid_signature", "missing signature/signature.json member")
                payload["validation"]["signature_valid"] = False
                signature_info["trust_classification"] = "invalid_signature"
            else:
                try:
                    signature_payload = _pack_inspect_parse_json(bytes(signature_blob), PACK_SIGNATURE_MEMBER)
                except Exception as exc:
                    _pack_inspect_error(payload, "invalid_signature", f"signature member parse failed: {exc}")
                    payload["validation"]["signature_valid"] = False
                    signature_info["trust_classification"] = "invalid_signature"
                    signature_payload = {}

                if isinstance(signature_payload, dict):
                    required_signature_fields = (
                        "signature_schema_version",
                        "signature_algorithm",
                        "signature_payload_version",
                        "signer_id",
                        "secret_fingerprint",
                        "signed_at",
                        "signature_value",
                    )
                    missing_signature_fields = [field for field in required_signature_fields if field not in signature_payload]
                    if missing_signature_fields:
                        _pack_inspect_error(
                            payload,
                            "invalid_signature",
                            f"signature member missing required fields: {missing_signature_fields}",
                        )
                        payload["validation"]["signature_valid"] = False
                        signature_info["trust_classification"] = "invalid_signature"

                    signed_at = str(signature_payload.get("signed_at", "") or "")
                    if signed_at and not PACK_INSPECT_UTC_TS_RE.match(signed_at):
                        _pack_inspect_error(payload, "invalid_signature", "signature/signature.json signed_at must be ISO-8601 UTC")
                        payload["validation"]["signature_valid"] = False
                        signature_info["trust_classification"] = "invalid_signature"

                    if int(signature_payload.get("signature_schema_version", 0)) != PACK_SIGNATURE_SCHEMA_VERSION:
                        _pack_inspect_error(payload, "invalid_signature", "unsupported signature schema version")
                        payload["validation"]["signature_valid"] = False
                        signature_info["trust_classification"] = "invalid_signature"

                    signature_value = str(signature_payload.get("signature_value", "") or "")
                    if not signature_value:
                        _pack_inspect_error(payload, "invalid_signature", "signature/signature.json signature_value is required")
                        payload["validation"]["signature_valid"] = False
                        signature_info["trust_classification"] = "invalid_signature"

                    manifest_match = (
                        str(signature_payload.get("signer_id", "") or "") == signer_id
                        and str(signature_payload.get("signature_algorithm", "") or "") == signature_algorithm
                        and str(signature_payload.get("signature_payload_version", "") or "") == signature_payload_version
                        and str(signature_payload.get("secret_fingerprint", "") or "") == secret_fingerprint
                    )
                    if not manifest_match:
                        _pack_inspect_error(
                            payload,
                            "invalid_signature",
                            "manifest signature metadata does not match signature/signature.json",
                        )
                        payload["validation"]["signature_valid"] = False
                        signature_info["trust_classification"] = "invalid_signature"
                    elif payload["validation"]["signature_valid"]:
                        signer_row = _trusted_signer_lookup_readonly(signer_id)
                        if verification_secret is None:
                            signature_info["verified"] = False
                            if signer_row is None:
                                signature_info["trust_classification"] = "unknown_signer"
                            else:
                                signer_status = str(signer_row.get("status", "") or "")
                                signer_trust_level = str(signer_row.get("trust_level", "") or "")
                                signature_info["signer_status"] = signer_status or None
                                signature_info["trust_level"] = signer_trust_level or None
                                if signer_status == "disabled":
                                    _pack_inspect_error(payload, "disabled_signer", f"signer {signer_id} is disabled")
                                    payload["validation"]["signature_valid"] = False
                                    signature_info["trust_classification"] = "disabled_signer"
                                elif signer_trust_level == "blocked":
                                    _pack_inspect_error(payload, "blocked_signer", f"signer {signer_id} is blocked")
                                    payload["validation"]["signature_valid"] = False
                                    signature_info["trust_classification"] = "blocked_signer"
                                else:
                                    signature_info["trust_classification"] = "signature_not_verified"
                        else:
                            expected_value = _pack_sign_hmac_v1(manifest, verification_secret)
                            if not hmac.compare_digest(expected_value, signature_value):
                                _pack_inspect_error(payload, "invalid_signature", "signature verification failed")
                                payload["validation"]["signature_valid"] = False
                                signature_info["trust_classification"] = "invalid_signature"
                            else:
                                signature_info["verified"] = True
                                if signer_row is None:
                                    signature_info["trust_classification"] = "unknown_signer"
                                else:
                                    signer_status = str(signer_row.get("status", "") or "")
                                    signer_trust_level = str(signer_row.get("trust_level", "") or "")
                                    signer_registry_fingerprint = normalize_optional_string(
                                        signer_row.get("secret_fingerprint")
                                    )
                                    signature_info["signer_status"] = signer_status or None
                                    signature_info["trust_level"] = signer_trust_level or None
                                    if signer_status == "disabled":
                                        _pack_inspect_error(payload, "disabled_signer", f"signer {signer_id} is disabled")
                                        payload["validation"]["signature_valid"] = False
                                        signature_info["trust_classification"] = "disabled_signer"
                                    elif signer_trust_level == "blocked":
                                        _pack_inspect_error(payload, "blocked_signer", f"signer {signer_id} is blocked")
                                        payload["validation"]["signature_valid"] = False
                                        signature_info["trust_classification"] = "blocked_signer"
                                    elif signer_registry_fingerprint != secret_fingerprint:
                                        _pack_inspect_error(
                                            payload,
                                            "secret_fingerprint_mismatch",
                                            f"signer {signer_id} secret_fingerprint does not match registry",
                                        )
                                        payload["validation"]["signature_valid"] = False
                                        signature_info["trust_classification"] = "secret_fingerprint_mismatch"
                                    else:
                                        signature_info["trust_classification"] = "trusted_signer"

    content_hash_payload = manifest.get("content_hash", {})
    if not isinstance(content_hash_payload, dict):
        _pack_inspect_error(payload, "content_hash_format", "manifest.content_hash must be an object")
        payload["validation"]["content_hash_valid"] = False
        payload["validation"]["covered_members_valid"] = False
        payload["validation"]["json_members_parse"] = False
    else:
        payload["content_hash"]["algorithm"] = str(content_hash_payload.get("algorithm", "") or "")
        payload["content_hash"]["manifest_value"] = str(content_hash_payload.get("value", "") or "")
        covered_members = content_hash_payload.get("covered_members", [])
        if not isinstance(covered_members, list) or not all(isinstance(item, str) for item in covered_members):
            _pack_inspect_error(payload, "content_hash_covered_members_format", "manifest.content_hash.covered_members must be a string list")
            payload["validation"]["covered_members_valid"] = False
            payload["validation"]["content_hash_valid"] = False
        else:
            payload["content_hash"]["covered_members"] = list(covered_members)
            canonical_set = sorted(PACK_CONTENT_HASH_COVERED_MEMBERS)
            observed_set = sorted(str(item) for item in covered_members)
            covered_valid = observed_set == canonical_set
            payload["validation"]["covered_members_valid"] = bool(covered_valid)
            if not covered_valid:
                _pack_inspect_error(
                    payload,
                    "covered_members_mismatch",
                    f"manifest covered_members mismatch: expected {canonical_set}, got {observed_set}",
                )

            try:
                canonical_bytes = {name: raw_member_bytes[name] for name in PACK_CONTENT_HASH_COVERED_MEMBERS}
                recomputed_hash_payload = _pack_content_hash(canonical_bytes, covered_members=list(PACK_CONTENT_HASH_COVERED_MEMBERS))
                recomputed_value = str(recomputed_hash_payload.get("value", ""))
                payload["content_hash"]["recomputed_value"] = recomputed_value
                algorithm_valid = str(content_hash_payload.get("algorithm", "")) == "sha256"
                if not algorithm_valid:
                    _pack_inspect_error(payload, "content_hash_algorithm", "manifest.content_hash.algorithm must be sha256")
                value_valid = str(content_hash_payload.get("value", "")) == recomputed_value
                if not value_valid:
                    _pack_inspect_error(payload, "content_hash_mismatch", "manifest content hash does not match recomputed canonical content hash")
                payload["validation"]["content_hash_valid"] = bool(covered_valid and algorithm_valid and value_valid)
                payload["content_hash"]["valid"] = bool(payload["validation"]["content_hash_valid"])
            except Exception as exc:
                _pack_inspect_error(payload, "content_hash_recompute_failed", f"{type(exc).__name__}: {exc}")
                payload["validation"]["content_hash_valid"] = False
                payload["content_hash"]["valid"] = False

    no_source_ids = True
    for member_name in PACK_INSPECT_REQUIRED_MEMBERS:
        member_text = raw_member_bytes.get(member_name, b"").decode("utf-8", errors="ignore")
        if PACK_INSPECT_SOURCE_ID_RE.search(member_text):
            no_source_ids = False
            _pack_inspect_error(payload, "source_memory_id_leak", f"source-like memory ID leak detected in {member_name}")
    payload["validation"]["no_source_memory_ids"] = no_source_ids

    try:
        rows = _pack_inspect_rows_from_jsonl(raw_member_bytes["content/memories.jsonl"])
        if not rows:
            raise ValueError("content/memories.jsonl is empty")
        payload["validation"]["jsonl_rows_parse"] = True
    except Exception as exc:
        _pack_inspect_error(payload, "memories_jsonl_parse_error", f"{type(exc).__name__}: {exc}")
        payload["validation"]["jsonl_rows_parse"] = False
        rows = []

    try:
        topics_json = _pack_inspect_parse_json(raw_member_bytes["content/topics.json"], "content/topics.json")
        file_fingerprints_json = _pack_inspect_parse_json(
            raw_member_bytes["content/file_fingerprints.json"], "content/file_fingerprints.json"
        )
        origin_json = _pack_inspect_parse_json(raw_member_bytes["provenance/origin.json"], "provenance/origin.json")
        redactions_json = _pack_inspect_parse_json(raw_member_bytes["provenance/redactions.json"], "provenance/redactions.json")
        payload["validation"]["json_members_parse"] = bool(payload["validation"].get("json_members_parse", True))
    except Exception as exc:
        _pack_inspect_error(payload, "json_member_parse_error", f"{type(exc).__name__}: {exc}")
        payload["validation"]["json_members_parse"] = False
        topics_json = {}
        file_fingerprints_json = {}
        origin_json = {}
        redactions_json = {}

    if origin_json:
        exported_at = str(origin_json.get("exported_at", "") or "")
        if exported_at and not PACK_INSPECT_UTC_TS_RE.match(exported_at):
            _pack_inspect_error(payload, "invalid_provenance_exported_at", "provenance/origin.json exported_at must be ISO-8601 UTC")
            payload["validation"]["json_members_parse"] = False

    by_kind: dict[str, int] = {}
    by_namespace: dict[str, int] = {}
    by_origin: dict[str, int] = {}
    row_ids_seen: set[str] = set()
    redaction_rows_valid = True
    for row in rows:
        required_row_fields = [
            "row_id_in_pack",
            "kind",
            "namespace_at_export",
            "origin_at_export",
            "text_fields",
            "topics",
            "created_at_in_source",
            "git_sha_at_write",
            "git_branch_at_write",
            "git_dirty_at_write",
            "touched_files",
            "import_freshness_at_export",
            "redaction_applied",
        ]
        missing_fields = [name for name in required_row_fields if name not in row]
        if missing_fields:
            _pack_inspect_error(payload, "row_missing_fields", f"row missing required fields: {missing_fields}")
            payload["validation"]["jsonl_rows_parse"] = False
            continue

        row_id = str(row.get("row_id_in_pack", ""))
        kind_name = str(row.get("kind", ""))
        if row_id in row_ids_seen:
            _pack_inspect_error(payload, "duplicate_row_id_in_pack", f"duplicate row_id_in_pack: {row_id}")
            payload["validation"]["jsonl_rows_parse"] = False
        row_ids_seen.add(row_id)

        if row_id.startswith("mem_"):
            _pack_inspect_error(payload, "source_memory_id_leak", f"row_id_in_pack resembles source memory ID: {row_id}")
            payload["validation"]["no_source_memory_ids"] = False

        if kind_name == "context_block":
            if not re.match(r"^ctx_\d{3,}$", row_id):
                _pack_inspect_error(payload, "row_id_format", f"context_block row_id_in_pack must match ctx_###: {row_id}")
                payload["validation"]["jsonl_rows_parse"] = False
        elif kind_name == "hippocampus_entry":
            if not re.match(r"^hip_\d{3,}$", row_id):
                _pack_inspect_error(payload, "row_id_format", f"hippocampus_entry row_id_in_pack must match hip_###: {row_id}")
                payload["validation"]["jsonl_rows_parse"] = False
        else:
            _pack_inspect_error(payload, "non_exportable_kind", f"row kind is not exportable: {kind_name}")
            payload["validation"]["jsonl_rows_parse"] = False

        text_fields = row.get("text_fields")
        if not isinstance(text_fields, dict):
            _pack_inspect_error(payload, "text_fields_type", f"row {row_id} text_fields must be an object")
            payload["validation"]["jsonl_rows_parse"] = False
        else:
            for key, value in text_fields.items():
                if str(key) not in PACK_REDACTION_TEXT_FIELDS:
                    _pack_inspect_warning(payload, "unexpected_text_field", f"row {row_id} contains unexpected text_fields key: {key}")
                if value is not None and not isinstance(value, str):
                    _pack_inspect_error(payload, "text_field_value_type", f"row {row_id} text_fields.{key} must be string or null")
                    payload["validation"]["jsonl_rows_parse"] = False

        topics_value = row.get("topics")
        if not isinstance(topics_value, list) or not all(isinstance(item, str) for item in topics_value):
            _pack_inspect_error(payload, "topics_type", f"row {row_id} topics must be a list of strings")
            payload["validation"]["jsonl_rows_parse"] = False

        touched_files = row.get("touched_files")
        if not isinstance(touched_files, list):
            _pack_inspect_error(payload, "touched_files_type", f"row {row_id} touched_files must be a list")
            payload["validation"]["jsonl_rows_parse"] = False
        else:
            for item in touched_files:
                if not isinstance(item, dict):
                    _pack_inspect_error(payload, "touched_file_item_type", f"row {row_id} touched_files item must be object")
                    payload["validation"]["jsonl_rows_parse"] = False
                    continue
                path_value = normalize_optional_string(item.get("path"))
                if path_value is None:
                    _pack_inspect_error(payload, "touched_file_path_missing", f"row {row_id} touched_files.path is required")
                    payload["validation"]["jsonl_rows_parse"] = False
                    continue
                normalized_path = path_value.replace("\\", "/")
                if normalized_path.startswith("/") or ":" in normalized_path:
                    _pack_inspect_error(payload, "touched_file_path_invalid", f"row {row_id} touched_files.path must be relative")
                    payload["validation"]["jsonl_rows_parse"] = False
                    continue
                segments = normalized_path.split("/")
                if any(segment in {"", ".", ".."} for segment in segments):
                    _pack_inspect_error(payload, "touched_file_path_invalid", f"row {row_id} touched_files.path contains traversal")
                    payload["validation"]["jsonl_rows_parse"] = False
                if "file_sha" in item and item.get("file_sha") is not None and not isinstance(item.get("file_sha"), str):
                    _pack_inspect_error(payload, "touched_file_sha_type", f"row {row_id} touched_files.file_sha must be string")
                    payload["validation"]["jsonl_rows_parse"] = False

        if row.get("redaction_applied") is not True:
            redaction_rows_valid = False
            _pack_inspect_error(payload, "row_redaction_applied_false", f"row {row_id} redaction_applied must be true")

        namespace_value = str(row.get("namespace_at_export", DEFAULT_MEMORY_NAMESPACE))
        origin_value = str(row.get("origin_at_export", DEFAULT_MEMORY_ORIGIN))
        by_kind[kind_name] = int(by_kind.get(kind_name, 0) + 1)
        by_namespace[namespace_value] = int(by_namespace.get(namespace_value, 0) + 1)
        by_origin[origin_value] = int(by_origin.get(origin_value, 0) + 1)

    counts_payload = payload.get("counts", {})
    if isinstance(counts_payload, dict):
        counts_payload["rows"] = len(rows)
        counts_payload["by_kind"] = by_kind
        counts_payload["by_namespace"] = by_namespace
        counts_payload["by_origin"] = by_origin

    manifest_selection = manifest.get("selection", {})
    manifest_counts = manifest.get("counts", {})
    manifest_redaction = manifest.get("redaction", {})
    manifest_ruleset = str(manifest.get("redaction_ruleset_version", "") or "")
    manifest_rules_applied = manifest.get("redaction_rules_applied", [])
    manifest_rules_applied_valid = True
    if not isinstance(manifest_rules_applied, list) or not all(isinstance(item, str) for item in manifest_rules_applied):
        _pack_inspect_error(payload, "manifest_rules_applied_invalid", "manifest.redaction_rules_applied must be a list of strings")
        manifest_rules_applied_valid = False
        manifest_rules_applied = []

    row_count_matches_manifest = True
    try:
        exported_rows = int(manifest_selection.get("exported_rows", -1))
        total_rows = int(manifest_selection.get("total_rows", -1))
        limited_value = bool(manifest_selection.get("limited", False))
        if exported_rows != len(rows):
            row_count_matches_manifest = False
            _pack_inspect_error(payload, "row_count_mismatch", "manifest.selection.exported_rows does not match memories.jsonl row count")
        if exported_rows > total_rows:
            row_count_matches_manifest = False
            _pack_inspect_error(payload, "selection_count_invalid", "manifest.selection.exported_rows cannot exceed total_rows")
        if exported_rows < total_rows and not limited_value:
            _pack_inspect_warning(payload, "selection_limited_inconsistent", "manifest.selection.limited should be true when exported_rows < total_rows")
        if exported_rows == total_rows and limited_value:
            _pack_inspect_warning(payload, "selection_limited_inconsistent", "manifest.selection.limited is true while exported_rows == total_rows")
    except Exception as exc:
        row_count_matches_manifest = False
        _pack_inspect_error(payload, "selection_parse_error", f"{type(exc).__name__}: {exc}")
    payload["validation"]["row_count_matches_manifest"] = bool(row_count_matches_manifest)

    manifest_by_kind = {}
    if isinstance(manifest_counts, dict):
        manifest_by_kind = manifest_counts.get("by_kind", {})
    if not isinstance(manifest_by_kind, dict):
        _pack_inspect_error(payload, "manifest_by_kind_invalid", "manifest.counts.by_kind must be an object")
        payload["validation"]["row_count_matches_manifest"] = False
    else:
        sum_by_kind = 0
        kind_values_valid = True
        for value in manifest_by_kind.values():
            if not isinstance(value, int) or int(value) < 0:
                kind_values_valid = False
                break
            sum_by_kind += int(value)
        if not kind_values_valid or sum_by_kind != len(rows):
            _pack_inspect_error(payload, "manifest_by_kind_mismatch", "manifest.counts.by_kind totals must equal exported row count")
            payload["validation"]["row_count_matches_manifest"] = False

    topics_rows = topics_json.get("topics", []) if isinstance(topics_json, dict) else []
    files_rows = file_fingerprints_json.get("files", []) if isinstance(file_fingerprints_json, dict) else []
    if isinstance(counts_payload, dict):
        counts_payload["topics"] = len(topics_rows) if isinstance(topics_rows, list) else 0
        counts_payload["referenced_files"] = len(files_rows) if isinstance(files_rows, list) else 0
    if isinstance(topics_rows, list):
        for row in topics_rows:
            if not isinstance(row, dict) or not isinstance(row.get("row_count"), int) or int(row.get("row_count")) < 0:
                _pack_inspect_error(payload, "topics_row_invalid", "content/topics.json contains invalid topic row_count")
                payload["validation"]["json_members_parse"] = False
                break
    else:
        _pack_inspect_error(payload, "topics_payload_invalid", "content/topics.json topics must be a list")
        payload["validation"]["json_members_parse"] = False

    if isinstance(files_rows, list):
        for row in files_rows:
            if not isinstance(row, dict) or not isinstance(row.get("memory_count"), int) or int(row.get("memory_count")) < 0:
                _pack_inspect_error(payload, "file_fingerprint_invalid", "content/file_fingerprints.json contains invalid memory_count")
                payload["validation"]["json_members_parse"] = False
                break
    else:
        _pack_inspect_error(payload, "file_fingerprints_payload_invalid", "content/file_fingerprints.json files must be a list")
        payload["validation"]["json_members_parse"] = False

    redaction_metadata_valid = bool(manifest_rules_applied_valid)
    provenance_ruleset = str(redactions_json.get("ruleset_version", "") or "") if isinstance(redactions_json, dict) else ""
    if manifest_ruleset != provenance_ruleset:
        redaction_metadata_valid = False
        _pack_inspect_error(payload, "redaction_ruleset_mismatch", "manifest and provenance redaction ruleset_version differ")
    if manifest_ruleset != BASELINE_REDACTION_RULESET_VERSION:
        payload["validation"]["supported_schema"] = False
        redaction_metadata_valid = False
        _pack_inspect_error(
            payload,
            "unsupported_redaction_ruleset",
            f"Unsupported redaction_ruleset_version: {manifest_ruleset}",
        )

    provenance_rules_applied = redactions_json.get("rules_applied", []) if isinstance(redactions_json, dict) else []
    if list(manifest_rules_applied) != list(provenance_rules_applied):
        redaction_metadata_valid = False
        _pack_inspect_error(payload, "redaction_rules_applied_mismatch", "manifest and provenance rules_applied differ")
    if "ipv6" in list(manifest_rules_applied):
        redaction_metadata_valid = False
        _pack_inspect_error(payload, "unsupported_redaction_rule", "rules_applied contains unsupported rule ipv6")

    def _dict_int_map(value: Any) -> dict[str, int] | None:
        if not isinstance(value, dict):
            return None
        out: dict[str, int] = {}
        for key, raw in value.items():
            if not isinstance(raw, int):
                return None
            out[str(key)] = int(raw)
        return out

    manifest_total_matches = int(manifest_redaction.get("total_matches", -1)) if isinstance(manifest_redaction, dict) else -1
    manifest_affected_rows = int(manifest_redaction.get("affected_rows", -1)) if isinstance(manifest_redaction, dict) else -1
    prov_total_matches = int(redactions_json.get("total_matches", -1)) if isinstance(redactions_json, dict) else -1
    prov_affected_rows = int(redactions_json.get("affected_rows", -1)) if isinstance(redactions_json, dict) else -1
    manifest_by_category = _dict_int_map(manifest_redaction.get("by_category") if isinstance(manifest_redaction, dict) else None)
    prov_by_category = _dict_int_map(redactions_json.get("by_category") if isinstance(redactions_json, dict) else None)
    manifest_rows_by_category = _dict_int_map(
        manifest_redaction.get("rows_by_category") if isinstance(manifest_redaction, dict) else None
    )
    prov_rows_by_category = _dict_int_map(redactions_json.get("rows_by_category") if isinstance(redactions_json, dict) else None)

    if (
        manifest_total_matches != prov_total_matches
        or manifest_affected_rows != prov_affected_rows
        or manifest_by_category is None
        or prov_by_category is None
        or manifest_rows_by_category is None
        or prov_rows_by_category is None
        or manifest_by_category != prov_by_category
        or manifest_rows_by_category != prov_rows_by_category
    ):
        redaction_metadata_valid = False
        _pack_inspect_error(payload, "redaction_count_mismatch", "manifest and provenance redaction metadata differ")

    if not redaction_rows_valid:
        redaction_metadata_valid = False

    payload["validation"]["redaction_metadata_valid"] = bool(redaction_metadata_valid)
    if isinstance(counts_payload, dict):
        counts_payload["redaction_total_matches"] = max(0, manifest_total_matches)
        counts_payload["redaction_affected_rows"] = max(0, manifest_affected_rows)

    payload["_rows_for_samples"] = rows
    return _pack_inspect_finalize(payload, include_samples=include_samples, sample_limit=sample_limit)


def _pack_sort_timestamp_iso(value: Any) -> float:
    stamp = normalize_optional_string(value)
    if not stamp:
        return 0.0
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return float(parsed.astimezone(timezone.utc).timestamp())


def pack_preview(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only selector preview for future memory-pack export."""
    if store_backend() != "sqlite":
        return tool_error("pack_preview requires sqlite backend")

    try:
        parsed = _pack_parse_common_filters(args)
        sample_per_kind = _safe_int(args.get("sample_per_kind"), 3, minimum=0, maximum=20)
        include_samples = parse_bool(args.get("include_samples"), default=True)
    except PackSelectorError as exc:
        return tool_error_code(exc.code, exc.message)
    except ValueError as exc:
        return tool_error(str(exc))

    warnings = _pack_kind_policy_warnings(args, list(parsed["kinds"]))

    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            selection = _pack_selection_context(conn, args, parsed, warnings)

            where_sql = str(selection["where_sql"])
            sql_params = list(selection["sql_params"])
            total_rows = int(selection["total_rows"])
            row_ids = list(selection["row_ids"])
            limit = int(selection["limit"])

            by_kind_rows = conn.execute(
                f"SELECT m.kind, COUNT(*) AS count FROM memories m WHERE {where_sql} GROUP BY m.kind ORDER BY m.kind ASC",
                tuple(sql_params),
            ).fetchall()
            by_kind = {
                str(row["kind"] if isinstance(row, sqlite3.Row) else row[0]): int(
                    row["count"] if isinstance(row, sqlite3.Row) else row[1]
                )
                for row in by_kind_rows
            }

            by_namespace_rows = conn.execute(
                f"SELECT m.namespace, COUNT(*) AS count FROM memories m WHERE {where_sql} GROUP BY m.namespace ORDER BY m.namespace ASC",
                tuple(sql_params),
            ).fetchall()
            by_namespace = {
                str(row["namespace"] if isinstance(row, sqlite3.Row) else row[0]): int(
                    row["count"] if isinstance(row, sqlite3.Row) else row[1]
                )
                for row in by_namespace_rows
            }

            by_origin_rows = conn.execute(
                f"SELECT m.origin, COUNT(*) AS count FROM memories m WHERE {where_sql} GROUP BY m.origin ORDER BY m.origin ASC",
                tuple(sql_params),
            ).fetchall()
            by_origin = {
                str(row["origin"] if isinstance(row, sqlite3.Row) else row[0]): int(
                    row["count"] if isinstance(row, sqlite3.Row) else row[1]
                )
                for row in by_origin_rows
            }

            topic_rows = conn.execute(
                "SELECT mt.topic, COUNT(*) AS count "
                "FROM memory_topics mt "
                "JOIN memories m ON m.id = mt.memory_id "
                f"WHERE {where_sql} "
                "GROUP BY mt.topic "
                "ORDER BY count DESC, mt.topic ASC "
                "LIMIT 20",
                tuple(sql_params),
            ).fetchall()
            topic_total_row = conn.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT mt.topic "
                "FROM memory_topics mt "
                "JOIN memories m ON m.id = mt.memory_id "
                f"WHERE {where_sql} "
                "GROUP BY mt.topic"
                ")",
                tuple(sql_params),
            ).fetchone()
            topic_total = int(topic_total_row[0] if topic_total_row else 0)
            by_topic = {
                str(row["topic"] if isinstance(row, sqlite3.Row) else row[0]): int(
                    row["count"] if isinstance(row, sqlite3.Row) else row[1]
                )
                for row in topic_rows
            }

            referenced_file_count_row = conn.execute(
                "SELECT COUNT(DISTINCT mf.path) "
                "FROM memory_files mf "
                "JOIN memories m ON m.id = mf.memory_id AND m.kind = mf.memory_table "
                f"WHERE {where_sql}",
                tuple(sql_params),
            ).fetchone()
            referenced_file_count = int(referenced_file_count_row[0] if referenced_file_count_row else 0)
            referenced_files_rows = conn.execute(
                "SELECT mf.path, COUNT(DISTINCT mf.memory_id) AS count "
                "FROM memory_files mf "
                "JOIN memories m ON m.id = mf.memory_id AND m.kind = mf.memory_table "
                f"WHERE {where_sql} "
                "GROUP BY mf.path "
                "ORDER BY count DESC, mf.path ASC "
                "LIMIT 20",
                tuple(sql_params),
            ).fetchall()
            top_referenced_files = [
                {
                    "path": str(row["path"] if isinstance(row, sqlite3.Row) else row[0]),
                    "count": int(row["count"] if isinstance(row, sqlite3.Row) else row[1]),
                }
                for row in referenced_files_rows
            ]

            samples: dict[str, list[dict[str, Any]]] = {}
            if include_samples and sample_per_kind > 0 and total_rows > 0:
                sample_rows = conn.execute(
                    "SELECT m.id, m.kind, m.text, m.created_at, m.updated_at, m.namespace, m.origin, m.import_freshness "
                    f"FROM memories m WHERE {where_sql} ORDER BY {selection['order_sql']}",
                    tuple(sql_params),
                ).fetchall()
                quotas = {kind_name: sample_per_kind for kind_name in by_kind}
                for row in sample_rows:
                    row_kind = str(row["kind"] if isinstance(row, sqlite3.Row) else row[1])
                    if quotas.get(row_kind, 0) <= 0:
                        continue
                    namespace_value = str(row["namespace"] if isinstance(row, sqlite3.Row) else row[5])
                    sample_item = {
                        "id": str(row["id"] if isinstance(row, sqlite3.Row) else row[0]),
                        "kind": row_kind,
                        "namespace": namespace_value,
                        "origin": str(row["origin"] if isinstance(row, sqlite3.Row) else row[6]),
                        "import_freshness": normalize_optional_string(
                            row["import_freshness"] if isinstance(row, sqlite3.Row) else row[7]
                        ),
                        "pack_id": derive_pack_id_from_namespace(namespace_value),
                        "created_at": normalize_optional_string(
                            row["created_at"] if isinstance(row, sqlite3.Row) else row[3]
                        ),
                        "updated_at": normalize_optional_string(
                            row["updated_at"] if isinstance(row, sqlite3.Row) else row[4]
                        ),
                        "text_preview": collapsed_preview_text(
                            row["text"] if isinstance(row, sqlite3.Row) else row[2],
                            max_chars=200,
                        ),
                    }
                    samples.setdefault(row_kind, []).append(sample_item)
                    quotas[row_kind] = quotas.get(row_kind, 0) - 1
                    if all(remaining <= 0 for remaining in quotas.values()):
                        break
                samples = {kind_name: samples[kind_name] for kind_name in sorted(samples)}

    except PackSelectorError as exc:
        return tool_error_code(exc.code, exc.message)
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")

    structured: dict[str, Any] = {
        "action": "pack_preview",
        "status": "ok",
        "filters": {
            "topics": list(selection["topics"]),
            "kinds": list(selection["kinds"]),
            "memory_ids": list(selection["resolved_memory_ids"]),
            "group_id": selection["group_id"],
            "scope": selection["scope"],
            "namespaces": list(selection["resolved_namespaces"]),
            "origins": list(selection["resolved_origins"]) if selection["resolved_origins"] is not None else [],
            "created_after": selection["created_after"],
            "created_before": selection["created_before"],
            "touched_paths": list(selection["touched_paths"]),
        },
        "selection": {
            "total_rows": int(selection["total_rows"]),
            "limited": bool(selection["limited"]),
            "limit": int(selection["limit"]),
            "row_ids": list(selection["row_ids"]),
        },
        "counts": {
            "by_kind": by_kind,
            "by_namespace": by_namespace,
            "by_origin": by_origin,
            "by_topic": by_topic,
            "by_topic_limited": bool(topic_total > 20),
        },
        "files": {
            "referenced_file_count": int(referenced_file_count),
            "top_referenced_files": top_referenced_files,
        },
        "aliases": {
            "referenced_alias_count": 0,
            "top_alias_concepts": [],
        },
        "samples": samples if include_samples else {},
        "warnings": warnings,
    }

    top_topics_line = ", ".join(f"{topic}={count}" for topic, count in list(by_topic.items())[:5]) or "none"
    top_files_line = ", ".join(f"{row['path']}={row['count']}" for row in top_referenced_files[:5]) or "none"
    warning_line = "; ".join(f"{item['code']}: {item['message']}" for item in warnings) if warnings else "none"
    lines = [
        f"Pack preview rows: {total_rows} (limited={str(total_rows > limit).lower()}, limit={limit})",
        f"By kind: {by_kind}",
        f"By namespace: {by_namespace}",
        f"Top topics: {top_topics_line}",
        f"Top referenced files: {top_files_line}",
        f"Warnings: {warning_line}",
    ]
    return text_result("\n".join(lines), structured)


def pack_redaction_preview(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only redaction dry-run over the pack selection engine."""
    if store_backend() != "sqlite":
        return tool_error("pack_redaction_preview requires sqlite backend")

    try:
        parsed = _pack_parse_common_filters(args)
        include_redacted_samples = parse_bool(args.get("include_redacted_samples"), default=True)
        max_redacted_samples = _safe_int(args.get("max_redacted_samples"), 10, minimum=0, maximum=500)
    except PackSelectorError as exc:
        return tool_error_code(exc.code, exc.message)
    except ValueError as exc:
        return tool_error(str(exc))

    warnings = _pack_kind_policy_warnings(args, list(parsed["kinds"]))
    warnings.append(_pack_baseline_warning())
    if max_redacted_samples > 50:
        max_redacted_samples = 50
        warnings.append(
            {
                "code": "max_redacted_samples_capped",
                "message": "max_redacted_samples was capped to 50.",
            }
        )

    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            selection = _pack_selection_context(conn, args, parsed, warnings)
    except PackSelectorError as exc:
        return tool_error_code(exc.code, exc.message)
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")

    total_rows = int(selection["total_rows"])
    limit = int(selection["limit"])
    row_ids = list(selection["row_ids"])
    limited = bool(total_rows > limit)
    if limited:
        warnings.append(
            {
                "code": "redaction_counts_limited",
                "message": "Redaction counts reflect only the limited scanned row set. Increase limit to scan more selected rows.",
            }
        )

    by_category: dict[str, int] = {}
    rows_by_category: dict[str, int] = {}
    total_matches = 0
    affected_rows = 0
    sample_candidates: list[dict[str, Any]] = []

    for row in selection["selected_rows"]:
        row_redaction = _pack_row_redaction(row)
        category_counts = dict(row_redaction["by_category"])
        match_count = int(row_redaction["total_matches"])
        if match_count <= 0:
            continue

        affected_rows += 1
        total_matches += int(match_count)
        categories = [name for name in PACK_REDACTION_RULE_ORDER if int(category_counts.get(name, 0)) > 0]
        for category in categories:
            by_category[category] = int(by_category.get(category, 0) + int(category_counts.get(category, 0)))
            rows_by_category[category] = int(rows_by_category.get(category, 0) + 1)

        if include_redacted_samples:
            memory_id = str(row.get("id", ""))
            updated_ts = _pack_sort_timestamp_iso(row.get("updated_at"))
            created_ts = _pack_sort_timestamp_iso(row.get("created_at"))
            sort_ts = updated_ts if updated_ts > 0 else created_ts
            sample_candidates.append(
                {
                    "memory_id": memory_id,
                    "kind": str(row.get("kind", "")),
                    "namespace": str(row.get("namespace", DEFAULT_MEMORY_NAMESPACE)),
                    "origin": str(row.get("origin", DEFAULT_MEMORY_ORIGIN)),
                    "categories": categories,
                    "match_count": int(match_count),
                    "redacted_preview": str(row_redaction["redacted_preview"]),
                    "_sort_ts": sort_ts,
                }
            )

    samples: list[dict[str, Any]] = []
    if include_redacted_samples and max_redacted_samples > 0 and sample_candidates:
        sample_candidates.sort(
            key=lambda item: (
                -int(item.get("match_count", 0)),
                -float(item.get("_sort_ts", 0.0)),
                str(item.get("memory_id", "")),
            )
        )
        for item in sample_candidates[:max_redacted_samples]:
            sample = dict(item)
            sample.pop("_sort_ts", None)
            samples.append(sample)

    structured: dict[str, Any] = {
        "action": "pack_redaction_preview",
        "status": "ok",
        "filters": {
            "topics": list(selection["topics"]),
            "kinds": list(selection["kinds"]),
            "memory_ids": list(selection["resolved_memory_ids"]),
            "group_id": selection["group_id"],
            "scope": selection["scope"],
            "namespaces": list(selection["resolved_namespaces"]),
            "origins": list(selection["resolved_origins"]) if selection["resolved_origins"] is not None else [],
            "created_after": selection["created_after"],
            "created_before": selection["created_before"],
            "touched_paths": list(selection["touched_paths"]),
        },
        "selection": {
            "total_rows": total_rows,
            "limited": limited,
            "limit": limit,
            "row_ids": row_ids,
        },
        "redaction": {
            "total_matches": int(total_matches),
            "affected_rows": int(affected_rows),
            "by_category": by_category,
            "rows_by_category": rows_by_category,
            "rules_applied": list(PACK_REDACTION_RULE_ORDER),
            "ruleset_version": BASELINE_REDACTION_RULESET_VERSION,
        },
        "samples": samples if include_redacted_samples else [],
        "warnings": warnings,
    }

    warning_line = "; ".join(f"{item['code']}: {item['message']}" for item in warnings) if warnings else "none"
    lines = [
        f"Pack redaction preview selected rows: {total_rows} (limited={str(limited).lower()}, limit={limit})",
        f"Affected rows: {affected_rows}",
        f"Total matches: {total_matches}",
        f"By category: {by_category}",
        f"Warnings: {warning_line}",
    ]
    return text_result("\n".join(lines), structured)


def _pack_validate_export_kinds(args: dict[str, Any], kinds: list[str]) -> None:
    if "kinds" not in args or args.get("kinds") is None:
        return
    for kind_name in kinds:
        if kind_name in PACK_PREVIEW_POLICY_WARNING_KINDS:
            raise ValueError(PACK_KIND_PREVIEW_ERROR_TEMPLATE.format(kind=kind_name))
        if kind_name not in PACK_EXPORT_ALLOWED_KINDS:
            raise ValueError(f"kind '{kind_name}' is not exportable in v1 policy")


def _pack_topics_by_memory_id(conn: sqlite3.Connection, row_ids: list[str]) -> dict[str, list[str]]:
    topic_map = {memory_id: [] for memory_id in row_ids}
    if not row_ids:
        return topic_map
    placeholders = ",".join("?" for _ in row_ids)
    rows = conn.execute(
        f"SELECT memory_id, topic FROM memory_topics WHERE memory_id IN ({placeholders}) ORDER BY topic ASC",
        tuple(row_ids),
    ).fetchall()
    for row in rows:
        memory_id = str(row["memory_id"] if isinstance(row, sqlite3.Row) else row[0])
        topic = str(row["topic"] if isinstance(row, sqlite3.Row) else row[1])
        topic_map.setdefault(memory_id, []).append(topic)
    return topic_map


def _pack_files_by_memory_id(conn: sqlite3.Connection, row_ids: list[str]) -> dict[str, list[dict[str, str]]]:
    file_map: dict[str, list[dict[str, str]]] = {memory_id: [] for memory_id in row_ids}
    if not row_ids:
        return file_map
    placeholders = ",".join("?" for _ in row_ids)
    rows = conn.execute(
        "SELECT mf.memory_id, mf.path, mf.file_sha "
        "FROM memory_files mf "
        "JOIN memories m ON m.id = mf.memory_id AND m.kind = mf.memory_table "
        f"WHERE mf.memory_id IN ({placeholders}) "
        "ORDER BY mf.memory_id ASC, mf.path ASC, mf.file_sha ASC",
        tuple(row_ids),
    ).fetchall()
    dedupe: dict[str, set[tuple[str, str]]] = {}
    for row in rows:
        memory_id = str(row["memory_id"] if isinstance(row, sqlite3.Row) else row[0])
        path_value = str(row["path"] if isinstance(row, sqlite3.Row) else row[1])
        sha_value = str(row["file_sha"] if isinstance(row, sqlite3.Row) else row[2])
        key = (path_value, sha_value)
        seen = dedupe.setdefault(memory_id, set())
        if key in seen:
            continue
        seen.add(key)
        file_map.setdefault(memory_id, []).append({"path": path_value, "file_sha": sha_value})
    return file_map


def _memory_group_is_mechanical_topic(topic: str) -> bool:
    topic_value = str(topic).strip().lower()
    return any(topic_value.startswith(prefix) for prefix in MEMORY_GROUP_MECHANICAL_TOPIC_PREFIXES)


def _memory_group_slug_label(value: str) -> str:
    text = str(value).replace("_", " ").replace("-", " ").replace("/", " ").strip()
    if not text:
        return "Unlabeled Group"
    return " ".join(part.capitalize() for part in text.split())


def _memory_group_path_parent(path_value: str) -> str | None:
    normalized = str(path_value).replace("\\", "/").strip("/")
    if not normalized or "/" not in normalized:
        return None
    parent = normalized.rsplit("/", 1)[0].strip("/")
    return parent or None


def _memory_group_linked_ids_from_raw(raw_value: Any) -> list[str]:
    text = normalize_optional_string(raw_value)
    if text is None:
        return []
    try:
        parsed = json.loads(text)
    except Exception:
        return []
    return normalize_linked_ids(parsed)


def _memory_group_base_rows(
    conn: sqlite3.Connection,
    args: dict[str, Any],
) -> dict[str, Any]:
    resolved_namespaces, resolved_origins = resolve_namespace_origin_filters(args, conn)
    seed_topics = normalize_optional_string_list(args.get("topics"), "topics") or []
    domains = normalize_optional_string_list(args.get("domains"), "domains") or []

    clauses = [
        "m.deleted = 0",
        "(m.superseded_by IS NULL OR m.superseded_by = '')",
    ]
    sql_params: list[Any] = []

    namespace_placeholders = ",".join("?" for _ in resolved_namespaces)
    clauses.append(f"m.namespace IN ({namespace_placeholders})")
    sql_params.extend(resolved_namespaces)

    if resolved_origins:
        origin_placeholders = ",".join("?" for _ in resolved_origins)
        clauses.append(f"m.origin IN ({origin_placeholders})")
        sql_params.extend(resolved_origins)

    if seed_topics:
        topic_placeholders = ",".join("?" for _ in seed_topics)
        clauses.append(
            "EXISTS ("
            "SELECT 1 FROM memory_topics mt "
            "WHERE mt.memory_id = m.id "
            f"AND mt.topic IN ({topic_placeholders})"
            ")"
        )
        sql_params.extend(seed_topics)

    if domains:
        domain_placeholders = ",".join("?" for _ in domains)
        clauses.append(f"m.domain IN ({domain_placeholders})")
        sql_params.extend(domains)

    where_sql = " AND ".join(clauses)
    rows = conn.execute(
        "SELECT m.id, m.kind, m.text, m.title, m.preview, m.domain, m.namespace, m.origin, "
        "m.import_freshness, m.git_sha, m.git_branch, m.git_dirty, m.created_at, m.updated_at, "
        "m.linked_ids_json, m.normalized_hash, m.shingle_hashes_json "
        f"FROM memories m WHERE {where_sql} "
        "ORDER BY COALESCE(m.updated_at, m.created_at, '') DESC, m.id ASC",
        tuple(sql_params),
    ).fetchall()
    memory_ids = [str(row["id"] if isinstance(row, sqlite3.Row) else row[0]) for row in rows]
    topics_by_memory_id = _pack_topics_by_memory_id(conn, memory_ids)
    files_by_memory_id = _pack_files_by_memory_id(conn, memory_ids)

    payload_rows: list[dict[str, Any]] = []
    row_map: dict[str, dict[str, Any]] = {}
    for row in rows:
        memory_id = str(row["id"] if isinstance(row, sqlite3.Row) else row[0])
        payload = {
            "id": memory_id,
            "kind": str(row["kind"] if isinstance(row, sqlite3.Row) else row[1]),
            "text": str(row["text"] if isinstance(row, sqlite3.Row) else row[2]),
            "title": normalize_optional_string(row["title"] if isinstance(row, sqlite3.Row) else row[3]),
            "preview": normalize_optional_string(row["preview"] if isinstance(row, sqlite3.Row) else row[4]),
            "domain": normalize_optional_string(row["domain"] if isinstance(row, sqlite3.Row) else row[5]),
            "namespace": str(row["namespace"] if isinstance(row, sqlite3.Row) else row[6]),
            "origin": str(row["origin"] if isinstance(row, sqlite3.Row) else row[7]),
            "import_freshness": normalize_optional_string(
                row["import_freshness"] if isinstance(row, sqlite3.Row) else row[8]
            ),
            "git_sha": normalize_optional_string(row["git_sha"] if isinstance(row, sqlite3.Row) else row[9]),
            "git_branch": normalize_optional_string(row["git_branch"] if isinstance(row, sqlite3.Row) else row[10]),
            "git_dirty": normalize_git_dirty(row["git_dirty"] if isinstance(row, sqlite3.Row) else row[11]),
            "created_at": normalize_optional_string(row["created_at"] if isinstance(row, sqlite3.Row) else row[12]),
            "updated_at": normalize_optional_string(row["updated_at"] if isinstance(row, sqlite3.Row) else row[13]),
            "linked_ids": _memory_group_linked_ids_from_raw(
                row["linked_ids_json"] if isinstance(row, sqlite3.Row) else row[14]
            ),
            "normalized_hash": normalize_optional_string(
                row["normalized_hash"] if isinstance(row, sqlite3.Row) else row[15]
            ),
            "shingle_hashes": _load_json_string_list(
                row["shingle_hashes_json"] if isinstance(row, sqlite3.Row) else row[16]
            ),
            "topics": list(topics_by_memory_id.get(memory_id, [])),
            "touched_files": list(files_by_memory_id.get(memory_id, [])),
        }
        payload_rows.append(payload)
        row_map[memory_id] = payload

    return {
        "rows": payload_rows,
        "row_map": row_map,
        "resolved_namespaces": resolved_namespaces,
        "resolved_origins": resolved_origins,
        "seed_topics": seed_topics,
        "domains": domains,
    }


def _memory_group_collect_related_ids(
    core_ids: set[str],
    row_map: dict[str, dict[str, Any]],
    topic_index: dict[str, set[str]],
    domain_index: dict[str, set[str]],
    path_index: dict[str, set[str]],
    normalized_hash_index: dict[str, set[str]],
    shingle_index: dict[str, set[str]],
    link_index: dict[str, set[str]],
) -> tuple[list[str], dict[str, list[str]]]:
    reason_map: dict[str, list[str]] = {}
    score_map: dict[str, int] = {}

    def add_reason(memory_id: str, reason: str, weight: int) -> None:
        if memory_id in core_ids:
            return
        reasons = reason_map.setdefault(memory_id, [])
        if reason not in reasons:
            reasons.append(reason)
        score_map[memory_id] = int(score_map.get(memory_id, 0) + weight)

    for memory_id in core_ids:
        row = row_map.get(memory_id)
        if row is None:
            continue
        for topic_name in row.get("topics", []):
            for candidate_id in topic_index.get(str(topic_name), set()):
                add_reason(candidate_id, f"shared topic:{topic_name}", 3)
        domain_value = normalize_optional_string(row.get("domain"))
        if domain_value:
            for candidate_id in domain_index.get(domain_value, set()):
                add_reason(candidate_id, f"shared domain:{domain_value}", 2)
        for file_info in row.get("touched_files", []):
            path_value = normalize_optional_string(file_info.get("path"))
            if path_value:
                for candidate_id in path_index.get(path_value, set()):
                    add_reason(candidate_id, f"shared file:{path_value}", 2)
        for linked_id in row.get("linked_ids", []):
            if linked_id in row_map:
                add_reason(linked_id, f"explicit link:{memory_id}", 4)
        normalized_hash = normalize_optional_string(row.get("normalized_hash"))
        if normalized_hash:
            for candidate_id in normalized_hash_index.get(normalized_hash, set()):
                add_reason(candidate_id, "same normalized hash", 4)
        for shingle in row.get("shingle_hashes", []):
            for candidate_id in shingle_index.get(str(shingle), set()):
                add_reason(candidate_id, "shared shingles", 1)
        for candidate_id in link_index.get(memory_id, set()):
            add_reason(candidate_id, f"explicit link:{memory_id}", 4)

    related_ids = [
        memory_id
        for memory_id, score in sorted(
            score_map.items(),
            key=lambda item: (-int(item[1]), str(item[0])),
        )
        if score >= 2
    ]
    related_reason_map = {memory_id: sorted(reason_map.get(memory_id, [])) for memory_id in related_ids}
    return related_ids, related_reason_map


def _memory_group_summary_text(rows: list[dict[str, Any]]) -> str:
    titles = [str(row.get("title") or "").strip() for row in rows if str(row.get("title") or "").strip()]
    if titles:
        snippet = "; ".join(titles[:2])
        return snippet[:200]
    topics: list[str] = []
    for row in rows:
        for topic_name in row.get("topics", []):
            topic_value = str(topic_name)
            if topic_value and topic_value not in topics:
                topics.append(topic_value)
                if len(topics) >= 4:
                    break
        if len(topics) >= 4:
            break
    if topics:
        return f"Topics: {', '.join(topics[:4])}"
    domains = [str(row.get('domain') or '').strip() for row in rows if str(row.get('domain') or '').strip()]
    if domains:
        return f"Domain: {domains[0]}"
    return "Computed memory group from Mnemo runtime metadata."


def _memory_group_is_synthetic_group(group: dict[str, Any]) -> bool:
    group_type = str(group.get("group_type", "") or "")
    group_id = str(group.get("group_id", "") or "")
    label = str(group.get("label", "") or "")
    joined = f"{group_id}\n{label}".lower()
    if group_type == "path" and "state/mnemo/synthetic_files/" in joined:
        return True
    return (
        "synthetic_files" in joined
        or "synthetic ux-lab" in joined
        or "synthetic:" in joined
        or "ux-lab" in joined
    )


def _memory_group_catalog_bucket(group: dict[str, Any]) -> tuple[int, str]:
    group_type = str(group.get("group_type", "") or "")
    synthetic = _memory_group_is_synthetic_group(group)
    if not synthetic and group_type == "topic":
        return (0, group_type)
    if not synthetic and group_type == "domain":
        return (1, group_type)
    if not synthetic and group_type == "alias":
        return (2, group_type)
    if not synthetic and group_type == "path":
        return (3, group_type)
    return (4, group_type)


def _memory_group_catalog_description(group: dict[str, Any]) -> str:
    group_type = str(group.get("group_type", "") or "group")
    core_count = int(group.get("core_memory_count", 0) or 0)
    core_exportable = int(group.get("core_exportable_count", 0) or 0)
    scope_recommendation = "core"
    recommended_scopes = group.get("recommended_scopes", [])
    if isinstance(recommended_scopes, list) and recommended_scopes:
        first = normalize_optional_string(recommended_scopes[0])
        if first:
            scope_recommendation = first
    description = (
        f"{group_type} - core {core_count} memories - {core_exportable} exportable - scope: {scope_recommendation}"
    )
    if _memory_group_is_synthetic_group(group):
        description += " - synthetic UX-lab evidence"
    return description


def _memory_group_catalog_option(group: dict[str, Any]) -> dict[str, Any]:
    group_id = str(group.get("group_id", "") or "")
    label = str(group.get("label", "") or group_id)
    if _memory_group_is_synthetic_group(group) and "synthetic" not in label.lower():
        label = f"{label} [synthetic]"
    scope_recommendation = "core"
    recommended_scopes = group.get("recommended_scopes", [])
    if isinstance(recommended_scopes, list) and recommended_scopes:
        first = normalize_optional_string(recommended_scopes[0])
        if first:
            scope_recommendation = first
    return {
        "label": label,
        "value": group_id,
        "description": _memory_group_catalog_description(group),
        "group_id": group_id,
        "group_type": str(group.get("group_type", "") or ""),
        "scope_recommendation": scope_recommendation,
        "core_memory_count": int(group.get("core_memory_count", 0) or 0),
        "core_exportable_count": int(group.get("core_exportable_count", 0) or 0),
        "memory_count": int(group.get("memory_count", 0) or 0),
        "exportable_memory_count": int(group.get("exportable_memory_count", 0) or 0),
        "synthetic": _memory_group_is_synthetic_group(group),
    }


def _memory_group_confidence(
    *,
    core_rows: list[dict[str, Any]],
    related_count: int,
    query: str | None,
    label: str,
    reasons: list[str],
) -> float:
    base = 0.20 + min(len(core_rows), 8) * 0.08
    if related_count > 0:
        base += min(related_count, 6) * 0.03
    evidence_bonus = 0.0
    if any(row.get("topics") for row in core_rows):
        evidence_bonus += 0.10
    if any(normalize_optional_string(row.get("domain")) for row in core_rows):
        evidence_bonus += 0.07
    if any(row.get("touched_files") for row in core_rows):
        evidence_bonus += 0.07
    query_bonus = 0.0
    query_text = normalize_optional_string(query)
    if query_text:
        haystack = " ".join([label] + reasons + [str(row.get("title") or "") for row in core_rows[:5]]).lower()
        if query_text.lower() in haystack:
            query_bonus = 0.18
    return round(min(0.99, base + evidence_bonus + query_bonus), 3)


def _memory_group_alias_term_is_safe(normalized_term: str, weight: float) -> bool:
    term_value = str(normalized_term).strip()
    if not term_value:
        return False
    token_count = len([token for token in term_value.split(" ") if token])
    if token_count >= 2:
        return True
    if len(term_value) >= 4:
        return True
    return float(weight) >= 1.5


def _memory_group_alias_source_rows(
    conn: sqlite3.Connection,
    language: str = DEFAULT_ALIAS_LANGUAGE,
) -> list[dict[str, Any]]:
    wanted_language = _normalize_alias_language(language)
    sql = (
        "SELECT "
        "c.concept_id AS concept_id, "
        "c.canonical AS canonical, "
        "c.domain AS concept_domain, "
        "c.language AS concept_language, "
        "c.weight AS concept_weight, "
        "t.alias_id AS alias_id, "
        "t.term AS term, "
        "t.normalized_term AS normalized_term, "
        "t.domain AS term_domain, "
        "t.language AS term_language, "
        "t.weight AS term_weight "
        "FROM alias_terms t "
        "JOIN alias_concepts c ON c.concept_id = t.concept_id "
        "WHERE c.status = 'active' AND t.status = 'active' "
        "AND COALESCE(NULLIF(c.language, ''), ?) = ? "
        "AND COALESCE(NULLIF(t.language, ''), ?) = ? "
        "ORDER BY c.canonical, t.term"
    )
    rows = conn.execute(
        sql,
        (DEFAULT_ALIAS_LANGUAGE, wanted_language, DEFAULT_ALIAS_LANGUAGE, wanted_language),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "concept_id": str(row["concept_id"]),
                "canonical": str(row["canonical"] or ""),
                "concept_domain": normalize_optional_string(row["concept_domain"]),
                "concept_language": _normalize_alias_language(row["concept_language"]),
                "concept_weight": float(row["concept_weight"] or 1.0),
                "alias_id": str(row["alias_id"] or ""),
                "term": str(row["term"] or ""),
                "normalized_term": _normalize_alias_term(row["normalized_term"] or row["term"]),
                "term_domain": normalize_optional_string(row["term_domain"]),
                "term_language": _normalize_alias_language(row["term_language"]),
                "term_weight": float(row["term_weight"] or 1.0),
            }
        )
    return out


def _memory_group_alias_concepts(alias_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    concept_map: dict[str, dict[str, Any]] = {}
    for row in alias_rows:
        concept_id = str(row.get("concept_id") or "").strip()
        if not concept_id:
            continue
        canonical = normalize_optional_string(row.get("canonical")) or concept_id
        concept = concept_map.setdefault(
            concept_id,
            {
                "concept_id": concept_id,
                "canonical": canonical,
                "label": canonical,
                "domain": normalize_optional_string(row.get("concept_domain")) or normalize_optional_string(row.get("term_domain")),
                "language": _normalize_alias_language(row.get("concept_language") or row.get("term_language")),
                "weight": max(float(row.get("concept_weight") or 1.0), float(row.get("term_weight") or 1.0)),
                "terms": [],
            },
        )
        term_value = normalize_optional_string(row.get("term"))
        normalized_term = _normalize_alias_term(row.get("normalized_term") or term_value)
        term_weight = float(row.get("term_weight") or 1.0)
        if term_value and normalized_term:
            concept["terms"].append(
                {
                    "term": term_value,
                    "normalized_term": normalized_term,
                    "weight": term_weight,
                }
            )
    concepts = list(concept_map.values())
    concepts.sort(key=lambda item: (str(item.get("label", "")).lower(), str(item.get("concept_id", ""))))
    return concepts


def _memory_group_alias_memory_reasons(
    memory_row: dict[str, Any],
    concept: dict[str, Any],
) -> list[str]:
    normalized_text = _normalize_alias_term(
        " ".join(
            [
                str(memory_row.get("title") or ""),
                str(memory_row.get("text") or ""),
                str(memory_row.get("preview") or ""),
            ]
        )
    )
    if not normalized_text:
        return []
    reasons: list[str] = []
    canonical = normalize_optional_string(concept.get("canonical")) or str(concept.get("concept_id", ""))
    canonical_norm = _normalize_alias_term(canonical)
    concept_domain = normalize_optional_string(concept.get("domain"))
    memory_domain = normalize_optional_string(memory_row.get("domain"))
    if canonical_norm and _memory_group_alias_term_is_safe(canonical_norm, float(concept.get("weight") or 1.0)):
        if _normalized_term_in_text(normalized_text, canonical_norm):
            reasons.append(f"matched canonical term {canonical}")
    alias_hits: list[str] = []
    for term_info in concept.get("terms", []):
        if not isinstance(term_info, dict):
            continue
        normalized_term = _normalize_alias_term(term_info.get("normalized_term") or term_info.get("term"))
        term_weight = float(term_info.get("weight") or concept.get("weight") or 1.0)
        if not _memory_group_alias_term_is_safe(normalized_term, term_weight):
            continue
        if _normalized_term_in_text(normalized_text, normalized_term):
            term_value = normalize_optional_string(term_info.get("term")) or normalized_term
            if canonical_norm and normalized_term == canonical_norm:
                continue
            if term_value not in alias_hits:
                alias_hits.append(term_value)
    for term_value in alias_hits:
        reasons.append(f"matched alias term {term_value}")
    if concept_domain and memory_domain and concept_domain == memory_domain:
        reasons.append(f"same alias domain {concept_domain}")
    return reasons


def _memory_group_build_catalog(
    conn: sqlite3.Connection,
    args: dict[str, Any],
    warnings: list[dict[str, str]],
) -> dict[str, Any]:
    scope = _memory_group_base_rows(conn, args)
    rows = list(scope["rows"])
    row_map = dict(scope["row_map"])
    min_group_size = _safe_int(
        args.get("min_group_size"),
        2,
        minimum=2,
        maximum=1000,
    )
    query_text = normalize_optional_string(args.get("query"))
    include_related = parse_bool(args.get("include_related"), default=True)

    topic_index: dict[str, set[str]] = {}
    domain_index: dict[str, set[str]] = {}
    path_index: dict[str, set[str]] = {}
    normalized_hash_index: dict[str, set[str]] = {}
    shingle_index: dict[str, set[str]] = {}
    link_index: dict[str, set[str]] = {str(row.get("id")): set() for row in rows}

    for row in rows:
        memory_id = str(row.get("id", ""))
        for topic_name in row.get("topics", []):
            topic_value = str(topic_name)
            if topic_value:
                topic_index.setdefault(topic_value, set()).add(memory_id)
        domain_value = normalize_optional_string(row.get("domain"))
        if domain_value:
            domain_index.setdefault(domain_value, set()).add(memory_id)
        for file_info in row.get("touched_files", []):
            path_value = normalize_optional_string(file_info.get("path"))
            if not path_value:
                continue
            path_index.setdefault(path_value, set()).add(memory_id)
            parent = _memory_group_path_parent(path_value)
            if parent:
                path_index.setdefault(parent, set()).add(memory_id)
        normalized_hash = normalize_optional_string(row.get("normalized_hash"))
        if normalized_hash:
            normalized_hash_index.setdefault(normalized_hash, set()).add(memory_id)
        for shingle in row.get("shingle_hashes", []):
            shingle_index.setdefault(str(shingle), set()).add(memory_id)
        for linked_id in row.get("linked_ids", []):
            if linked_id in row_map:
                link_index.setdefault(memory_id, set()).add(linked_id)
                link_index.setdefault(linked_id, set()).add(memory_id)

    discovered: list[dict[str, Any]] = []

    def register_group(
        *,
        group_id: str,
        group_type: str,
        label: str,
        core_ids: set[str],
        reasons: list[str],
        core_reason_map: dict[str, list[str]] | None = None,
        confidence_bonus: float = 0.0,
    ) -> None:
        reason_lookup = core_reason_map or {}
        if len(core_ids) < min_group_size:
            return
        sorted_core_ids = sorted(core_ids)
        core_rows = [row_map[memory_id] for memory_id in sorted_core_ids if memory_id in row_map]
        related_ids: list[str] = []
        related_reason_map: dict[str, list[str]] = {}
        if include_related:
            related_ids, related_reason_map = _memory_group_collect_related_ids(
                set(sorted_core_ids),
                row_map,
                topic_index,
                domain_index,
                path_index,
                normalized_hash_index,
                shingle_index,
                link_index,
            )
        full_tree_ids = set(sorted_core_ids) | set(related_ids)
        for memory_id in list(sorted(full_tree_ids)):
            for linked_id in sorted(link_index.get(memory_id, set())):
                if linked_id in row_map:
                    full_tree_ids.add(linked_id)
        total_rows = [row_map[memory_id] for memory_id in sorted(full_tree_ids) if memory_id in row_map]
        exportable_count = sum(1 for row in total_rows if str(row.get("kind", "")) in PACK_EXPORT_ALLOWED_KINDS)
        core_exportable_count = sum(1 for row in core_rows if str(row.get("kind", "")) in PACK_EXPORT_ALLOWED_KINDS)
        core_topics = sorted({str(topic) for row in core_rows for topic in row.get("topics", [])})[:10]
        related_topics = sorted(
            {
                str(topic)
                for memory_id in related_ids
                for topic in row_map.get(memory_id, {}).get("topics", [])
                if str(topic)
            }
        )[:10]
        domains = sorted(
            {str(row.get("domain")) for row in total_rows if normalize_optional_string(row.get("domain")) is not None}
        )[:10]
        kinds: dict[str, int] = {}
        namespaces: dict[str, int] = {}
        origins: dict[str, int] = {}
        touched_paths: dict[str, int] = {}
        for row in total_rows:
            kind_name = str(row.get("kind", ""))
            kinds[kind_name] = int(kinds.get(kind_name, 0) + 1)
            namespace_value = str(row.get("namespace", DEFAULT_MEMORY_NAMESPACE))
            namespaces[namespace_value] = int(namespaces.get(namespace_value, 0) + 1)
            origin_value = str(row.get("origin", DEFAULT_MEMORY_ORIGIN))
            origins[origin_value] = int(origins.get(origin_value, 0) + 1)
            for file_info in row.get("touched_files", []):
                path_value = normalize_optional_string(file_info.get("path"))
                if path_value:
                    touched_paths[path_value] = int(touched_paths.get(path_value, 0) + 1)
        sample_rows = core_rows[: max(1, MEMORY_GROUP_SAMPLE_PER_GROUP_DEFAULT)]
        recommended_scopes = ["core"]
        if related_ids:
            recommended_scopes.append("core_plus_related")
        if len(full_tree_ids) > len(core_ids):
            recommended_scopes.append("full_tree")
        if "full_tree" not in recommended_scopes:
            recommended_scopes.append("full_tree")
        confidence_value = _memory_group_confidence(
            core_rows=core_rows,
            related_count=len(related_ids),
            query=query_text,
            label=label,
            reasons=reasons,
        )
        confidence_value = min(0.99, max(0.0, float(confidence_value) + float(confidence_bonus)))
        discovered.append(
            {
                "group_id": group_id,
                "group_type": group_type,
                "label": label,
                "summary": _memory_group_summary_text(core_rows),
                "confidence": confidence_value,
                "memory_count": len(full_tree_ids),
                "exportable_memory_count": int(exportable_count),
                "core_memory_count": len(core_ids),
                "core_exportable_count": int(core_exportable_count),
                "related_memory_count": len(related_ids),
                "core_topics": core_topics,
                "related_topics": related_topics,
                "domains": domains,
                "kinds": kinds,
                "namespaces": namespaces,
                "origins": origins,
                "touched_paths": [
                    path_value
                    for path_value, _count in sorted(touched_paths.items(), key=lambda item: (-int(item[1]), item[0]))[:10]
                ],
                "sample_memory_ids": [str(row.get("id", "")) for row in sample_rows],
                "sample_titles": [str(row.get("title") or collapsed_preview_text(row.get("text"), 80)) for row in sample_rows],
                "recommended_scopes": recommended_scopes,
                "reasons": reasons,
                "_core_ids": sorted_core_ids,
                "_related_ids": related_ids,
                "_full_tree_ids": sorted(full_tree_ids),
                "_membership_reasons": {
                    memory_id: list(
                        reason_lookup.get(memory_id, [f"core membership via {group_type}:{label}"])
                    )
                    for memory_id in sorted_core_ids
                    if memory_id in row_map
                }
                | related_reason_map,
            }
        )

    for topic_name, ids in sorted(topic_index.items()):
        if _memory_group_is_mechanical_topic(topic_name):
            continue
        register_group(
            group_id=f"topic:{topic_name}",
            group_type="topic",
            label=_memory_group_slug_label(topic_name),
            core_ids=set(ids),
            reasons=[f"shared topic:{topic_name}"],
        )

    for domain_value, ids in sorted(domain_index.items()):
        register_group(
            group_id=f"domain:{domain_value}",
            group_type="domain",
            label=_memory_group_slug_label(domain_value),
            core_ids=set(ids),
            reasons=[f"shared domain:{domain_value}"],
        )

    for path_value, ids in sorted(path_index.items()):
        group_kind = "path" if "/" in path_value and "." in path_value.rsplit("/", 1)[-1] else "path"
        register_group(
            group_id=(f"path:{path_value}" if "." in path_value.rsplit("/", 1)[-1] else f"dir:{path_value}"),
            group_type=group_kind,
            label=_memory_group_slug_label(path_value),
            core_ids=set(ids),
            reasons=[f"shared path:{path_value}"],
        )

    alias_rows = _memory_group_alias_source_rows(conn)
    for concept in _memory_group_alias_concepts(alias_rows):
        concept_id = normalize_optional_string(concept.get("concept_id"))
        concept_label = normalize_optional_string(concept.get("canonical")) or normalize_optional_string(concept.get("label"))
        if not concept_id or not concept_label:
            continue
        concept_domain = normalize_optional_string(concept.get("domain"))
        concept_reasons = [f"alias concept:{concept_label}"]
        if concept_domain:
            concept_reasons.append(f"alias domain:{concept_domain}")
        core_ids: set[str] = set()
        core_reason_map: dict[str, list[str]] = {}
        confidence_bonus = 0.0
        for row in rows:
            memory_id = str(row.get("id", ""))
            if not memory_id:
                continue
            memory_domain = normalize_optional_string(row.get("domain"))
            if concept_domain and memory_domain not in {concept_domain, None}:
                continue
            alias_reasons = _memory_group_alias_memory_reasons(row, concept)
            if not alias_reasons:
                continue
            core_ids.add(memory_id)
            membership_reasons = [f"matched alias concept {concept_label}"] + alias_reasons
            core_reason_map[memory_id] = membership_reasons
            if any(reason.startswith("matched canonical term ") for reason in alias_reasons):
                confidence_bonus += 0.02
            if sum(1 for reason in alias_reasons if reason.startswith("matched alias term ")) >= 2:
                confidence_bonus += 0.01
            if concept_domain and memory_domain and concept_domain == memory_domain:
                confidence_bonus += 0.01
        register_group(
            group_id=f"alias:{concept_id}",
            group_type="alias",
            label=concept_label,
            core_ids=core_ids,
            reasons=concept_reasons,
            core_reason_map=core_reason_map,
            confidence_bonus=min(0.05, confidence_bonus),
        )

    seen_component_roots: set[str] = set()
    for memory_id in sorted(link_index):
        if memory_id in seen_component_roots:
            continue
        component: set[str] = set()
        stack = [memory_id]
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            for neighbor in sorted(link_index.get(current, set())):
                if neighbor not in component:
                    stack.append(neighbor)
        seen_component_roots.update(component)
        if len(component) < min_group_size:
            continue
        root_id = sorted(component)[0]
        root_row = row_map.get(root_id, {})
        label = str(root_row.get("title") or root_row.get("id") or "Linked Group")
        register_group(
            group_id=f"link:{root_id}",
            group_type="link",
            label=label,
            core_ids=component,
            reasons=["explicit linked_ids_json relationships"],
        )

    discovered.sort(
        key=lambda item: (
            -float(item.get("confidence", 0.0)),
            -int(item.get("exportable_memory_count", 0)),
            -int(item.get("memory_count", 0)),
            str(item.get("group_id", "")),
        )
    )
    return {
        "groups": discovered,
        "rows": rows,
        "row_map": row_map,
        "resolved_namespaces": scope["resolved_namespaces"],
        "resolved_origins": scope["resolved_origins"],
    }


def _memory_group_resolve_selection(
    catalog: dict[str, Any],
    group_id: str,
    scope: str,
) -> dict[str, Any]:
    chosen = next((group for group in catalog["groups"] if str(group.get("group_id", "")) == group_id), None)
    if chosen is None:
        raise PackSelectorError("memory_group_not_found", f"group {group_id} was not found")

    raw_ids = (
        list(chosen.get("_core_ids", []))
        if scope == "core"
        else list(chosen.get("_core_ids", [])) + list(chosen.get("_related_ids", []))
        if scope == "core_plus_related"
        else list(chosen.get("_full_tree_ids", []))
    )
    seen_ids: set[str] = set()
    ordered_ids: list[str] = []
    for memory_id in raw_ids:
        value = str(memory_id)
        if value and value not in seen_ids:
            seen_ids.add(value)
            ordered_ids.append(value)
    row_map = dict(catalog["row_map"])
    return {
        "group": chosen,
        "ordered_ids": ordered_ids,
        "row_map": row_map,
        "resolved_namespaces": list(catalog.get("resolved_namespaces") or []),
        "resolved_origins": catalog.get("resolved_origins"),
    }


def memory_group_discover(args: dict[str, Any]) -> dict[str, Any]:
    if store_backend() != "sqlite":
        return tool_error("memory_group_discover requires sqlite backend")

    try:
        limit_groups = _safe_int(
            args.get("limit_groups"),
            MEMORY_GROUP_DISCOVER_LIMIT_DEFAULT,
            minimum=1,
            maximum=MEMORY_GROUP_DISCOVER_LIMIT_MAX,
        )
        sample_per_group = _safe_int(
            args.get("sample_per_group"),
            MEMORY_GROUP_SAMPLE_PER_GROUP_DEFAULT,
            minimum=0,
            maximum=10,
        )
        include_samples = parse_bool(args.get("include_samples"), default=True)
        output_mode = normalize_optional_string(args.get("output_mode")) or "raw"
        catalog_for = normalize_optional_string(args.get("catalog_for"))
        include_raw_groups = parse_bool(args.get("include_raw_groups"), default=True)
    except ValueError as exc:
        return tool_error(str(exc))

    warnings: list[dict[str, str]] = []
    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            catalog = _memory_group_build_catalog(conn, args, warnings)
    except ValueError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")

    if output_mode == "catalog":
        options_source = list(catalog["groups"])
        options_source.sort(
            key=lambda item: (
                _memory_group_catalog_bucket(item)[0],
                -int(item.get("core_exportable_count", 0) or 0),
                -int(item.get("core_memory_count", 0) or 0),
                str(item.get("label", "") or str(item.get("group_id", ""))).lower(),
            )
        )
        options = [_memory_group_catalog_option(group) for group in options_source[:limit_groups]]
        structured: dict[str, Any] = {
            "action": "memory_group_discover",
            "status": "ok",
            "output_mode": "catalog",
            "catalog": {
                "total_groups": len(options_source),
                "shown_groups": len(options),
                "visible_rows_scanned": len(catalog["rows"]),
                "options": options,
            },
            "warnings": warnings,
        }
        if include_raw_groups:
            groups = []
            for raw_group in options_source[:limit_groups]:
                group = {
                    key: value
                    for key, value in raw_group.items()
                    if not str(key).startswith("_")
                }
                if not include_samples:
                    group["sample_memory_ids"] = []
                    group["sample_titles"] = []
                else:
                    group["sample_memory_ids"] = list(group.get("sample_memory_ids", []))[:sample_per_group]
                    group["sample_titles"] = list(group.get("sample_titles", []))[:sample_per_group]
                groups.append(group)
            structured["groups"] = groups
        if catalog_for == "export" and not options:
            warnings.append(
                {
                    "code": "no_export_groups",
                    "message": "No exportable groups were found in the current visible scope.",
                }
            )
        lines = [
            f"Discovered memory group catalog: {len(options)} shown / {len(options_source)} total",
            f"Visible rows scanned: {len(catalog['rows'])}",
        ]
        return text_result("\n".join(lines), structured)

    groups = []
    for raw_group in list(catalog["groups"])[:limit_groups]:
        group = {
            key: value
            for key, value in raw_group.items()
            if not str(key).startswith("_")
        }
        if not include_samples:
            group["sample_memory_ids"] = []
            group["sample_titles"] = []
        else:
            group["sample_memory_ids"] = list(group.get("sample_memory_ids", []))[:sample_per_group]
            group["sample_titles"] = list(group.get("sample_titles", []))[:sample_per_group]
        groups.append(group)

    structured = {
        "action": "memory_group_discover",
        "status": "ok",
        "output_mode": "raw",
        "groups": groups,
        "warnings": warnings,
    }
    lines = [
        f"Discovered memory groups: {len(groups)}",
        f"Visible rows scanned: {len(catalog['rows'])}",
    ]
    return text_result("\n".join(lines), structured)


def memory_group_preview(args: dict[str, Any]) -> dict[str, Any]:
    if store_backend() != "sqlite":
        return tool_error("memory_group_preview requires sqlite backend")

    group_id = normalize_optional_string(args.get("group_id"))
    if group_id is None:
        return tool_error_code("memory_group_not_found", "group_id is required")

    try:
        scope = normalize_choice(
            args.get("scope"),
            "scope",
            ("core", "core_plus_related", "full_tree"),
            default="core_plus_related",
            strict=True,
        ) or "core_plus_related"
        limit = _safe_int(
            args.get("limit"),
            MEMORY_GROUP_PREVIEW_LIMIT_DEFAULT,
            minimum=1,
            maximum=MEMORY_GROUP_PREVIEW_LIMIT_MAX,
        )
        include_samples = parse_bool(args.get("include_samples"), default=True)
        sample_per_kind = _safe_int(args.get("sample_per_kind"), 3, minimum=0, maximum=10)
        include_pack_readiness = parse_bool(args.get("include_pack_readiness"), default=True)
        include_redaction_summary = parse_bool(args.get("include_redaction_summary"), default=False)
        include_memory_ids = parse_bool(args.get("include_memory_ids"), default=True)
    except ValueError as exc:
        return tool_error(str(exc))

    warnings: list[dict[str, str]] = []
    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            catalog = _memory_group_build_catalog(conn, args, warnings)
            resolved = _memory_group_resolve_selection(catalog, group_id, scope)
    except PackSelectorError as exc:
        return tool_error_code(exc.code, exc.message)
    except ValueError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")

    chosen = dict(resolved["group"])
    ordered_ids = list(resolved["ordered_ids"])
    row_map = dict(resolved["row_map"])
    limited = bool(len(ordered_ids) > limit)
    selected_ids = list(ordered_ids[:limit])
    selected_rows = [row_map[memory_id] for memory_id in selected_ids if memory_id in row_map]

    by_kind: dict[str, int] = {}
    by_namespace: dict[str, int] = {}
    by_origin: dict[str, int] = {}
    by_topic: dict[str, int] = {}
    exportable_ids: list[str] = []
    non_exportable = 0
    for row in selected_rows:
        kind_name = str(row.get("kind", ""))
        by_kind[kind_name] = int(by_kind.get(kind_name, 0) + 1)
        namespace_value = str(row.get("namespace", DEFAULT_MEMORY_NAMESPACE))
        by_namespace[namespace_value] = int(by_namespace.get(namespace_value, 0) + 1)
        origin_value = str(row.get("origin", DEFAULT_MEMORY_ORIGIN))
        by_origin[origin_value] = int(by_origin.get(origin_value, 0) + 1)
        for topic_name in row.get("topics", []):
            by_topic[str(topic_name)] = int(by_topic.get(str(topic_name), 0) + 1)
        if kind_name in PACK_EXPORT_ALLOWED_KINDS:
            exportable_ids.append(str(row.get("id", "")))
        else:
            non_exportable += 1

    samples: dict[str, list[dict[str, Any]]] = {}
    if include_samples and sample_per_kind > 0:
        quotas: dict[str, int] = {}
        for row in selected_rows:
            kind_name = str(row.get("kind", ""))
            if quotas.get(kind_name) is None:
                quotas[kind_name] = sample_per_kind
            if quotas.get(kind_name, 0) <= 0:
                continue
            samples.setdefault(kind_name, []).append(
                {
                    "id": str(row.get("id", "")),
                    "title": normalize_optional_string(row.get("title")),
                    "preview": collapsed_preview_text(row.get("text"), 160),
                    "namespace": str(row.get("namespace", DEFAULT_MEMORY_NAMESPACE)),
                    "origin": str(row.get("origin", DEFAULT_MEMORY_ORIGIN)),
                }
            )
            quotas[kind_name] = int(quotas.get(kind_name, 0) - 1)

    membership_reasons = {
        memory_id: list(chosen.get("_membership_reasons", {}).get(memory_id, []))
        for memory_id in selected_ids
    }

    structured: dict[str, Any] = {
        "action": "memory_group_preview",
        "status": "ok",
        "group": {key: value for key, value in chosen.items() if not str(key).startswith("_")},
        "scope": scope,
        "selection": {
            "total_rows": len(ordered_ids),
            "limited": limited,
            "limit": limit,
            "memory_ids": selected_ids if include_memory_ids else [],
        },
        "counts": {
            "by_kind": by_kind,
            "by_topic": dict(sorted(by_topic.items(), key=lambda item: (-int(item[1]), item[0]))[:20]),
            "by_namespace": by_namespace,
            "by_origin": by_origin,
            "exportable": len(exportable_ids),
            "non_exportable": int(non_exportable),
        },
        "pack_readiness": {
            "can_export_default": bool(exportable_ids and non_exportable == 0),
            "exportable_kinds": list(PACK_EXPORT_ALLOWED_KINDS),
            "excluded_kinds": {kind: count for kind, count in by_kind.items() if kind not in PACK_EXPORT_ALLOWED_KINDS},
            "recommended_pack_selector": {
                "memory_ids": exportable_ids,
                "kinds": list(PACK_EXPORT_ALLOWED_KINDS),
            },
        }
        if include_pack_readiness
        else {},
        "samples": {kind: samples[kind] for kind in sorted(samples)},
        "membership_reasons": membership_reasons,
        "warnings": warnings,
    }
    if include_redaction_summary:
        total_matches = 0
        by_category: dict[str, int] = {}
        for row in selected_rows:
            redaction = _pack_row_redaction(row)
            total_matches += int(redaction["total_matches"])
            for category_name, count in dict(redaction["by_category"]).items():
                by_category[str(category_name)] = int(by_category.get(str(category_name), 0) + int(count))
        structured["redaction_summary"] = {
            "ruleset_version": BASELINE_REDACTION_RULESET_VERSION,
            "total_matches": int(total_matches),
            "by_category": by_category,
        }

    lines = [
        f"Memory group preview: {group_id}",
        f"Rows selected: {len(ordered_ids)} (limited={str(limited).lower()}, limit={limit})",
        f"Exportable rows: {len(exportable_ids)}",
    ]
    return text_result("\n".join(lines), structured)


def pack_export(args: dict[str, Any]) -> dict[str, Any]:
    if store_backend() != "sqlite":
        return tool_error("pack_export requires sqlite backend")

    sign_pack = parse_bool(args.get("sign_pack"), default=False)
    allow_unsigned = parse_bool(args.get("allow_unsigned"), default=False)
    if not sign_pack and not allow_unsigned:
        return tool_error(PACK_ALLOW_UNSIGNED_ERROR)

    signer_id: str | None = None
    signing_secret: str | None = None
    signature_algorithm = normalize_optional_string(args.get("signature_algorithm")) or PACK_SIGNATURE_ALGORITHM_HMAC_LOCAL
    signature_algorithm = signature_algorithm.strip().lower()
    secret_fingerprint: str | None = None
    try:
        sanitized_pack_name = _sanitize_pack_name(args.get("pack_name"))
        parsed = _pack_parse_common_filters(args)
        _pack_validate_export_kinds(args, list(parsed["kinds"]))
        allow_limited_export = parse_bool(args.get("allow_limited_export"), default=False)
        # Preflight conflict validation before any export-side effects.
        resolve_namespace_origin_filters(args, None)
        if sign_pack:
            signer_id = _normalize_signer_id(args.get("signer_id"))
            if signature_algorithm != PACK_SIGNATURE_ALGORITHM_HMAC_LOCAL:
                raise ValueError(f"unsupported signature_algorithm: {signature_algorithm}")
            signing_secret = _validate_secret_length(args.get("signing_secret"), field_name="signing_secret")
            secret_fingerprint = _secret_fingerprint(signing_secret)
    except PackSelectorError as exc:
        return tool_error_code(exc.code, exc.message)
    except ValueError as exc:
        message = str(exc)
        if "signing_secret" in message and "at least" in message:
            return tool_error_code("secret_too_short", message)
        return tool_error(message)

    warnings: list[dict[str, str]] = []
    signer_registry_fingerprint: str | None = None
    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            selection = _pack_selection_context(conn, args, parsed, warnings)
            total_rows = int(selection["total_rows"])
            limit = int(selection["limit"])
            limited = bool(total_rows > limit)
            if total_rows <= 0:
                raise ValueError("selection returned zero rows; nothing to export")
            if limited and not allow_limited_export:
                raise PackSelectorError(
                    "limited_export_requires_confirmation",
                    "selection is limited; increase limit or pass allow_limited_export=true to export only the limited selected row set",
                )
            selected_rows = list(selection["selected_rows"])
            for row in selected_rows:
                kind_name = str(row.get("kind", ""))
                if kind_name not in PACK_EXPORT_ALLOWED_KINDS:
                    if kind_name in PACK_PREVIEW_POLICY_WARNING_KINDS:
                        raise ValueError(PACK_KIND_PREVIEW_ERROR_TEMPLATE.format(kind=kind_name))
                    raise ValueError(f"kind '{kind_name}' is not exportable in v1 policy")
            selected_row_ids = [str(row.get("id", "")) for row in selected_rows]
            topics_by_memory_id = _pack_topics_by_memory_id(conn, selected_row_ids)
            files_by_memory_id = _pack_files_by_memory_id(conn, selected_row_ids)
            if sign_pack and signer_id is not None:
                signer_row = conn.execute(
                    "SELECT secret_fingerprint FROM trusted_signers WHERE signer_id = ?",
                    (signer_id,),
                ).fetchone()
                if signer_row is not None:
                    signer_registry_fingerprint = normalize_optional_string(
                        signer_row["secret_fingerprint"] if isinstance(signer_row, sqlite3.Row) else signer_row[0]
                    )
    except PackSelectorError as exc:
        return tool_error_code(exc.code, exc.message)
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")

    if bool(total_rows > limit and allow_limited_export):
        warnings.append(
            {
                "code": "limited_export",
                "message": "Only the limited selected row set was exported.",
            }
        )
    if sign_pack:
        warnings.append(_pack_local_hmac_warning())
        if signer_registry_fingerprint and secret_fingerprint and signer_registry_fingerprint != secret_fingerprint:
            warnings.append(
                {
                    "code": "secret_fingerprint_mismatch_possible",
                    "message": "Provided signing_secret fingerprint differs from the local trusted_signers fingerprint for signer_id.",
                }
            )
    else:
        warnings.append(
            {
                "code": "unsigned_development_pack",
                "message": "This pack is unsigned because signing was not requested for this export.",
            }
        )
    warnings.append(_pack_baseline_warning())

    pack_id = _pack_make_pack_id()
    try:
        output_dir = _pack_output_dir(args.get("output_dir"))
        final_zip_path = _pack_output_path(output_dir, sanitized_pack_name, pack_id)
    except ValueError as exc:
        return tool_error(str(exc))

    exported_rows: list[dict[str, Any]] = []
    row_redaction_entries: list[dict[str, Any]] = []
    by_kind: dict[str, int] = {}
    by_namespace: dict[str, int] = {}
    by_origin: dict[str, int] = {}
    by_topic: dict[str, int] = {}
    by_category: dict[str, int] = {}
    rows_by_category: dict[str, int] = {}
    total_matches = 0
    affected_rows = 0

    path_to_memory_ids: dict[str, set[str]] = {}
    path_to_shas: dict[str, set[str]] = {}
    context_counter = 0
    hippocampus_counter = 0

    for row in selected_rows:
        kind_name = str(row.get("kind", ""))
        if kind_name == "context_block":
            context_counter += 1
            row_id_in_pack = f"ctx_{context_counter:03d}"
        elif kind_name == "hippocampus_entry":
            hippocampus_counter += 1
            row_id_in_pack = f"hip_{hippocampus_counter:03d}"
        else:
            # Defensively fail here even though this is already validated above.
            if kind_name in PACK_PREVIEW_POLICY_WARNING_KINDS:
                return tool_error(PACK_KIND_PREVIEW_ERROR_TEMPLATE.format(kind=kind_name))
            return tool_error(f"kind '{kind_name}' is not exportable in v1 policy")

        source_id = str(row.get("id", ""))
        row_topics = sorted(topics_by_memory_id.get(source_id, []))
        touched_files = list(files_by_memory_id.get(source_id, []))
        row_redaction = _pack_row_redaction(row)
        row_categories = list(row_redaction["categories"])
        row_match_count = int(row_redaction["total_matches"])
        if row_match_count > 0:
            affected_rows += 1
            row_redaction_entries.append(
                {
                    "row_id_in_pack": row_id_in_pack,
                    "categories": row_categories,
                    "match_count": row_match_count,
                }
            )
        total_matches += row_match_count
        row_counts = dict(row_redaction["by_category"])
        for category_name in PACK_REDACTION_RULE_ORDER:
            category_hits = int(row_counts.get(category_name, 0))
            if category_hits <= 0:
                continue
            by_category[category_name] = int(by_category.get(category_name, 0) + category_hits)
            rows_by_category[category_name] = int(rows_by_category.get(category_name, 0) + 1)

        namespace_value = str(row.get("namespace", DEFAULT_MEMORY_NAMESPACE))
        origin_value = str(row.get("origin", DEFAULT_MEMORY_ORIGIN))
        by_kind[kind_name] = int(by_kind.get(kind_name, 0) + 1)
        by_namespace[namespace_value] = int(by_namespace.get(namespace_value, 0) + 1)
        by_origin[origin_value] = int(by_origin.get(origin_value, 0) + 1)
        for topic_value in row_topics:
            by_topic[topic_value] = int(by_topic.get(topic_value, 0) + 1)
        for file_info in touched_files:
            path_value = str(file_info.get("path", ""))
            sha_value = str(file_info.get("file_sha", ""))
            if not path_value:
                continue
            path_to_memory_ids.setdefault(path_value, set()).add(row_id_in_pack)
            if sha_value:
                path_to_shas.setdefault(path_value, set()).add(sha_value)

        exported_rows.append(
            {
                "row_id_in_pack": row_id_in_pack,
                "kind": kind_name,
                "namespace_at_export": namespace_value,
                "origin_at_export": origin_value,
                "text_fields": dict(row_redaction["text_fields"]),
                "topics": row_topics,
                "created_at_in_source": normalize_optional_string(row.get("created_at")),
                "git_sha_at_write": normalize_optional_string(row.get("git_sha")),
                "git_branch_at_write": normalize_optional_string(row.get("git_branch")),
                "git_dirty_at_write": normalize_git_dirty(row.get("git_dirty")),
                "touched_files": touched_files,
                "import_freshness_at_export": normalize_optional_string(row.get("import_freshness")),
                "redaction_applied": True,
            }
        )

    exported_rows_count = len(exported_rows)
    limited = bool(total_rows > limit)
    topic_summary = [
        {"topic": topic_name, "row_count": int(count)}
        for topic_name, count in sorted(by_topic.items(), key=lambda item: (-int(item[1]), item[0]))
    ]
    file_fingerprints = [
        {
            "path": path_value,
            "memory_count": len(path_to_memory_ids.get(path_value, set())),
            "file_shas": sorted(path_to_shas.get(path_value, set())),
        }
        for path_value in sorted(
            path_to_memory_ids,
            key=lambda path_item: (-len(path_to_memory_ids.get(path_item, set())), path_item),
        )
    ]
    referenced_file_count = len(path_to_memory_ids)
    exported_at = now_iso()
    source_namespaces = sorted({str(row.get("namespace_at_export", DEFAULT_MEMORY_NAMESPACE)) for row in exported_rows})
    source_origins = sorted({str(row.get("origin_at_export", DEFAULT_MEMORY_ORIGIN)) for row in exported_rows})
    filters_payload = {
        "topics": list(selection["topics"]),
        "kinds": list(selection["kinds"]),
        "memory_ids": [],
        "group_id": selection["group_id"],
        "scope": selection["scope"],
        "namespaces": list(selection["resolved_namespaces"]),
        "origins": list(selection["resolved_origins"]) if selection["resolved_origins"] is not None else [],
        "created_after": selection["created_after"],
        "created_before": selection["created_before"],
        "touched_paths": list(selection["touched_paths"]),
    }

    redactions_payload = {
        "ruleset_version": BASELINE_REDACTION_RULESET_VERSION,
        "rules_applied": list(PACK_REDACTION_RULE_ORDER),
        "total_matches": int(total_matches),
        "affected_rows": int(affected_rows),
        "by_category": by_category,
        "rows_by_category": rows_by_category,
        "by_pack_row": row_redaction_entries,
    }
    member_payloads: dict[str, Any] = {
        "content/memories.jsonl": exported_rows,
        "content/topics.json": {"topics": topic_summary},
        "content/file_fingerprints.json": {"files": file_fingerprints},
        "provenance/origin.json": {
            "source": "mnemo",
            "mnemo_version": SERVER_VERSION,
            "exported_at": exported_at,
            "selection_filters": filters_payload,
            "source_namespaces": source_namespaces,
            "source_origins": source_origins,
        },
        "provenance/redactions.json": redactions_payload,
    }
    member_bytes: dict[str, bytes] = {}
    member_bytes["content/memories.jsonl"] = _pack_jsonl_bytes(member_payloads["content/memories.jsonl"])
    for member_name in (
        "content/topics.json",
        "content/file_fingerprints.json",
        "provenance/origin.json",
        "provenance/redactions.json",
    ):
        member_bytes[member_name] = _pack_json_bytes(member_payloads[member_name])
    content_hash = _pack_content_hash(member_bytes)

    manifest: dict[str, Any] = {
        "pack_schema_version": 1,
        "pack_id": pack_id,
        "pack_name": sanitized_pack_name,
        "created_at": exported_at,
        "mnemo_version": SERVER_VERSION,
        "signed": bool(sign_pack),
        "unsigned_reason": None if sign_pack else PACK_UNSIGNED_REASON_OPERATOR,
        "content_hash": content_hash,
        "redaction_ruleset_version": BASELINE_REDACTION_RULESET_VERSION,
        "redaction_rules_applied": list(PACK_REDACTION_RULE_ORDER),
        "selection": {
            "filters": filters_payload,
            "total_rows": int(total_rows),
            "exported_rows": int(exported_rows_count),
            "limited": limited,
            "limit": int(limit),
        },
        "counts": {
            "by_kind": by_kind,
            "by_namespace": by_namespace,
            "by_origin": by_origin,
            "by_topic": by_topic,
        },
        "files": {
            "referenced_file_count": int(referenced_file_count),
        },
        "redaction": {
            "total_matches": int(total_matches),
            "affected_rows": int(affected_rows),
            "by_category": by_category,
            "rows_by_category": rows_by_category,
        },
    }
    if sign_pack:
        assert signer_id is not None
        assert secret_fingerprint is not None
        manifest["signature"] = {
            "signature_algorithm": signature_algorithm,
            "signature_payload_version": PACK_SIGNATURE_PAYLOAD_VERSION_V1,
            "signer_id": signer_id,
            "secret_fingerprint": secret_fingerprint,
            "signature_member": PACK_SIGNATURE_MEMBER,
        }
        assert signing_secret is not None
        signature_value = _pack_sign_hmac_v1(manifest, signing_secret)
        member_bytes[PACK_SIGNATURE_MEMBER] = _pack_json_bytes(
            {
                "signature_schema_version": PACK_SIGNATURE_SCHEMA_VERSION,
                "signature_algorithm": signature_algorithm,
                "signature_payload_version": PACK_SIGNATURE_PAYLOAD_VERSION_V1,
                "signer_id": signer_id,
                "secret_fingerprint": secret_fingerprint,
                "signed_at": exported_at,
                "signature_value": signature_value,
            }
        )
    member_bytes["manifest.json"] = _pack_json_bytes(manifest)

    temp_zip_path = Path(
        tempfile.NamedTemporaryFile(dir=str(output_dir), delete=False, suffix=f"{PACK_FILE_EXTENSION}.tmp").name
    )
    replaced = False
    try:
        with zipfile.ZipFile(temp_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for member_name in PACK_REQUIRED_MEMBERS:
                archive.writestr(member_name, member_bytes[member_name])
            if sign_pack:
                archive.writestr(PACK_SIGNATURE_MEMBER, member_bytes[PACK_SIGNATURE_MEMBER])
        validated_manifest = _pack_validate_zip(temp_zip_path)
        if str(validated_manifest.get("pack_id", "")) != pack_id:
            raise ValueError("validated manifest pack_id mismatch")

        conn = _sqlite_connect()
        try:
            _sqlite_ensure_schema(conn)
            conn.commit()
            conn.execute(
                """
                INSERT INTO exported_packs(
                    pack_id, pack_name, exported_at, row_count, redaction_count, signed, manifest_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pack_id,
                    sanitized_pack_name,
                    exported_at,
                    int(exported_rows_count),
                    int(total_matches),
                    1 if sign_pack else 0,
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                ),
            )
            os.replace(temp_zip_path, final_zip_path)
            replaced = True
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            if replaced:
                try:
                    final_zip_path.unlink()
                except Exception as cleanup_exc:
                    return tool_error(
                        f"{type(exc).__name__}: {exc} (post-replace cleanup failed: {cleanup_exc})"
                    )
            else:
                try:
                    if temp_zip_path.exists():
                        temp_zip_path.unlink()
                except Exception:
                    pass
            return tool_error(f"{type(exc).__name__}: {exc}")
        finally:
            conn.close()
    except Exception as exc:
        try:
            if not replaced and temp_zip_path.exists():
                temp_zip_path.unlink()
        except Exception:
            pass
        return tool_error(f"{type(exc).__name__}: {exc}")
    finally:
        if not replaced:
            try:
                if temp_zip_path.exists():
                    temp_zip_path.unlink()
            except Exception:
                pass

    structured = {
        "action": "pack_export",
        "status": "ok",
        "pack_id": pack_id,
        "pack_name": sanitized_pack_name,
        "output_path": str(final_zip_path),
        "signed": bool(sign_pack),
        "unsigned_reason": None if sign_pack else PACK_UNSIGNED_REASON_OPERATOR,
        "content_hash": {
            "algorithm": "sha256",
            "value": str(content_hash["value"]),
        },
        "selection": {
            "total_rows": int(total_rows),
            "exported_rows": int(exported_rows_count),
            "limited": limited,
            "limit": int(limit),
        },
        "redaction": {
            "ruleset_version": BASELINE_REDACTION_RULESET_VERSION,
            "total_matches": int(total_matches),
            "affected_rows": int(affected_rows),
            "by_category": by_category,
            "rows_by_category": rows_by_category,
        },
        "contents": {
            "manifest": "manifest.json",
            "memories": "content/memories.jsonl",
            "topics": "content/topics.json",
            "file_fingerprints": "content/file_fingerprints.json",
            "origin": "provenance/origin.json",
            "redactions": "provenance/redactions.json",
            "signature": PACK_SIGNATURE_MEMBER if sign_pack else None,
        },
        "warnings": warnings,
    }
    warning_codes = [str(item.get("code", "")) for item in warnings if isinstance(item, dict)]
    lines = [
        f"Pack export written: {final_zip_path}",
        f"Rows: {exported_rows_count} (selected={total_rows}, limited={str(limited).lower()}, limit={limit})",
        f"Content hash: {content_hash['value']}",
        f"Redaction matches: {total_matches}",
        f"Warnings: {', '.join(code for code in warning_codes if code) if warning_codes else 'none'}",
    ]
    return text_result("\n".join(lines), structured)


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


def _sqlite_events_fts_flag() -> bool:
    if store_backend() != "sqlite":
        return False
    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            _sqlite_bootstrap_if_needed(conn)
            meta_value = _sqlite_get_meta(conn, "events_fts_available")
            if meta_value is not None:
                return str(meta_value).strip() == "1"
            available = _sqlite_events_fts_available(conn)
            _sqlite_set_meta(conn, "events_fts_available", "1" if available else "0")
            if available and _sqlite_get_meta(conn, "events_fts_index_built_at") is None:
                _sqlite_rebuild_events_fts_index(conn)
            return available
    except Exception:
        return False


def _idf_profile_to_dict(profile: Any) -> dict[str, Any]:
    if profile is None:
        return {}
    if hasattr(profile, "to_dict"):
        payload = profile.to_dict()
        if isinstance(payload, dict):
            return dict(payload)
    if isinstance(profile, dict):
        return dict(profile)
    return {}


def _idf_remaining(profile_info: dict[str, Any]) -> dict[str, int]:
    return {
        "documents": max(0, int(profile_info.get("min_documents", 0)) - int(profile_info.get("doc_count", 0))),
        "unique_terms": max(0, int(profile_info.get("min_unique_terms", 0)) - int(profile_info.get("unique_terms", 0))),
        "total_tokens": max(0, int(profile_info.get("min_total_tokens", 0)) - int(profile_info.get("total_tokens", 0))),
    }


def _idf_profile_diagnostic(
    *,
    scope: str,
    name: str,
    thresholds: dict[str, int],
    status: str,
    active: bool,
    doc_count: int,
    unique_terms: int,
    total_tokens: int,
    corpus_signature: str | None = None,
    updated_at: str | None = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    info = {
        "scope": scope,
        "name": name,
        "status": status,
        "active": bool(active),
        "doc_count": max(0, int(doc_count)),
        "unique_terms": max(0, int(unique_terms)),
        "total_tokens": max(0, int(total_tokens)),
        "min_documents": max(1, int(thresholds.get("min_documents", 1))),
        "min_unique_terms": max(1, int(thresholds.get("min_unique_terms", 1))),
        "min_total_tokens": max(1, int(thresholds.get("min_total_tokens", 1))),
        "profile_version": IDF_PROFILE_VERSION,
        "corpus_signature": corpus_signature,
        "updated_at": updated_at,
        "profile": profile,
    }
    info["remaining"] = _idf_remaining(info)
    return info


def _idf_corpus_records(domain: str | None = None) -> list[dict[str, Any]]:
    min_text_tokens = idf_min_text_tokens()
    wanted_domain = normalize_optional_string(domain)
    records: list[dict[str, Any]] = []
    if store_backend() == "sqlite":
        try:
            with _sqlite_session() as conn:
                _sqlite_ensure_schema(conn)
                _sqlite_bootstrap_if_needed(conn)
                query = """
                    SELECT id, text, domain, token_count, unique_token_count, content_hash, normalized_hash,
                           signature_updated_at, updated_at, created_at
                    FROM memories
                    WHERE deleted = 0
                      AND (superseded_by IS NULL OR superseded_by = '')
                      AND text IS NOT NULL
                      AND TRIM(text) != ''
                """
                params: list[Any] = []
                if wanted_domain is not None:
                    query += " AND domain = ?"
                    params.append(wanted_domain)
                rows = conn.execute(query, params).fetchall()
            for row in rows:
                text_value = str(row["text"] or "").strip()
                if not text_value:
                    continue
                token_count = int(row["token_count"] or 0)
                if token_count <= 0:
                    token_count = len(_normalize_for_signature(text_value))
                if token_count < min_text_tokens:
                    continue
                unique_terms = int(row["unique_token_count"] or 0)
                if unique_terms <= 0:
                    unique_terms = len(set(_normalize_for_signature(text_value)))
                records.append(
                    {
                        "id": str(row["id"] or ""),
                        "text": text_value,
                        "domain": normalize_optional_string(row["domain"]),
                        "token_count": token_count,
                        "unique_token_count": unique_terms,
                        "content_hash": normalize_optional_string(row["content_hash"]),
                        "normalized_hash": normalize_optional_string(row["normalized_hash"]),
                        "signature_updated_at": normalize_optional_string(row["signature_updated_at"]),
                        "updated_at": normalize_optional_string(row["updated_at"]),
                        "created_at": normalize_optional_string(row["created_at"]),
                    }
                )
            return records
        except Exception:
            return []

    try:
        store = load_store()
    except Exception:
        return []
    for memory in store.get("memories", []):
        if not isinstance(memory, dict) or not is_active(memory):
            continue
        text_value = str(memory.get("text", "")).strip()
        if not text_value:
            continue
        mem_domain = normalize_optional_string(memory.get("domain"))
        if wanted_domain is not None and mem_domain != wanted_domain:
            continue
        token_count = int(memory.get("token_count") or 0)
        if token_count <= 0:
            token_count = len(_normalize_for_signature(text_value))
        if token_count < min_text_tokens:
            continue
        unique_terms = int(memory.get("unique_token_count") or 0)
        if unique_terms <= 0:
            unique_terms = len(set(_normalize_for_signature(text_value)))
        records.append(
            {
                "id": str(memory.get("id") or ""),
                "text": text_value,
                "domain": mem_domain,
                "token_count": token_count,
                "unique_token_count": unique_terms,
                "content_hash": normalize_optional_string(memory.get("content_hash")),
                "normalized_hash": normalize_optional_string(memory.get("normalized_hash")),
                "signature_updated_at": normalize_optional_string(memory.get("signature_updated_at")),
                "updated_at": normalize_optional_string(memory.get("updated_at")),
                "created_at": normalize_optional_string(memory.get("created_at")),
            }
        )
    return records


def _idf_corpus_signature(records: list[dict[str, Any]]) -> str:
    digest = hashlib.sha1()
    digest.update(str(IDF_PROFILE_VERSION).encode("ascii"))
    for record in sorted(records, key=lambda item: (str(item.get("id") or ""), str(item.get("created_at") or ""))):
        payload = {
            "id": str(record.get("id") or ""),
            "domain": str(record.get("domain") or ""),
            "token_count": int(record.get("token_count") or 0),
            "unique_token_count": int(record.get("unique_token_count") or 0),
            "content_hash": str(record.get("content_hash") or ""),
            "normalized_hash": str(record.get("normalized_hash") or ""),
            "signature_updated_at": str(record.get("signature_updated_at") or ""),
            "updated_at": str(record.get("updated_at") or ""),
        }
        digest.update(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return digest.hexdigest()


def _idf_quick_stats(domain: str | None = None) -> dict[str, int]:
    min_text_tokens = idf_min_text_tokens()
    wanted_domain = normalize_optional_string(domain)
    if store_backend() == "sqlite":
        try:
            with _sqlite_session() as conn:
                _sqlite_ensure_schema(conn)
                _sqlite_bootstrap_if_needed(conn)
                query = """
                    SELECT COUNT(*) AS doc_count, COALESCE(SUM(token_count), 0) AS total_tokens
                    FROM memories
                    WHERE deleted = 0
                      AND (superseded_by IS NULL OR superseded_by = '')
                      AND text IS NOT NULL
                      AND TRIM(text) != ''
                      AND COALESCE(token_count, 0) >= ?
                """
                params: list[Any] = [min_text_tokens]
                if wanted_domain is not None:
                    query += " AND domain = ?"
                    params.append(wanted_domain)
                row = conn.execute(query, params).fetchone()
            return {
                "doc_count": int(row["doc_count"] if row is not None else 0),
                "total_tokens": int(row["total_tokens"] if row is not None else 0),
            }
        except Exception:
            return {"doc_count": 0, "total_tokens": 0}

    try:
        records = _idf_corpus_records(domain=wanted_domain)
    except Exception:
        records = []
    return {
        "doc_count": len(records),
        "total_tokens": int(sum(int(record.get("token_count") or 0) for record in records)),
    }


def _idf_domain_quick_stats() -> dict[str, dict[str, int]]:
    min_text_tokens = idf_min_text_tokens()
    out: dict[str, dict[str, int]] = {}
    if store_backend() == "sqlite":
        try:
            with _sqlite_session() as conn:
                _sqlite_ensure_schema(conn)
                _sqlite_bootstrap_if_needed(conn)
                rows = conn.execute(
                    """
                    SELECT domain, COUNT(*) AS doc_count, COALESCE(SUM(token_count), 0) AS total_tokens
                    FROM memories
                    WHERE deleted = 0
                      AND (superseded_by IS NULL OR superseded_by = '')
                      AND text IS NOT NULL
                      AND TRIM(text) != ''
                      AND COALESCE(token_count, 0) >= ?
                      AND domain IS NOT NULL
                      AND TRIM(domain) != ''
                    GROUP BY domain
                    """,
                    (min_text_tokens,),
                ).fetchall()
            for row in rows:
                domain_name = normalize_optional_string(row["domain"])
                if not domain_name:
                    continue
                out[domain_name] = {
                    "doc_count": int(row["doc_count"] or 0),
                    "total_tokens": int(row["total_tokens"] or 0),
                }
            return out
        except Exception:
            return {}

    for record in _idf_corpus_records():
        domain_name = normalize_optional_string(record.get("domain"))
        if not domain_name:
            continue
        entry = out.setdefault(domain_name, {"doc_count": 0, "total_tokens": 0})
        entry["doc_count"] += 1
        entry["total_tokens"] += int(record.get("token_count") or 0)
    return out


def _load_cached_idf_profile(scope: str, name: str) -> dict[str, Any] | None:
    if store_backend() != "sqlite":
        return None
    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            _sqlite_bootstrap_if_needed(conn)
            row = conn.execute(
                """
                SELECT scope, name, profile_version, status, active, doc_count, unique_terms, total_tokens,
                       min_documents, min_unique_terms, min_total_tokens, corpus_signature, profile_json, updated_at
                FROM idf_profiles
                WHERE scope = ? AND name = ? AND profile_version = ?
                """,
                (scope, name, IDF_PROFILE_VERSION),
            ).fetchone()
        if row is None:
            return None
        profile_json = normalize_optional_string(row["profile_json"])
        profile_data: dict[str, Any] | None = None
        if profile_json:
            try:
                parsed = json.loads(profile_json)
                if isinstance(parsed, dict):
                    profile_data = parsed
            except Exception:
                profile_data = None
        return {
            "scope": str(row["scope"] or scope),
            "name": str(row["name"] or name),
            "profile_version": int(row["profile_version"] or IDF_PROFILE_VERSION),
            "status": str(row["status"] or "cold"),
            "active": bool(int(row["active"] or 0)),
            "doc_count": int(row["doc_count"] or 0),
            "unique_terms": int(row["unique_terms"] or 0),
            "total_tokens": int(row["total_tokens"] or 0),
            "min_documents": int(row["min_documents"] or 0),
            "min_unique_terms": int(row["min_unique_terms"] or 0),
            "min_total_tokens": int(row["min_total_tokens"] or 0),
            "corpus_signature": normalize_optional_string(row["corpus_signature"]),
            "profile": profile_data,
            "updated_at": normalize_optional_string(row["updated_at"]),
        }
    except Exception:
        return None


def _save_idf_profile(
    scope: str,
    name: str,
    profile: dict[str, Any],
    corpus_signature: str,
    active: bool,
) -> None:
    if store_backend() != "sqlite":
        return
    payload = _idf_profile_to_dict(profile)
    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            _sqlite_bootstrap_if_needed(conn)
            conn.execute(
                """
                INSERT INTO idf_profiles(
                    scope, name, profile_version, status, active,
                    doc_count, unique_terms, total_tokens,
                    min_documents, min_unique_terms, min_total_tokens,
                    corpus_signature, profile_json, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, name, profile_version) DO UPDATE SET
                    status = excluded.status,
                    active = excluded.active,
                    doc_count = excluded.doc_count,
                    unique_terms = excluded.unique_terms,
                    total_tokens = excluded.total_tokens,
                    min_documents = excluded.min_documents,
                    min_unique_terms = excluded.min_unique_terms,
                    min_total_tokens = excluded.min_total_tokens,
                    corpus_signature = excluded.corpus_signature,
                    profile_json = excluded.profile_json,
                    updated_at = excluded.updated_at
                """,
                (
                    scope,
                    name,
                    IDF_PROFILE_VERSION,
                    str(payload.get("status", "cold")),
                    1 if active else 0,
                    int(payload.get("doc_count", 0) or 0),
                    int(payload.get("unique_terms", 0) or 0),
                    int(payload.get("total_tokens", 0) or 0),
                    int(payload.get("min_documents", 0) or 0),
                    int(payload.get("min_unique_terms", 0) or 0),
                    int(payload.get("min_total_tokens", 0) or 0),
                    corpus_signature,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    now_iso(),
                ),
            )
    except Exception:
        return


def _build_idf_profile_if_ready(
    scope: str,
    name: str,
    records: list[dict[str, Any]],
    thresholds: dict[str, int],
    *,
    force: bool = False,
) -> dict[str, Any]:
    salience, reason = load_optional_agent_salience()
    builder = getattr(salience, "build_idf_profile", None) if salience is not None else None
    if not callable(builder):
        return _idf_profile_diagnostic(
            scope=scope,
            name=name,
            thresholds=thresholds,
            status="unavailable",
            active=False,
            doc_count=0,
            unique_terms=0,
            total_tokens=0,
            profile={"reason": reason} if reason else None,
        )
    documents = [str(record.get("text", "")) for record in records]
    domain_value = None if scope == "project" else name
    built = builder(
        documents,
        domain=domain_value,
        min_documents=int(thresholds.get("min_documents", 0)),
        min_unique_terms=int(thresholds.get("min_unique_terms", 0)),
        min_total_tokens=int(thresholds.get("min_total_tokens", 0)),
    )
    payload = _idf_profile_to_dict(built)
    status = str(payload.get("status", "cold"))
    active = bool(payload.get("ready", False))
    if force and documents:
        status = "ready"
        active = True
        payload["status"] = "ready"
        payload["ready"] = True
    return _idf_profile_diagnostic(
        scope=scope,
        name=name,
        thresholds=thresholds,
        status=status,
        active=active,
        doc_count=int(payload.get("doc_count", len(documents)) or 0),
        unique_terms=int(payload.get("unique_terms", 0) or 0),
        total_tokens=int(payload.get("total_tokens", 0) or 0),
        profile=payload,
    )


def _idf_public_view(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    out.pop("profile", None)
    return out


def _ensure_idf_profiles(trigger: str = "doctor") -> dict[str, Any]:
    mode = idf_mode()
    force = mode == "force"
    project_thresholds = idf_thresholds("project")
    domain_thresholds = idf_thresholds("domain")
    quick_project = _idf_quick_stats()
    project_name = "default"
    state: dict[str, Any] = {
        "mode": mode,
        "available": False,
        "project": _idf_profile_diagnostic(
            scope="project",
            name=project_name,
            thresholds=project_thresholds,
            status="cold",
            active=False,
            doc_count=int(quick_project.get("doc_count", 0)),
            unique_terms=0,
            total_tokens=int(quick_project.get("total_tokens", 0)),
        ),
        "domains": {},
        "warnings": [],
        "recommendations": [],
    }

    salience, reason = load_optional_agent_salience()
    idf_available = salience is not None and callable(getattr(salience, "build_idf_profile", None))
    state["available"] = bool(idf_available)
    wants_full_refresh = trigger in {"doctor", "salience_check", "maintenance"}

    if mode == "off":
        cached_project = _load_cached_idf_profile("project", project_name)
        if cached_project is not None:
            state["project"] = _idf_profile_diagnostic(
                scope="project",
                name=project_name,
                thresholds=project_thresholds,
                status="disabled",
                active=False,
                doc_count=int(cached_project.get("doc_count", 0)),
                unique_terms=int(cached_project.get("unique_terms", 0)),
                total_tokens=int(cached_project.get("total_tokens", 0)),
                corpus_signature=normalize_optional_string(cached_project.get("corpus_signature")),
                updated_at=normalize_optional_string(cached_project.get("updated_at")),
                profile=cached_project.get("profile"),
            )
        else:
            state["project"] = _idf_profile_diagnostic(
                scope="project",
                name=project_name,
                thresholds=project_thresholds,
                status="disabled",
                active=False,
                doc_count=int(quick_project.get("doc_count", 0)),
                unique_terms=0,
                total_tokens=int(quick_project.get("total_tokens", 0)),
            )
        for domain_name, quick in sorted(_idf_domain_quick_stats().items()):
            cached_domain = _load_cached_idf_profile("domain", domain_name)
            state["domains"][domain_name] = _idf_profile_diagnostic(
                scope="domain",
                name=domain_name,
                thresholds=domain_thresholds,
                status="disabled",
                active=False,
                doc_count=int((cached_domain or quick).get("doc_count", 0)),
                unique_terms=int((cached_domain or {}).get("unique_terms", 0)),
                total_tokens=int((cached_domain or quick).get("total_tokens", 0)),
                corpus_signature=normalize_optional_string((cached_domain or {}).get("corpus_signature")),
                updated_at=normalize_optional_string((cached_domain or {}).get("updated_at")),
                profile=(cached_domain or {}).get("profile") if cached_domain else None,
            )
        return state

    if not idf_available:
        state["project"] = _idf_profile_diagnostic(
            scope="project",
            name=project_name,
            thresholds=project_thresholds,
            status="unavailable",
            active=False,
            doc_count=int(quick_project.get("doc_count", 0)),
            unique_terms=0,
            total_tokens=int(quick_project.get("total_tokens", 0)),
        )
        if mode != "off":
            state["warnings"].append(
                "Agent Salience IDF helpers unavailable; install or point AGENT_SALIENCE_HOME to enable IDF activation."
            )
        if reason and mode != "off":
            state["warnings"].append(str(reason))
        return state

    should_build_project = force or wants_full_refresh
    if not should_build_project:
        should_build_project = (
            int(quick_project.get("doc_count", 0)) >= int(project_thresholds["min_documents"])
            and int(quick_project.get("total_tokens", 0)) >= int(project_thresholds["min_total_tokens"])
        )
    if should_build_project:
        project_records = _idf_corpus_records()
        project_signature = _idf_corpus_signature(project_records)
        cached_project = _load_cached_idf_profile("project", project_name)
        if cached_project is not None and normalize_optional_string(cached_project.get("corpus_signature")) == project_signature:
            state["project"] = _idf_profile_diagnostic(
                scope="project",
                name=project_name,
                thresholds=project_thresholds,
                status=str(cached_project.get("status", "cold")),
                active=bool(cached_project.get("active", False)),
                doc_count=int(cached_project.get("doc_count", 0)),
                unique_terms=int(cached_project.get("unique_terms", 0)),
                total_tokens=int(cached_project.get("total_tokens", 0)),
                corpus_signature=project_signature,
                updated_at=normalize_optional_string(cached_project.get("updated_at")),
                profile=cached_project.get("profile"),
            )
        else:
            built = _build_idf_profile_if_ready(
                "project",
                project_name,
                project_records,
                project_thresholds,
                force=force,
            )
            built["corpus_signature"] = project_signature
            built["updated_at"] = now_iso()
            _save_idf_profile("project", project_name, built.get("profile") or {}, project_signature, bool(built.get("active")))
            state["project"] = built
    else:
        cached_project = _load_cached_idf_profile("project", project_name)
        if cached_project is not None:
            state["project"] = _idf_profile_diagnostic(
                scope="project",
                name=project_name,
                thresholds=project_thresholds,
                status="cold",
                active=False,
                doc_count=int(quick_project.get("doc_count", 0)),
                unique_terms=int(cached_project.get("unique_terms", 0)),
                total_tokens=int(quick_project.get("total_tokens", 0)),
                corpus_signature=normalize_optional_string(cached_project.get("corpus_signature")),
                updated_at=normalize_optional_string(cached_project.get("updated_at")),
                profile=cached_project.get("profile"),
            )
        else:
            state["project"] = _idf_profile_diagnostic(
                scope="project",
                name=project_name,
                thresholds=project_thresholds,
                status="cold",
                active=False,
                doc_count=int(quick_project.get("doc_count", 0)),
                unique_terms=0,
                total_tokens=int(quick_project.get("total_tokens", 0)),
            )

    domain_quick = _idf_domain_quick_stats()
    for domain_name, quick in sorted(domain_quick.items()):
        should_build_domain = force or wants_full_refresh
        if not should_build_domain:
            should_build_domain = (
                int(quick.get("doc_count", 0)) >= int(domain_thresholds["min_documents"])
                and int(quick.get("total_tokens", 0)) >= int(domain_thresholds["min_total_tokens"])
            )
        if should_build_domain:
            domain_records = _idf_corpus_records(domain=domain_name)
            domain_signature = _idf_corpus_signature(domain_records)
            cached_domain = _load_cached_idf_profile("domain", domain_name)
            if cached_domain is not None and normalize_optional_string(cached_domain.get("corpus_signature")) == domain_signature:
                state["domains"][domain_name] = _idf_profile_diagnostic(
                    scope="domain",
                    name=domain_name,
                    thresholds=domain_thresholds,
                    status=str(cached_domain.get("status", "cold")),
                    active=bool(cached_domain.get("active", False)),
                    doc_count=int(cached_domain.get("doc_count", 0)),
                    unique_terms=int(cached_domain.get("unique_terms", 0)),
                    total_tokens=int(cached_domain.get("total_tokens", 0)),
                    corpus_signature=domain_signature,
                    updated_at=normalize_optional_string(cached_domain.get("updated_at")),
                    profile=cached_domain.get("profile"),
                )
            else:
                built_domain = _build_idf_profile_if_ready(
                    "domain",
                    domain_name,
                    domain_records,
                    domain_thresholds,
                    force=force,
                )
                built_domain["corpus_signature"] = domain_signature
                built_domain["updated_at"] = now_iso()
                _save_idf_profile(
                    "domain",
                    domain_name,
                    built_domain.get("profile") or {},
                    domain_signature,
                    bool(built_domain.get("active")),
                )
                state["domains"][domain_name] = built_domain
        else:
            cached_domain = _load_cached_idf_profile("domain", domain_name)
            if cached_domain is not None:
                state["domains"][domain_name] = _idf_profile_diagnostic(
                    scope="domain",
                    name=domain_name,
                    thresholds=domain_thresholds,
                    status="cold",
                    active=False,
                    doc_count=int(quick.get("doc_count", 0)),
                    unique_terms=int(cached_domain.get("unique_terms", 0)),
                    total_tokens=int(quick.get("total_tokens", 0)),
                    corpus_signature=normalize_optional_string(cached_domain.get("corpus_signature")),
                    updated_at=normalize_optional_string(cached_domain.get("updated_at")),
                    profile=cached_domain.get("profile"),
                )
            else:
                state["domains"][domain_name] = _idf_profile_diagnostic(
                    scope="domain",
                    name=domain_name,
                    thresholds=domain_thresholds,
                    status="cold",
                    active=False,
                    doc_count=int(quick.get("doc_count", 0)),
                    unique_terms=0,
                    total_tokens=int(quick.get("total_tokens", 0)),
                )

    if mode == "auto" and state["project"].get("status") == "cold":
        remaining = state["project"].get("remaining", {})
        state["recommendations"].append(
            "Project IDF is cold; remaining documents="
            f"{int(remaining.get('documents', 0))}, unique_terms={int(remaining.get('unique_terms', 0))}, "
            f"total_tokens={int(remaining.get('total_tokens', 0))}."
        )
    return state


def _resolve_idf_profile_for_memory_or_query(
    domain: str | None = None,
    *,
    idf_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = idf_state if isinstance(idf_state, dict) else _ensure_idf_profiles(trigger="salience_check")
    wanted_domain = normalize_optional_string(domain)
    selected: dict[str, Any] | None = None
    scope = "project"
    name = "default"
    domain_info = None
    if wanted_domain and isinstance(state.get("domains"), dict):
        maybe_domain = state["domains"].get(wanted_domain)
        if isinstance(maybe_domain, dict):
            domain_info = maybe_domain
    project_info = state.get("project") if isinstance(state.get("project"), dict) else None

    # Prefer active domain IDF when requested.
    if isinstance(domain_info, dict) and bool(domain_info.get("active")) and isinstance(domain_info.get("profile"), dict):
        selected = domain_info
        scope = "domain"
        name = wanted_domain or "default"
    # Otherwise use active project IDF when available.
    elif isinstance(project_info, dict) and bool(project_info.get("active")) and isinstance(project_info.get("profile"), dict):
        selected = project_info
        scope = "project"
        name = "default"
    # If neither is active, return the requested domain diagnostics when present, else project diagnostics.
    elif isinstance(domain_info, dict):
        selected = domain_info
        scope = "domain"
        name = wanted_domain or "default"
    elif isinstance(project_info, dict):
        selected = project_info
        scope = "project"
        name = "default"
    status = str((selected or {}).get("status", "cold"))
    active = bool((selected or {}).get("active", False))
    profile = (selected or {}).get("profile")
    if not active or not isinstance(profile, dict):
        profile = None
    return {
        "mode": str(state.get("mode", idf_mode())),
        "available": bool(state.get("available", False)),
        "scope": scope,
        "name": name,
        "status": status,
        "active": active and profile is not None,
        "profile": profile,
    }


def _refresh_idf_profiles_safely(trigger: str = "write") -> None:
    try:
        _ensure_idf_profiles(trigger=trigger)
    except Exception:
        return


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
    event_count = 0
    recent_event_count = 0
    missing_event_columns: list[str] = []
    events_fts_enabled = False
    last_event_iso: str | None = None
    last_event_kind: str | None = None
    recent_cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    if backend == "sqlite":
        try:
            with _sqlite_session() as conn:
                _sqlite_ensure_schema(conn)
                _sqlite_bootstrap_if_needed(conn)
                row = conn.execute("SELECT COUNT(*) FROM events").fetchone()
                event_count = int(row[0]) if row else 0
                row = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE COALESCE(ts, created_at) >= ?",
                    (recent_cutoff,),
                ).fetchone()
                recent_event_count = int(row[0]) if row else 0
                latest = conn.execute(
                    """
                    SELECT action, event_type, COALESCE(ts, created_at) AS stamp
                    FROM events
                    ORDER BY COALESCE(ts, created_at) DESC, rowid DESC
                    LIMIT 1
                    """
                ).fetchone()
                if latest:
                    last_event_iso = str(latest["stamp"] or "").strip() or None
                    last_event_kind = str(latest["action"] or latest["event_type"] or "").strip() or None
                event_cols = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
                required_event_cols = {
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
                missing_event_columns = sorted(required_event_cols - event_cols)
            events_fts_enabled = _sqlite_events_fts_flag()
        except Exception:
            event_count = 0
            recent_event_count = 0
    else:
        rows = _legacy_event_rows(include_archive=False) if (event_logging_enabled() or query_logging_enabled()) else []
        event_count = len(rows)
        recent_event_count = sum(1 for row in rows if str(row.get("ts") or "") >= recent_cutoff)
        latest = rows[0] if rows else {}
        last_event_iso = str(latest.get("ts") or "").strip() or None
        last_event_kind = str(latest.get("action") or latest.get("event_type") or "").strip() or None

    if not event_logging_enabled():
        warnings.append("MNEMO_LOG_EVENTS=0; event history is not being recorded")
    elif backend == "json" and not events_exists:
        warnings.append("events log file not found yet; no events recorded")
    if missing_event_columns:
        warnings.append(f"events table missing typed columns: {', '.join(missing_event_columns)}")

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
    idf_state = _ensure_idf_profiles(trigger="doctor")
    idf_project = _idf_public_view(idf_state.get("project", {})) if isinstance(idf_state.get("project"), dict) else {}
    idf_domains: dict[str, dict[str, Any]] = {}
    if isinstance(idf_state.get("domains"), dict):
        for domain_name, domain_info in sorted(idf_state["domains"].items()):
            if isinstance(domain_info, dict):
                idf_domains[domain_name] = _idf_public_view(domain_info)
    idf_warnings = [str(item) for item in idf_state.get("warnings", []) if str(item).strip()]
    idf_recommendations = [str(item) for item in idf_state.get("recommendations", []) if str(item).strip()]
    for warning in idf_warnings:
        if warning not in warnings:
            warnings.append(warning)

    aliases_payload: dict[str, Any] = {
        "available": backend == "sqlite",
        "concept_count": 0,
        "active_concept_count": 0,
        "alias_count": 0,
        "active_alias_count": 0,
        "pending_proposal_count": 0,
        "rejected_proposal_count": 0,
        "views_available": False,
        "warnings": [],
        "recommendations": [],
    }
    if backend == "sqlite":
        try:
            with _sqlite_session() as conn:
                _sqlite_ensure_schema(conn)
                _sqlite_bootstrap_if_needed(conn)
                aliases_payload["concept_count"] = int(conn.execute("SELECT COUNT(*) FROM alias_concepts").fetchone()[0])
                aliases_payload["active_concept_count"] = int(
                    conn.execute("SELECT COUNT(*) FROM alias_concepts WHERE status = 'active'").fetchone()[0]
                )
                aliases_payload["alias_count"] = int(conn.execute("SELECT COUNT(*) FROM alias_terms").fetchone()[0])
                aliases_payload["active_alias_count"] = int(
                    conn.execute("SELECT COUNT(*) FROM alias_terms WHERE status = 'active'").fetchone()[0]
                )
                aliases_payload["pending_proposal_count"] = int(
                    conn.execute("SELECT COUNT(*) FROM alias_proposals WHERE status = 'pending'").fetchone()[0]
                )
                aliases_payload["rejected_proposal_count"] = int(
                    conn.execute("SELECT COUNT(*) FROM alias_proposals WHERE status = 'rejected'").fetchone()[0]
                )
                view_rows = conn.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='view' AND name IN (
                        'v_alias_vocabulary',
                        'v_alias_pending_proposals',
                        'v_alias_concept_counts'
                    )
                    """
                ).fetchall()
                aliases_payload["views_available"] = len(view_rows) == 3
        except Exception as exc:
            aliases_payload["warnings"].append(f"alias diagnostics unavailable: {type(exc).__name__}")
        if int(aliases_payload.get("pending_proposal_count", 0)) > 0 and int(aliases_payload.get("active_alias_count", 0)) == 0:
            aliases_payload["recommendations"].append(
                "Review pending alias proposals and approve high-confidence items via maintenance approve_alias."
            )
        if not bool(aliases_payload.get("views_available")):
            aliases_payload["warnings"].append("alias inspection views are unavailable")
    else:
        aliases_payload["warnings"].append("alias tables are only available in sqlite backend")

    memory_packs_payload: dict[str, Any] = {
        "count_by_namespace": {},
        "count_by_origin": {},
        "count_by_namespace_kind": {},
        "total_topic_count": 0,
        "top_topics": [],
        "untagged_memory_count": 0,
        "import_freshness_non_null_count": 0,
        "imported_packs_count": 0,
        "exported_packs_count": 0,
    }
    if backend == "sqlite":
        try:
            with _sqlite_session() as conn:
                _sqlite_ensure_schema(conn)
                _sqlite_bootstrap_if_needed(conn)

                namespace_rows = conn.execute(
                    "SELECT namespace, COUNT(*) AS count FROM memories GROUP BY namespace ORDER BY count DESC, namespace ASC"
                ).fetchall()
                origin_rows = conn.execute(
                    "SELECT origin, COUNT(*) AS count FROM memories GROUP BY origin ORDER BY count DESC, origin ASC"
                ).fetchall()
                kind_rows = conn.execute(
                    """
                    SELECT namespace, kind, COUNT(*) AS count
                    FROM memories
                    GROUP BY namespace, kind
                    ORDER BY namespace ASC, count DESC, kind ASC
                    """
                ).fetchall()
                topic_rows = conn.execute(
                    """
                    SELECT topic, COUNT(*) AS count
                    FROM memory_topics
                    GROUP BY topic
                    ORDER BY count DESC, topic ASC
                    LIMIT 20
                    """
                ).fetchall()
                total_topic_count = int(conn.execute("SELECT COUNT(*) FROM memory_topics").fetchone()[0])
                untagged_count = int(
                    conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM memories m
                        LEFT JOIN memory_topics t ON t.memory_id = m.id
                        WHERE t.memory_id IS NULL
                        """
                    ).fetchone()[0]
                )
                freshness_non_null_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM memories WHERE import_freshness IS NOT NULL AND TRIM(import_freshness) != ''"
                    ).fetchone()[0]
                )
                imported_packs_count = int(conn.execute("SELECT COUNT(*) FROM imported_packs").fetchone()[0])
                exported_packs_count = int(conn.execute("SELECT COUNT(*) FROM exported_packs").fetchone()[0])

                count_by_namespace: dict[str, int] = {}
                for row in namespace_rows:
                    namespace_key = str(row["namespace"] or DEFAULT_MEMORY_NAMESPACE)
                    count_by_namespace[namespace_key] = int(row["count"] or 0)
                count_by_origin: dict[str, int] = {}
                for row in origin_rows:
                    origin_key = str(row["origin"] or DEFAULT_MEMORY_ORIGIN)
                    count_by_origin[origin_key] = int(row["count"] or 0)
                count_by_namespace_kind: dict[str, dict[str, int]] = {}
                for row in kind_rows:
                    namespace_key = str(row["namespace"] or DEFAULT_MEMORY_NAMESPACE)
                    kind_key = str(row["kind"] or "note")
                    count_by_namespace_kind.setdefault(namespace_key, {})[kind_key] = int(row["count"] or 0)
                top_topics = [
                    {"topic": str(row["topic"] or ""), "count": int(row["count"] or 0)}
                    for row in topic_rows
                    if str(row["topic"] or "").strip()
                ]
                memory_packs_payload = {
                    "count_by_namespace": count_by_namespace,
                    "count_by_origin": count_by_origin,
                    "count_by_namespace_kind": count_by_namespace_kind,
                    "total_topic_count": total_topic_count,
                    "top_topics": top_topics,
                    "untagged_memory_count": untagged_count,
                    "import_freshness_non_null_count": freshness_non_null_count,
                    "imported_packs_count": imported_packs_count,
                    "exported_packs_count": exported_packs_count,
                }
        except Exception as exc:
            warnings.append(f"memory packs diagnostics unavailable: {type(exc).__name__}")
    else:
        count_by_namespace: dict[str, int] = {}
        count_by_origin: dict[str, int] = {}
        count_by_namespace_kind: dict[str, dict[str, int]] = {}
        import_freshness_non_null_count = 0
        for memory in memories:
            namespace_key = memory_namespace(memory)
            origin_key = memory_origin(memory)
            kind_key = str(memory.get("kind") or "note")
            count_by_namespace[namespace_key] = count_by_namespace.get(namespace_key, 0) + 1
            count_by_origin[origin_key] = count_by_origin.get(origin_key, 0) + 1
            kind_counts = count_by_namespace_kind.setdefault(namespace_key, {})
            kind_counts[kind_key] = kind_counts.get(kind_key, 0) + 1
            if memory_import_freshness(memory):
                import_freshness_non_null_count += 1
        memory_packs_payload = {
            "count_by_namespace": count_by_namespace,
            "count_by_origin": count_by_origin,
            "count_by_namespace_kind": count_by_namespace_kind,
            "total_topic_count": 0,
            "top_topics": [],
            "untagged_memory_count": memory_count,
            "import_freshness_non_null_count": import_freshness_non_null_count,
            "imported_packs_count": 0,
            "exported_packs_count": 0,
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
    # Signature outdated warning
    sig_missing = 0
    sig_total = 0
    if store_backend() == "sqlite":
        try:
            with _sqlite_session() as _conn:
                _sqlite_ensure_schema(_conn)
                sig_total = _conn.execute("SELECT COUNT(*) FROM memories WHERE deleted=0").fetchone()[0]
                sig_missing = _conn.execute(
                    """SELECT COUNT(*) FROM memories WHERE deleted=0
                       AND (signature_version IS NULL OR signature_version != ?
                            OR normalizer_version IS NULL OR normalizer_version != ?
                            OR content_hash IS NULL OR normalized_hash IS NULL
                            OR shingle_hashes_json IS NULL)""",
                    (SIGNATURE_VERSION, NORMALIZER_VERSION),
                ).fetchone()[0]
        except Exception:
            pass
    if sig_total > 0 and sig_missing / sig_total > 0.10:
        warnings.append(
            f"signatures_outdated: {sig_missing} of {sig_total} active memories have missing or outdated signatures. "
            f"Run mnemo {{\"action\":\"maintenance\",\"params\":{{\"action\":\"backfill_signatures\",\"dry_run\":false}}}}."
        )
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
    if backend == "sqlite" and not events_fts_enabled:
        recommendations.append("SQLite events FTS5 is unavailable; event search uses lexical fallback.")
    for recommendation in idf_recommendations:
        if recommendation not in recommendations:
            recommendations.append(recommendation)
    for warning in aliases_payload.get("warnings", []):
        warning_text = str(warning).strip()
        if warning_text and warning_text not in warnings:
            warnings.append(warning_text)
    for recommendation in aliases_payload.get("recommendations", []):
        recommendation_text = str(recommendation).strip()
        if recommendation_text and recommendation_text not in recommendations:
            recommendations.append(recommendation_text)

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
        "event_count": event_count,
        "recent_event_count": recent_event_count,
        "events_fts_enabled": events_fts_enabled,
        "missing_event_columns": missing_event_columns,
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
    idf_domains_ready = sum(1 for info in idf_domains.values() if bool(info.get("active")))
    backend_exists = sqlite_exists if backend == "sqlite" else mem_exists
    backend_file_name = "mnemo.sqlite" if backend == "sqlite" else "memory.json"
    backend_size = sqlite_size if backend == "sqlite" else memory_file_payload["size_bytes"]
    summary_lines = [
        f"Mnemo {SERVER_VERSION} - {backend_file_name} {'exists' if backend_exists else 'missing'} ({backend_size} bytes, {memory_file_payload['memory_count']} memories)",
        f"Last write: {memory_file_payload['last_write_iso']}  Last id: {memory_file_payload['last_memory_id']}",
        f"Events: {event_count} total, {recent_event_count} in last 24h, events_fts={'on' if events_fts_enabled else 'off'}",
        f"Aliases: active_concepts={int(aliases_payload.get('active_concept_count', 0))} active_aliases={int(aliases_payload.get('active_alias_count', 0))} pending_proposals={int(aliases_payload.get('pending_proposal_count', 0))}",
        f"Namespaces: {len(memory_packs_payload.get('count_by_namespace', {}))} namespaces, {int(memory_packs_payload.get('total_topic_count', 0))} topic rows, imported_packs={int(memory_packs_payload.get('imported_packs_count', 0))}, exported_packs={int(memory_packs_payload.get('exported_packs_count', 0))}",
        f"Kinds: {kind_summary}",
        f"Drift: {drift_value:.2f} ({drift_interp})  Salience: {salience_text}  Search: {search_backend}",
        f"IDF: project={idf_project.get('status', 'cold')} active={'yes' if idf_project.get('active') else 'no'} domains_ready={idf_domains_ready} mode={idf_state.get('mode', idf_mode())}",
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
        "event_count": event_count,
        "recent_event_count": recent_event_count,
        "events_fts_enabled": events_fts_enabled,
        "missing_event_columns": missing_event_columns,
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
        "fts": {
            "available": fts_available,
            "enabled": fts_available,
            "candidate_source": "fts5" if fts_available else "fallback",
        },
        "search_backend": search_backend,
        "memory_file": memory_file_payload,
        "events_log": events_payload,
        "archive": archive_payload,
        "drift": drift,
        "salience": salience_payload,
        "aliases": aliases_payload,
        "memory_packs": memory_packs_payload,
        "idf": {
            "mode": str(idf_state.get("mode", idf_mode())),
            "available": bool(idf_state.get("available", False)),
            "project": idf_project,
            "domains": idf_domains,
            "warnings": idf_warnings,
            "recommendations": idf_recommendations,
        },
        "warnings": warnings,
        "recommendations": recommendations,
    }
    return text_result("\n".join(summary_lines), payload)




def memory_backfill_signatures_gateway(args: dict[str, Any]) -> dict[str, Any]:
    dry_run = parse_bool(args.get("dry_run"), default=True)
    return _backfill_signatures_maintenance(args, dry_run)


def memory_consolidate_full_gateway(args: dict[str, Any]) -> dict[str, Any]:
    return _consolidate_full_maintenance(args)


class PackImportError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)


class PackPromoteError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)


def _pack_import_add_warning(
    warnings: list[dict[str, Any]],
    code: str,
    message: str,
    *,
    extra: dict[str, Any] | None = None,
) -> None:
    key = f"{code}:{message}"
    seen = {f"{str(item.get('code', ''))}:{str(item.get('message', ''))}" for item in warnings if isinstance(item, dict)}
    if key in seen:
        return
    row: dict[str, Any] = {"code": str(code), "message": str(message)}
    if isinstance(extra, dict):
        for name, value in extra.items():
            if name in row:
                continue
            row[str(name)] = value
    warnings.append(row)


def _pack_error_with_warnings(
    code: str,
    message: str,
    warnings: list[dict[str, Any]] | None,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": {"code": str(code), "message": str(message)},
        "warnings": [dict(item) for item in (warnings or []) if isinstance(item, dict)],
    }
    if isinstance(extra, dict) and extra:
        payload.update(extra)
    return {
        "content": [{"type": "text", "text": f"Error [{code}]: {message}"}],
        "isError": True,
        "structuredContent": payload,
    }


def _pack_import_file_freshness(repo_root: str | None, path_value: Any, file_sha: Any) -> str:
    path_text = normalize_optional_string(path_value)
    sha_text = normalize_optional_string(file_sha)
    if path_text is None or sha_text is None or repo_root is None:
        return "unknown"
    current_sha = current_file_sha(repo_root, path_text)
    if current_sha is None:
        try:
            local_path = (Path(repo_root) / path_text).resolve()
        except Exception:
            local_path = Path(repo_root) / path_text
        return "missing" if not local_path.exists() else "unknown"
    return "verified" if str(current_sha) == sha_text else "stale"


def _pack_import_memory_freshness(
    repo_root: str | None,
    touched_files: list[dict[str, Any]],
    by_file_counts: dict[str, int],
) -> str:
    if not touched_files:
        return "unknown"
    labels: list[str] = []
    for item in touched_files:
        if not isinstance(item, dict):
            labels.append("unknown")
            by_file_counts["unknown"] = int(by_file_counts.get("unknown", 0) + 1)
            continue
        label = _pack_import_file_freshness(repo_root, item.get("path"), item.get("file_sha"))
        if label not in PACK_IMPORT_FRESHNESS_VALUES:
            label = "unknown"
        labels.append(label)
        by_file_counts[label] = int(by_file_counts.get(label, 0) + 1)
    if labels and all(label == "verified" for label in labels):
        return "verified"
    if "missing" in labels:
        return "missing"
    if "stale" in labels:
        return "stale"
    return "unknown"


def _pack_source_label_basename(raw_value: Any) -> str:
    value = normalize_optional_string(raw_value)
    if value is None:
        return ""
    normalized = value.replace("\\", "/")
    if "/" in normalized:
        normalized = normalized.rsplit("/", 1)[-1]
    return normalized or value


def _pack_row_id_natural_key(row_id: Any) -> tuple[str, int, int, str]:
    value = str(row_id or "")
    idx = len(value)
    while idx > 0 and value[idx - 1].isdigit():
        idx -= 1
    if idx < len(value) and idx > 0:
        return (value[:idx], 0, int(value[idx:]), value)
    return (value, 1, 0, value)


def _pack_like_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _pack_review_sample_preview(row: dict[str, Any]) -> str:
    for key in ("text", "title"):
        value = normalize_optional_string(row.get(key))
        if value:
            return collapsed_preview_text(value, max_chars=200)
    extra_candidates = [
        key
        for key, value in row.items()
        if key not in {"text", "title"}
        and isinstance(value, str)
        and bool(str(value).strip())
    ]
    for key in sorted(extra_candidates):
        value = normalize_optional_string(row.get(key))
        if value:
            return collapsed_preview_text(value, max_chars=200)
    return ""


def _pack_row_filters_supplied(args: dict[str, Any], *, include_query: bool) -> bool:
    list_fields = ("topics", "kinds", "import_freshness", "row_ids", "memory_ids", "touched_paths")
    for name in list_fields:
        values = normalize_optional_string_list(args.get(name), name) or []
        if values:
            return True
    if include_query and normalize_optional_string(args.get("query")):
        return True
    return False


def _get_imported_pack(conn: sqlite3.Connection, pack_id: str) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT pack_id, pack_name, source_label, trust_level, namespace,
               imported_at, manifest_json, freshness_summary_json, received_zip_sha256
        FROM imported_packs
        WHERE pack_id = ?
        """,
        (pack_id,),
    ).fetchone()
    if isinstance(row, sqlite3.Row):
        return row
    return None


def _select_imported_pack_rows(
    conn: sqlite3.Connection,
    pack_id: str,
    args: dict[str, Any],
    warnings: list[dict[str, str]],
    *,
    allow_query: bool,
) -> dict[str, Any]:
    topics = normalize_optional_string_list(args.get("topics"), "topics") or []
    kinds_raw = normalize_optional_string_list(args.get("kinds"), "kinds") or []
    kinds: list[str] = []
    seen_kinds: set[str] = set()
    for item in kinds_raw:
        value = str(item).strip().lower()
        if not value or value in seen_kinds:
            continue
        seen_kinds.add(value)
        kinds.append(value)

    freshness_raw = normalize_optional_string_list(args.get("import_freshness"), "import_freshness") or []
    import_freshness: list[str] = []
    seen_freshness: set[str] = set()
    allowed_freshness = set(PACK_IMPORT_FRESHNESS_VALUES)
    for item in freshness_raw:
        value = str(item).strip().lower()
        if not value or value in seen_freshness:
            continue
        if value not in allowed_freshness:
            raise ValueError(
                "import_freshness must be an array containing only: "
                + ", ".join(PACK_IMPORT_FRESHNESS_VALUES)
            )
        seen_freshness.add(value)
        import_freshness.append(value)

    row_ids = normalize_optional_string_list(args.get("row_ids"), "row_ids") or []
    memory_ids_input = normalize_optional_string_list(args.get("memory_ids"), "memory_ids") or []
    raw_touched_paths = normalize_optional_string_list(args.get("touched_paths"), "touched_paths") or []
    touched_paths = normalize_touched_paths(args.get("touched_paths")) or []
    query_text = normalize_optional_string(args.get("query"))
    if query_text is not None and not allow_query:
        _pack_import_add_warning(
            warnings,
            "unsupported_filter_query",
            "query is not supported for this action and was ignored.",
        )
        query_text = None

    limit = _safe_int(args.get("limit"), PACK_REVIEW_LIMIT_DEFAULT, minimum=1, maximum=PACK_REVIEW_LIMIT_MAX)

    clauses = ["ipr.pack_id = ?"]
    sql_params: list[Any] = [pack_id]

    memory_ids_filter = list(memory_ids_input)
    if memory_ids_input:
        pack_memory_rows = conn.execute(
            "SELECT memory_id FROM imported_pack_rows WHERE pack_id = ?",
            (pack_id,),
        ).fetchall()
        pack_memory_ids = {
            str(row["memory_id"] if isinstance(row, sqlite3.Row) else row[0]) for row in pack_memory_rows
        }
        existing_rows = conn.execute(
            f"SELECT id FROM memories WHERE id IN ({','.join('?' for _ in memory_ids_input)})",
            tuple(memory_ids_input),
        ).fetchall()
        existing_ids = {str(row["id"] if isinstance(row, sqlite3.Row) else row[0]) for row in existing_rows}
        outside_existing = sorted(existing_ids - pack_memory_ids)
        if outside_existing:
            _pack_import_add_warning(
                warnings,
                "memory_ids_outside_pack_filtered",
                f"{len(outside_existing)} memory_ids were outside pack {pack_id} and were filtered out.",
            )
        memory_ids_filter = [memory_id for memory_id in memory_ids_input if memory_id in pack_memory_ids]

    if kinds:
        placeholders = ",".join("?" for _ in kinds)
        clauses.append(f"m.kind IN ({placeholders})")
        sql_params.extend(kinds)

    if import_freshness:
        placeholders = ",".join("?" for _ in import_freshness)
        clauses.append(
            "CASE WHEN m.import_freshness IS NULL OR m.import_freshness = '' THEN 'unknown' "
            f"ELSE m.import_freshness END IN ({placeholders})"
        )
        sql_params.extend(import_freshness)

    if row_ids:
        placeholders = ",".join("?" for _ in row_ids)
        clauses.append(f"ipr.row_id_in_pack IN ({placeholders})")
        sql_params.extend(row_ids)

    if memory_ids_input:
        if not memory_ids_filter:
            clauses.append("1 = 0")
        else:
            placeholders = ",".join("?" for _ in memory_ids_filter)
            clauses.append(f"m.id IN ({placeholders})")
            sql_params.extend(memory_ids_filter)

    if topics:
        placeholders = ",".join("?" for _ in topics)
        clauses.append(
            "EXISTS ("
            "SELECT 1 FROM memory_topics mt "
            "WHERE mt.memory_id = m.id "
            f"AND mt.topic IN ({placeholders})"
            ")"
        )
        sql_params.extend(topics)

    if touched_paths:
        path_clauses: list[str] = []
        if raw_touched_paths:
            raw_placeholders = ",".join("?" for _ in raw_touched_paths)
            path_clauses.append(f"mf_path.path IN ({raw_placeholders})")
        normalized_placeholders = ",".join("?" for _ in touched_paths)
        normalized_expr = "REPLACE(REPLACE(mf_path.path, char(92), '/'), './', '')"
        path_clauses.append(f"{normalized_expr} IN ({normalized_placeholders})")
        clauses.append(
            "EXISTS ("
            "SELECT 1 FROM memory_files mf_path "
            "WHERE mf_path.memory_id = m.id "
            "AND mf_path.memory_table = m.kind "
            f"AND ({' OR '.join(path_clauses)})"
            ")"
        )
        sql_params.extend(raw_touched_paths)
        sql_params.extend(touched_paths)

    if query_text is not None:
        like_value = f"%{_pack_like_escape(query_text.lower())}%"
        clauses.append(
            "(LOWER(COALESCE(m.text, '')) LIKE ? ESCAPE '\\' "
            "OR LOWER(COALESCE(m.title, '')) LIKE ? ESCAPE '\\')"
        )
        sql_params.extend([like_value, like_value])

    where_sql = " AND ".join(clauses)
    from_sql = (
        "FROM imported_pack_rows ipr "
        "JOIN memories m ON m.id = ipr.memory_id "
        "LEFT JOIN promoted_pack_rows ppr ON ppr.pack_id = ipr.pack_id AND ppr.row_id_in_pack = ipr.row_id_in_pack"
    )

    total_pack_row = conn.execute(
        "SELECT COUNT(*) FROM imported_pack_rows WHERE pack_id = ?",
        (pack_id,),
    ).fetchone()
    total_pack_rows = int(total_pack_row[0] if total_pack_row else 0)

    selected_rows_raw = conn.execute(
        "SELECT ipr.row_id_in_pack, ipr.memory_id, m.kind, m.namespace, m.origin, m.import_freshness, "
        "m.text, m.title, m.preview, m.domain, m.git_sha, m.git_branch, m.git_dirty, m.created_at, m.updated_at, "
        "ppr.promoted_memory_id, ppr.promotion_id, ppr.promoted_at "
        f"{from_sql} WHERE {where_sql}",
        tuple(sql_params),
    ).fetchall()
    selected_rows_all: list[dict[str, Any]] = [
        {
            "row_id_in_pack": str(row["row_id_in_pack"] if isinstance(row, sqlite3.Row) else row[0]),
            "memory_id": str(row["memory_id"] if isinstance(row, sqlite3.Row) else row[1]),
            "kind": str(row["kind"] if isinstance(row, sqlite3.Row) else row[2]),
            "namespace": str(row["namespace"] if isinstance(row, sqlite3.Row) else row[3]),
            "origin": str(row["origin"] if isinstance(row, sqlite3.Row) else row[4]),
            "import_freshness": normalize_optional_string(
                row["import_freshness"] if isinstance(row, sqlite3.Row) else row[5]
            )
            or "unknown",
            "text": str(row["text"] if isinstance(row, sqlite3.Row) else row[6]),
            "title": normalize_optional_string(row["title"] if isinstance(row, sqlite3.Row) else row[7]),
            "preview": normalize_optional_string(row["preview"] if isinstance(row, sqlite3.Row) else row[8]),
            "domain": normalize_optional_string(row["domain"] if isinstance(row, sqlite3.Row) else row[9]),
            "git_sha": normalize_optional_string(row["git_sha"] if isinstance(row, sqlite3.Row) else row[10]),
            "git_branch": normalize_optional_string(row["git_branch"] if isinstance(row, sqlite3.Row) else row[11]),
            "git_dirty": normalize_git_dirty(row["git_dirty"] if isinstance(row, sqlite3.Row) else row[12]),
            "created_at": normalize_optional_string(row["created_at"] if isinstance(row, sqlite3.Row) else row[13]),
            "updated_at": normalize_optional_string(row["updated_at"] if isinstance(row, sqlite3.Row) else row[14]),
            "promoted_to_memory_id": normalize_optional_string(
                row["promoted_memory_id"] if isinstance(row, sqlite3.Row) else row[15]
            ),
            "promotion_id": normalize_optional_string(
                row["promotion_id"] if isinstance(row, sqlite3.Row) else row[16]
            ),
            "promoted_at": normalize_optional_string(
                row["promoted_at"] if isinstance(row, sqlite3.Row) else row[17]
            ),
        }
        for row in selected_rows_raw
    ]
    selected_rows_all.sort(
        key=lambda item: (
            str(item.get("kind", "")),
            _pack_row_id_natural_key(item.get("row_id_in_pack")),
            str(item.get("memory_id", "")),
        )
    )

    selected_total = len(selected_rows_all)
    limited = bool(selected_total > limit)
    limited_rows = list(selected_rows_all[:limit])

    by_kind: dict[str, int] = {}
    by_import_freshness: dict[str, int] = {name: 0 for name in PACK_IMPORT_FRESHNESS_VALUES}
    for row in selected_rows_all:
        kind_name = str(row.get("kind", ""))
        freshness_label = str(row.get("import_freshness", "unknown") or "unknown")
        if freshness_label not in by_import_freshness:
            freshness_label = "unknown"
        by_kind[kind_name] = int(by_kind.get(kind_name, 0) + 1)
        by_import_freshness[freshness_label] = int(by_import_freshness.get(freshness_label, 0) + 1)

    topic_rows = conn.execute(
        "SELECT mt.topic, COUNT(*) AS count "
        f"{from_sql} "
        "JOIN memory_topics mt ON mt.memory_id = m.id "
        f"WHERE {where_sql} "
        "GROUP BY mt.topic "
        "ORDER BY count DESC, mt.topic ASC",
        tuple(sql_params),
    ).fetchall()
    by_topic: dict[str, int] = {}
    for row in topic_rows:
        topic_name = str(row["topic"] if isinstance(row, sqlite3.Row) else row[0])
        topic_count = int(row["count"] if isinstance(row, sqlite3.Row) else row[1])
        by_topic[topic_name] = topic_count

    referenced_file_count_row = conn.execute(
        "SELECT COUNT(DISTINCT mf.path) "
        f"{from_sql} "
        "JOIN memory_files mf ON mf.memory_id = m.id AND mf.memory_table = m.kind "
        f"WHERE {where_sql}",
        tuple(sql_params),
    ).fetchone()
    referenced_files = int(referenced_file_count_row[0] if referenced_file_count_row else 0)
    top_files_rows = conn.execute(
        "SELECT mf.path, COUNT(DISTINCT mf.memory_id) AS count "
        f"{from_sql} "
        "JOIN memory_files mf ON mf.memory_id = m.id AND mf.memory_table = m.kind "
        f"WHERE {where_sql} "
        "GROUP BY mf.path "
        "ORDER BY count DESC, mf.path ASC "
        "LIMIT 20",
        tuple(sql_params),
    ).fetchall()
    top_referenced_files = [
        {
            "path": str(row["path"] if isinstance(row, sqlite3.Row) else row[0]),
            "count": int(row["count"] if isinstance(row, sqlite3.Row) else row[1]),
        }
        for row in top_files_rows
    ]

    limited_memory_ids = [str(row.get("memory_id", "")) for row in limited_rows]
    topics_by_memory = _pack_topics_by_memory_id(conn, limited_memory_ids)
    files_by_memory = _pack_files_by_memory_id(conn, limited_memory_ids)
    for row in limited_rows:
        memory_id = str(row.get("memory_id", ""))
        row["topics"] = list(topics_by_memory.get(memory_id, []))
        row["touched_files"] = list(files_by_memory.get(memory_id, []))

    return {
        "pack_id": pack_id,
        "topics_filter": topics,
        "kinds_filter": kinds,
        "import_freshness_filter": import_freshness,
        "row_ids_filter": row_ids,
        "memory_ids_filter": memory_ids_filter,
        "memory_ids_input": memory_ids_input,
        "touched_paths_filter": touched_paths,
        "query_filter": query_text,
        "limit": limit,
        "total_pack_rows": total_pack_rows,
        "selected_total": selected_total,
        "limited": limited,
        "selected_rows": limited_rows,
        "selected_row_ids": [str(row.get("row_id_in_pack", "")) for row in limited_rows],
        "by_kind": by_kind,
        "by_import_freshness": by_import_freshness,
        "by_topic": by_topic,
        "referenced_files": referenced_files,
        "top_referenced_files": top_referenced_files,
    }


def _pack_review_import_grouped_summary(
    conn: sqlite3.Connection,
    selected_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    limited_rows = list(selected_rows)
    freshness_counts: dict[str, int] = {name: 0 for name in PACK_IMPORT_FRESHNESS_VALUES}
    topic_groups: dict[str, dict[str, Any]] = {}
    domain_groups: dict[str, dict[str, Any]] = {}
    path_groups: dict[str, dict[str, Any]] = {}

    def add_group(bucket: dict[str, dict[str, Any]], key: str, *, label: str, row: dict[str, Any]) -> None:
        item = bucket.setdefault(
            key,
            {
                "group_id": key,
                "label": label,
                "row_count": 0,
                "row_ids": [],
                "memory_ids": [],
                "sample_titles": [],
            },
        )
        item["row_count"] = int(item["row_count"] + 1)
        row_id = str(row.get("row_id_in_pack", ""))
        memory_id = str(row.get("memory_id", ""))
        if row_id and row_id not in item["row_ids"]:
            item["row_ids"].append(row_id)
        if memory_id and memory_id not in item["memory_ids"]:
            item["memory_ids"].append(memory_id)
        title = normalize_optional_string(row.get("title")) or _pack_review_sample_preview(row)
        if title and title not in item["sample_titles"] and len(item["sample_titles"]) < 3:
            item["sample_titles"].append(title)

    for row in limited_rows:
        freshness_label = str(row.get("import_freshness", "unknown") or "unknown")
        if freshness_label not in freshness_counts:
            freshness_label = "unknown"
        freshness_counts[freshness_label] = int(freshness_counts.get(freshness_label, 0) + 1)
        for topic_name in row.get("topics", []):
            topic_value = str(topic_name)
            add_group(
                topic_groups,
                f"import_topic:{topic_value}",
                label=_memory_group_slug_label(topic_value),
                row=row,
            )
        domain_value = normalize_optional_string(row.get("domain"))
        if domain_value:
            add_group(
                domain_groups,
                f"import_domain:{domain_value}",
                label=_memory_group_slug_label(domain_value),
                row=row,
            )
        for file_info in row.get("touched_files", []):
            path_value = normalize_optional_string(
                file_info.get("path") if isinstance(file_info, dict) else file_info
            )
            if path_value:
                add_group(
                    path_groups,
                    f"import_path:{path_value}",
                    label=path_value,
                    row=row,
                )

    def finalize(bucket: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        groups = list(bucket.values())
        groups.sort(key=lambda item: (-int(item.get("row_count", 0)), str(item.get("group_id", ""))))
        final: list[dict[str, Any]] = []
        for item in groups[:20]:
            final.append(
                {
                    "group_id": str(item.get("group_id", "")),
                    "label": str(item.get("label", "")),
                    "row_count": int(item.get("row_count", 0)),
                    "row_ids": list(item.get("row_ids", []))[:3],
                    "memory_ids": list(item.get("memory_ids", []))[:3],
                    "sample_titles": list(item.get("sample_titles", []))[:3],
                }
            )
        return final

    top_topic_groups = finalize(topic_groups)
    top_domain_groups = finalize(domain_groups)
    top_path_groups = finalize(path_groups)
    suggested_promotion_groups = top_topic_groups[:5] if top_topic_groups else top_path_groups[:5]
    return {
        "top_topic_groups": top_topic_groups,
        "top_domain_groups": top_domain_groups,
        "top_path_groups": top_path_groups,
        "freshness_counts": freshness_counts,
        "suggested_promotion_groups": suggested_promotion_groups,
    }


def pack_list_imports(args: dict[str, Any]) -> dict[str, Any]:
    if store_backend() != "sqlite":
        return tool_error("pack_list_imports requires sqlite backend")

    try:
        trust_level = normalize_choice(
            args.get("trust_level"),
            "trust_level",
            ("quarantine", "trusted"),
            default=None,
            strict=True,
        )
        pack_id_filter = normalize_optional_string(args.get("pack_id"))
        namespace_filter = normalize_optional_string(args.get("namespace"))
        include_counts = parse_bool(args.get("include_counts"), default=True)
        include_topics = parse_bool(args.get("include_topics"), default=True)
        include_freshness = parse_bool(args.get("include_freshness"), default=True)
        limit = _safe_int(
            args.get("limit"),
            PACK_LIST_IMPORTS_LIMIT_DEFAULT,
            minimum=1,
            maximum=PACK_LIST_IMPORTS_LIMIT_MAX,
        )
    except ValueError as exc:
        return tool_error(str(exc))

    warnings: list[dict[str, str]] = []
    packs: list[dict[str, Any]] = []

    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)

            clauses = ["1 = 1"]
            sql_params: list[Any] = []
            if trust_level is not None:
                clauses.append("trust_level = ?")
                sql_params.append(trust_level)
            if pack_id_filter is not None:
                clauses.append("pack_id = ?")
                sql_params.append(pack_id_filter)
            if namespace_filter is not None:
                clauses.append("namespace = ?")
                sql_params.append(namespace_filter)

            where_sql = " AND ".join(clauses)
            total_row = conn.execute(
                f"SELECT COUNT(*) FROM imported_packs WHERE {where_sql}",
                tuple(sql_params),
            ).fetchone()
            total = int(total_row[0] if total_row else 0)

            rows = conn.execute(
                "SELECT pack_id, pack_name, namespace, trust_level, imported_at, source_label, "
                "freshness_summary_json, received_zip_sha256 "
                f"FROM imported_packs WHERE {where_sql} "
                "ORDER BY imported_at DESC, pack_id ASC "
                "LIMIT ?",
                tuple(sql_params + [limit]),
            ).fetchall()

            for row in rows:
                row_pack_id = str(row["pack_id"] if isinstance(row, sqlite3.Row) else row[0])
                row_pack_name = str(row["pack_name"] if isinstance(row, sqlite3.Row) else row[1])
                row_namespace = str(row["namespace"] if isinstance(row, sqlite3.Row) else row[2])
                row_trust_level = str(row["trust_level"] if isinstance(row, sqlite3.Row) else row[3])
                row_imported_at = str(row["imported_at"] if isinstance(row, sqlite3.Row) else row[4])
                row_source_label = _pack_source_label_basename(row["source_label"] if isinstance(row, sqlite3.Row) else row[5])
                freshness_raw = row["freshness_summary_json"] if isinstance(row, sqlite3.Row) else row[6]
                received_zip_sha256 = normalize_optional_string(
                    row["received_zip_sha256"] if isinstance(row, sqlite3.Row) else row[7]
                ) or ""

                memory_count = 0
                topic_count = 0
                memory_file_count = 0
                if include_counts:
                    memory_count_row = conn.execute(
                        "SELECT COUNT(*) FROM imported_pack_rows WHERE pack_id = ?",
                        (row_pack_id,),
                    ).fetchone()
                    memory_count = int(memory_count_row[0] if memory_count_row else 0)

                    topic_count_row = conn.execute(
                        "SELECT COUNT(*) "
                        "FROM memory_topics mt "
                        "JOIN imported_pack_rows ipr ON ipr.memory_id = mt.memory_id "
                        "WHERE ipr.pack_id = ?",
                        (row_pack_id,),
                    ).fetchone()
                    topic_count = int(topic_count_row[0] if topic_count_row else 0)

                    memory_file_count_row = conn.execute(
                        "SELECT COUNT(*) "
                        "FROM memory_files "
                        "WHERE memory_id IN (SELECT memory_id FROM imported_pack_rows WHERE pack_id = ?)",
                        (row_pack_id,),
                    ).fetchone()
                    memory_file_count = int(memory_file_count_row[0] if memory_file_count_row else 0)

                freshness_payload: dict[str, Any] = {"by_memory": {}, "by_file": {}}
                if include_freshness:
                    parsed_freshness: dict[str, Any] | None = None
                    freshness_text = normalize_optional_string(freshness_raw)
                    if freshness_text is not None:
                        try:
                            candidate = json.loads(freshness_text)
                            if isinstance(candidate, dict):
                                parsed_freshness = candidate
                            else:
                                raise ValueError("freshness summary is not an object")
                        except Exception:
                            _pack_import_add_warning(
                                warnings,
                                "malformed_freshness_summary",
                                f"freshness_summary_json is malformed for pack {row_pack_id}",
                            )
                    if isinstance(parsed_freshness, dict):
                        by_memory = parsed_freshness.get("by_memory")
                        by_file = parsed_freshness.get("by_file")
                        freshness_payload = {
                            "by_memory": by_memory if isinstance(by_memory, dict) else {},
                            "by_file": by_file if isinstance(by_file, dict) else {},
                        }

                top_topics: list[dict[str, Any]] = []
                if include_topics:
                    top_topic_rows = conn.execute(
                        "SELECT mt.topic, COUNT(*) AS row_count "
                        "FROM memory_topics mt "
                        "JOIN imported_pack_rows ipr ON ipr.memory_id = mt.memory_id "
                        "WHERE ipr.pack_id = ? "
                        "GROUP BY mt.topic "
                        "ORDER BY row_count DESC, mt.topic ASC "
                        "LIMIT 10",
                        (row_pack_id,),
                    ).fetchall()
                    top_topics = [
                        {
                            "topic": str(topic_row["topic"] if isinstance(topic_row, sqlite3.Row) else topic_row[0]),
                            "row_count": int(
                                topic_row["row_count"] if isinstance(topic_row, sqlite3.Row) else topic_row[1]
                            ),
                        }
                        for topic_row in top_topic_rows
                    ]

                packs.append(
                    {
                        "pack_id": row_pack_id,
                        "pack_name": row_pack_name,
                        "namespace": row_namespace,
                        "trust_level": row_trust_level,
                        "imported_at": row_imported_at,
                        "source_label": row_source_label,
                        "received_zip_sha256": received_zip_sha256,
                        "memory_count": int(memory_count),
                        "topic_count": int(topic_count),
                        "memory_file_count": int(memory_file_count),
                        "freshness": freshness_payload,
                        "top_topics": top_topics,
                    }
                )
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")

    structured = {
        "action": "pack_list_imports",
        "status": "ok",
        "total": int(total),
        "limited": bool(total > limit),
        "limit": int(limit),
        "packs": packs,
        "warnings": warnings,
    }
    lines = [
        f"Imported packs: {total} (limited={str(total > limit).lower()}, limit={limit})",
        f"Returned: {len(packs)}",
    ]
    return text_result("\n".join(lines), structured)


def pack_review_import(args: dict[str, Any]) -> dict[str, Any]:
    if store_backend() != "sqlite":
        return tool_error("pack_review_import requires sqlite backend")

    pack_id = normalize_optional_string(args.get("pack_id"))
    if pack_id is None:
        return tool_error_code("pack_not_found", "pack_id is required")

    include_samples = parse_bool(args.get("include_samples"), default=True)
    include_grouped_summary = parse_bool(args.get("include_grouped_summary"), default=False)
    sample_limit = _safe_int(
        args.get("sample_limit"),
        PACK_REVIEW_SAMPLE_LIMIT_DEFAULT,
        minimum=0,
        maximum=PACK_REVIEW_SAMPLE_LIMIT_MAX,
    )
    warnings: list[dict[str, str]] = []

    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            pack_row = _get_imported_pack(conn, pack_id)
            if pack_row is None:
                return tool_error_code("pack_not_found", f"pack {pack_id} was not found")
            selection = _select_imported_pack_rows(
                conn,
                pack_id,
                args,
                warnings,
                allow_query=True,
            )
    except ValueError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")

    selected_rows = list(selection["selected_rows"])
    grouped_summary: dict[str, Any] = {}
    if include_grouped_summary:
        try:
            with _sqlite_session() as conn:
                _sqlite_ensure_schema(conn)
                grouped_summary = _pack_review_import_grouped_summary(conn, selected_rows)
        except Exception as exc:
            _pack_import_add_warning(
                warnings,
                "grouped_summary_unavailable",
                f"grouped summary unavailable: {type(exc).__name__}: {exc}",
            )
    samples: list[dict[str, Any]] = []
    if include_samples and sample_limit > 0:
        for row in selected_rows[:sample_limit]:
            samples.append(
                {
                    "row_id_in_pack": str(row.get("row_id_in_pack", "")),
                    "memory_id": str(row.get("memory_id", "")),
                    "kind": str(row.get("kind", "")),
                    "namespace": str(row.get("namespace", "")),
                    "origin": str(row.get("origin", "")),
                    "import_freshness": str(row.get("import_freshness", "unknown") or "unknown"),
                    "topics": list(row.get("topics", [])),
                    "touched_files": list(row.get("touched_files", [])),
                    "git_sha": normalize_optional_string(row.get("git_sha")),
                    "git_branch": normalize_optional_string(row.get("git_branch")),
                    "git_dirty": normalize_git_dirty(row.get("git_dirty")),
                    "promoted_to_memory_id": normalize_optional_string(row.get("promoted_to_memory_id")),
                    "promotion_id": normalize_optional_string(row.get("promotion_id")),
                    "promoted_at": normalize_optional_string(row.get("promoted_at")),
                    "preview": _pack_review_sample_preview(row),
                }
            )

    structured = {
        "action": "pack_review_import",
        "status": "ok",
        "pack": {
            "pack_id": str(pack_row["pack_id"]),
            "pack_name": str(pack_row["pack_name"]),
            "namespace": str(pack_row["namespace"]),
            "trust_level": str(pack_row["trust_level"]),
            "imported_at": str(pack_row["imported_at"]),
            "source_label": _pack_source_label_basename(pack_row["source_label"]),
            "received_zip_sha256": normalize_optional_string(pack_row["received_zip_sha256"]) or "",
        },
        "selection": {
            "total_pack_rows": int(selection["total_pack_rows"]),
            "selected_rows": int(selection["selected_total"]),
            "limited": bool(selection["limited"]),
            "limit": int(selection["limit"]),
        },
        "counts": {
            "by_kind": dict(selection["by_kind"]),
            "by_import_freshness": dict(selection["by_import_freshness"]),
            "by_topic": dict(selection["by_topic"]),
            "referenced_files": int(selection["referenced_files"]),
        },
        "files": {
            "top_referenced_files": list(selection["top_referenced_files"]),
        },
        "grouped_summary": grouped_summary if include_grouped_summary else {},
        "samples": samples if include_samples else [],
        "warnings": warnings,
    }
    lines = [
        f"Pack review: {pack_id}",
        f"Rows selected: {selection['selected_total']} (limited={str(selection['limited']).lower()}, limit={selection['limit']})",
        f"By kind: {selection['by_kind']}",
    ]
    return text_result("\n".join(lines), structured)


def pack_promote_preview(args: dict[str, Any]) -> dict[str, Any]:
    if store_backend() != "sqlite":
        return tool_error("pack_promote_preview requires sqlite backend")

    pack_id = normalize_optional_string(args.get("pack_id"))
    if pack_id is None:
        return tool_error_code("pack_not_found", "pack_id is required")

    include_samples = parse_bool(args.get("include_samples"), default=True)
    sample_limit = _safe_int(
        args.get("sample_limit"),
        PACK_REVIEW_SAMPLE_LIMIT_DEFAULT,
        minimum=0,
        maximum=PACK_REVIEW_SAMPLE_LIMIT_MAX,
    )
    warnings: list[dict[str, str]] = []

    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            pack_row = _get_imported_pack(conn, pack_id)
            if pack_row is None:
                return tool_error_code("pack_not_found", f"pack {pack_id} was not found")
            trust_level = str(pack_row["trust_level"])
            if trust_level not in {"quarantine", "trusted"}:
                return tool_error_code(
                    "unsupported_trust_level_for_promotion_preview",
                    f"pack {pack_id} has trust_level={pack_row['trust_level']}; only quarantine or trusted packs are eligible",
                )
            if trust_level == "trusted":
                _pack_import_add_warning(
                    warnings,
                    "trusted_import_source",
                    "Promotion preview source rows come from a trusted import namespace.",
                    extra={"phase": "preview"},
                )
            if not _pack_row_filters_supplied(args, include_query=False):
                _pack_import_add_warning(
                    warnings,
                    "preview_all_pack_rows",
                    "No row filters were supplied; previewing all pack rows up to the provided limit.",
                )
            selection = _select_imported_pack_rows(
                conn,
                pack_id,
                args,
                warnings,
                allow_query=False,
            )
    except ValueError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"{type(exc).__name__}: {exc}")

    if bool(selection["limited"]):
        _pack_import_add_warning(
            warnings,
            "promotion_preview_limited",
            "Preview results are limited by the provided limit.",
        )

    selected_rows = list(selection["selected_rows"])
    would_create_memory_count = len(selected_rows)
    would_copy_topic_count = 0
    would_copy_memory_file_count = 0
    candidate_rows_all: list[dict[str, Any]] = []
    for row in selected_rows:
        topics = list(row.get("topics", []))
        touched_files = list(row.get("touched_files", []))
        would_copy_topic_count += len(topics)
        would_copy_memory_file_count += len(touched_files)
        imported_memory_id = str(row.get("memory_id", ""))
        import_freshness = str(row.get("import_freshness", "unknown") or "unknown")
        candidate_rows_all.append(
            {
                "row_id_in_pack": str(row.get("row_id_in_pack", "")),
                "imported_memory_id": imported_memory_id,
                "kind": str(row.get("kind", "")),
                "import_freshness": import_freshness,
                "topics": topics,
                "git_sha": normalize_optional_string(row.get("git_sha")),
                "git_branch": normalize_optional_string(row.get("git_branch")),
                "git_dirty": normalize_git_dirty(row.get("git_dirty")),
                "would_generate_memory_id": True,
                "target_namespace": DEFAULT_MEMORY_NAMESPACE,
                "target_origin": "promoted",
                "provenance": {
                    "promoted_from_pack_id": pack_id,
                    "promoted_from_row_id_in_pack": str(row.get("row_id_in_pack", "")),
                    "promoted_from_imported_memory_id": imported_memory_id,
                    "original_import_freshness": import_freshness,
                },
            }
        )

    candidate_rows = list(candidate_rows_all)
    if len(candidate_rows) > PACK_PROMOTE_PREVIEW_CANDIDATE_OUTPUT_MAX:
        candidate_rows = candidate_rows[:PACK_PROMOTE_PREVIEW_CANDIDATE_OUTPUT_MAX]
        _pack_import_add_warning(
            warnings,
            "candidate_rows_truncated",
            f"candidate_rows output was truncated to {PACK_PROMOTE_PREVIEW_CANDIDATE_OUTPUT_MAX}",
        )

    samples: list[dict[str, Any]] = []
    if include_samples and sample_limit > 0:
        for row in selected_rows[:sample_limit]:
            samples.append(
                {
                    "row_id_in_pack": str(row.get("row_id_in_pack", "")),
                    "imported_memory_id": str(row.get("memory_id", "")),
                    "kind": str(row.get("kind", "")),
                    "import_freshness": str(row.get("import_freshness", "unknown") or "unknown"),
                    "preview": _pack_review_sample_preview(row),
                }
            )

    structured = {
        "action": "pack_promote_preview",
        "status": "ok",
        "pack": {
            "pack_id": str(pack_row["pack_id"]),
            "pack_name": str(pack_row["pack_name"]),
            "namespace": str(pack_row["namespace"]),
            "trust_level": str(pack_row["trust_level"]),
        },
        "selection": {
            "selected_rows": int(selection["selected_total"]),
            "limited": bool(selection["limited"]),
            "limit": int(selection["limit"]),
        },
        "promotion_plan": {
            "target_namespace": DEFAULT_MEMORY_NAMESPACE,
            "target_origin": "promoted",
            "would_create_memory_count": int(would_create_memory_count),
            "would_copy_topic_count": int(would_copy_topic_count),
            "would_copy_memory_file_count": int(would_copy_memory_file_count),
            "would_preserve_git_provenance": True,
            "would_preserve_pack_provenance": True,
        },
        "candidate_rows": candidate_rows,
        "samples": samples if include_samples else [],
        "warnings": warnings,
    }
    lines = [
        f"Pack promotion preview: {pack_id}",
        f"Selected rows: {selection['selected_total']} (limited={str(selection['limited']).lower()}, limit={selection['limit']})",
        f"Would create: {would_create_memory_count} local promoted rows",
    ]
    return text_result("\n".join(lines), structured)


def pack_promote(args: dict[str, Any]) -> dict[str, Any]:
    if store_backend() != "sqlite":
        return tool_error("pack_promote requires sqlite backend")

    pack_id = normalize_optional_string(args.get("pack_id"))
    if pack_id is None:
        return tool_error_code("pack_not_found", "pack_id is required")

    if "query" in args and args.get("query") is not None:
        return tool_error_code(
            "query_filter_not_allowed_for_promotion",
            "query filter is not allowed for pack_promote; use explicit row filters.",
        )

    confirm_promote = parse_bool(args.get("confirm_promote"), default=False)
    if not confirm_promote:
        return tool_error_code(
            "confirm_promote_required",
            "pack_promote requires confirm_promote=true to proceed.",
        )

    allow_promote_all = parse_bool(args.get("allow_promote_all"), default=False)
    allow_limited_promotion = parse_bool(args.get("allow_limited_promotion"), default=False)
    warnings: list[dict[str, str]] = []

    if not _pack_row_filters_supplied(args, include_query=False) and not allow_promote_all:
        return _pack_error_with_warnings(
            "promote_all_requires_explicit_allow",
            "No row filters were supplied; pass allow_promote_all=true to promote all selected rows.",
            warnings,
        )

    promoted_rows_all: list[dict[str, Any]] = []
    memory_count = 0
    topic_count = 0
    memory_file_count = 0
    mapping_count = 0
    promoted_at = now_iso()
    promotion_id = _pack_make_promotion_id()
    source_namespace = ""
    pack_name = ""
    selected_rows: list[dict[str, Any]] = []
    selected_total_count = 0
    limited = False
    limit = PACK_REVIEW_LIMIT_DEFAULT

    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            pack_row = _get_imported_pack(conn, pack_id)
            if pack_row is None:
                return tool_error_code("pack_not_found", f"pack {pack_id} was not found")

            trust_level = str(pack_row["trust_level"])
            source_namespace = normalize_optional_string(pack_row["namespace"]) or ""
            pack_name = str(pack_row["pack_name"])
            if trust_level not in {"quarantine", "trusted"}:
                return tool_error_code(
                    "unsupported_trust_level_for_promotion",
                    f"pack {pack_id} has trust_level={trust_level}; only quarantine or trusted packs are eligible",
                )
            if trust_level == "quarantine":
                if not source_namespace.startswith(PACK_QUARANTINE_PREFIX):
                    return tool_error_code(
                        "namespace_trust_invariant",
                        "quarantine promotion requires namespace prefix pack:quarantine:",
                    )
            elif trust_level == "trusted":
                if not source_namespace.startswith(PACK_TRUSTED_PREFIX):
                    return tool_error_code(
                        "namespace_trust_invariant",
                        "trusted promotion requires namespace prefix pack:trusted:",
                    )
                _pack_import_add_warning(
                    warnings,
                    "trusted_import_source",
                    "Promotion source rows come from a trusted import namespace.",
                    extra={"phase": "promotion"},
                )

            source_signer_id: str | None = None
            source_secret_fingerprint: str | None = None
            if trust_level == "trusted":
                manifest_payload = {}
                try:
                    manifest_raw = normalize_optional_string(pack_row["manifest_json"])
                    if manifest_raw:
                        parsed_manifest = json.loads(manifest_raw)
                        if isinstance(parsed_manifest, dict):
                            manifest_payload = parsed_manifest
                except Exception:
                    manifest_payload = {}
                signature_payload = manifest_payload.get("signature") if isinstance(manifest_payload, dict) else None
                if isinstance(signature_payload, dict):
                    source_signer_id = normalize_optional_string(signature_payload.get("signer_id"))
                    source_secret_fingerprint = normalize_optional_string(signature_payload.get("secret_fingerprint"))

            selection = _select_imported_pack_rows(
                conn,
                pack_id,
                args,
                warnings,
                allow_query=False,
            )
            selected_total = int(selection["selected_total"])
            selected_total_count = selected_total
            limit = int(selection["limit"])
            limited = bool(selection["limited"])
            if selected_total <= 0:
                return _pack_error_with_warnings(
                    "selected_rows_empty",
                    f"No rows matched promotion filters for pack {pack_id}.",
                    warnings,
                )
            if limited and not allow_limited_promotion:
                return _pack_error_with_warnings(
                    "limited_promotion_requires_explicit_allow",
                    "Selection exceeds limit; increase limit or pass allow_limited_promotion=true.",
                    warnings,
                )
            if limited and allow_limited_promotion:
                _pack_import_add_warning(
                    warnings,
                    "limited_promotion",
                    "Only the limited selected row set was promoted.",
                )

            selected_rows = list(selection["selected_rows"])
            allowed_kinds = set(PACK_EXPORT_ALLOWED_KINDS)
            invalid_kinds = sorted({str(row.get("kind", "")) for row in selected_rows if str(row.get("kind", "")) not in allowed_kinds})
            if invalid_kinds:
                raise PackPromoteError(
                    "unsupported_kind_for_promotion",
                    f"unsupported kind(s) for promotion: {', '.join(invalid_kinds)}",
                )

            # Promotion only accepts mapped imported rows for this pack namespace.
            for row in selected_rows:
                row_namespace = normalize_optional_string(row.get("namespace")) or ""
                row_origin = normalize_optional_string(row.get("origin")) or ""
                if row_namespace != source_namespace or row_origin != "imported":
                    raise PackPromoteError(
                        "ineligible_source_row",
                        "selected rows must be imported rows mapped to the target pack namespace",
                    )

            selected_row_ids = [str(row.get("row_id_in_pack", "")) for row in selected_rows]
            if selected_row_ids:
                placeholders = ",".join("?" for _ in selected_row_ids)
                duplicate_rows = conn.execute(
                    f"""
                    SELECT row_id_in_pack, imported_memory_id, promoted_memory_id, promotion_id
                    FROM promoted_pack_rows
                    WHERE pack_id = ? AND row_id_in_pack IN ({placeholders})
                    ORDER BY row_id_in_pack ASC
                    """,
                    tuple([pack_id] + selected_row_ids),
                ).fetchall()
            else:
                duplicate_rows = []
            if duplicate_rows:
                duplicate_details = [
                    {
                        "row_id_in_pack": str(row["row_id_in_pack"] if isinstance(row, sqlite3.Row) else row[0]),
                        "imported_memory_id": str(row["imported_memory_id"] if isinstance(row, sqlite3.Row) else row[1]),
                        "promoted_memory_id": str(row["promoted_memory_id"] if isinstance(row, sqlite3.Row) else row[2]),
                        "promotion_id": normalize_optional_string(row["promotion_id"] if isinstance(row, sqlite3.Row) else row[3]),
                    }
                    for row in duplicate_rows[:50]
                ]
                return _pack_error_with_warnings(
                    "pack_rows_already_promoted",
                    "One or more selected pack rows were already promoted.",
                    warnings,
                    extra={
                        "already_promoted_rows": duplicate_details,
                        "already_promoted_rows_truncated": bool(len(duplicate_rows) > 50),
                    },
                )

            filters_payload = {
                "topics": list(selection["topics_filter"]),
                "kinds": list(selection["kinds_filter"]),
                "import_freshness": list(selection["import_freshness_filter"]),
                "row_ids": list(selection["row_ids_filter"]),
                "memory_ids": list(selection["memory_ids_input"]),
                "touched_paths": list(selection["touched_paths_filter"]),
                "limit": int(limit),
            }
            conn.execute(
                """
                INSERT INTO promotion_audit(
                    promotion_id, pack_id, promoted_at, filters_json, row_count,
                    limited, allow_promote_all, allow_limited_promotion
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    promotion_id,
                    pack_id,
                    promoted_at,
                    json.dumps(filters_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    int(len(selected_rows)),
                    1 if limited else 0,
                    1 if allow_promote_all else 0,
                    1 if allow_limited_promotion else 0,
                ),
            )

            for row in selected_rows:
                row_id_in_pack = str(row.get("row_id_in_pack", ""))
                imported_memory_id = str(row.get("memory_id", ""))
                kind_name = str(row.get("kind", ""))
                text_value = str(row.get("text", ""))
                title_value = normalize_optional_string(row.get("title"))
                import_freshness_value = str(row.get("import_freshness", "unknown") or "unknown")

                metadata: dict[str, Any] = {
                    "pack_promotion": {
                        "promoted_from_pack_id": pack_id,
                        "promoted_from_row_id_in_pack": row_id_in_pack,
                        "promoted_from_imported_memory_id": imported_memory_id,
                        "promotion_id": promotion_id,
                        "promoted_at": promoted_at,
                        "original_import_freshness": import_freshness_value,
                        "promotion_source": "pack_promote",
                        "source_trust_level": trust_level,
                    }
                }
                if trust_level == "trusted":
                    if source_signer_id is not None:
                        metadata["pack_promotion"]["source_signer_id"] = source_signer_id
                    if source_secret_fingerprint is not None:
                        metadata["pack_promotion"]["source_secret_fingerprint"] = source_secret_fingerprint
                if title_value is not None:
                    metadata["title"] = title_value

                promoted_memory_id = make_id(f"{pack_id}:{row_id_in_pack}:{imported_memory_id}:{promotion_id}")
                promoted_memory = new_memory(
                    promoted_memory_id,
                    kind_name,
                    text_value,
                    source=f"pack_promote:{pack_id}",
                    tags=[],
                    linked_ids=[],
                    git_sha=normalize_optional_string(row.get("git_sha")),
                    git_branch=normalize_optional_string(row.get("git_branch")),
                    git_dirty=normalize_git_dirty(row.get("git_dirty")),
                    namespace=DEFAULT_MEMORY_NAMESPACE,
                    origin="promoted",
                    import_freshness=import_freshness_value,
                    metadata=metadata,
                )
                _sqlite_upsert_memory(
                    conn,
                    promoted_memory,
                    respect_provided_git_on_new=True,
                    store_touched_files=False,
                )
                memory_count += 1

                for topic_value in list(row.get("topics", [])):
                    topic_text = normalize_optional_string(topic_value)
                    if topic_text is None:
                        continue
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO memory_topics(memory_id, topic, created_at, source)
                        VALUES(?, ?, ?, ?)
                        """,
                        (promoted_memory_id, topic_text, promoted_at, "promotion"),
                    )
                    topic_count += max(0, int(cur.rowcount))

                seen_file_keys: set[tuple[str, str]] = set()
                for file_item in list(row.get("touched_files", [])):
                    if not isinstance(file_item, dict):
                        continue
                    path_text = normalize_optional_string(file_item.get("path"))
                    file_sha_text = normalize_optional_string(file_item.get("file_sha"))
                    if path_text is None or file_sha_text is None:
                        continue
                    file_key = (path_text, file_sha_text)
                    if file_key in seen_file_keys:
                        continue
                    seen_file_keys.add(file_key)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO memory_files(memory_table, memory_id, path, file_sha)
                        VALUES(?, ?, ?, ?)
                        """,
                        (kind_name, promoted_memory_id, path_text, file_sha_text),
                    )
                    memory_file_count += 1

                conn.execute(
                    """
                    INSERT INTO promoted_pack_rows(
                        pack_id, row_id_in_pack, imported_memory_id, promoted_memory_id,
                        kind, promoted_at, original_import_freshness, promotion_id
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pack_id,
                        row_id_in_pack,
                        imported_memory_id,
                        promoted_memory_id,
                        kind_name,
                        promoted_at,
                        import_freshness_value,
                        promotion_id,
                    ),
                )
                mapping_count += 1
                promoted_rows_all.append(
                    {
                        "row_id_in_pack": row_id_in_pack,
                        "imported_memory_id": imported_memory_id,
                        "promoted_memory_id": promoted_memory_id,
                        "kind": kind_name,
                        "promotion_id": promotion_id,
                        "original_import_freshness": import_freshness_value,
                    }
                )
    except PackPromoteError as exc:
        return _pack_error_with_warnings(exc.code, exc.message, warnings)
    except sqlite3.IntegrityError as exc:
        return _pack_error_with_warnings("pack_promote_integrity_error", str(exc), warnings)
    except Exception as exc:
        return _pack_error_with_warnings("pack_promote_failed", f"{type(exc).__name__}: {exc}", warnings)

    output_rows = list(promoted_rows_all)
    if len(output_rows) > PACK_PROMOTE_OUTPUT_MAX_ROWS:
        output_rows = output_rows[:PACK_PROMOTE_OUTPUT_MAX_ROWS]
        _pack_import_add_warning(
            warnings,
            "promoted_rows_truncated",
            f"promoted_rows output truncated to {PACK_PROMOTE_OUTPUT_MAX_ROWS}",
        )

    structured = {
        "action": "pack_promote",
        "status": "ok",
        "promotion_id": promotion_id,
        "pack_id": pack_id,
        "pack_name": pack_name,
        "promoted_at": promoted_at,
        "source_namespace": source_namespace,
        "target_namespace": DEFAULT_MEMORY_NAMESPACE,
        "target_origin": "promoted",
        "selection": {
            "selected_rows": int(selected_total_count),
            "promoted_rows": int(memory_count),
            "limited": bool(limited),
            "limit": int(limit),
        },
        "promoted": {
            "memory_count": int(memory_count),
            "topic_count": int(topic_count),
            "memory_file_count": int(memory_file_count),
            "mapping_count": int(mapping_count),
        },
        "promoted_rows": output_rows,
        "warnings": warnings,
    }
    lines = [
        f"Pack promoted: {pack_id}",
        f"Promotion id: {promotion_id}",
        f"Promoted rows: {memory_count} (limited={str(limited).lower()}, limit={limit})",
    ]
    return text_result("\n".join(lines), structured)


def pack_inspect(args: dict[str, Any]) -> dict[str, Any]:
    include_samples = parse_bool(args.get("include_samples"), default=False)
    sample_limit = _safe_int(args.get("sample_limit"), 5, minimum=0, maximum=200)
    if sample_limit > 20:
        sample_limit = 20
    verification_secret = normalize_optional_string(args.get("verification_secret"))
    if verification_secret is not None and len(verification_secret) < PACK_SECRET_MIN_LENGTH:
        return tool_error_code(
            "secret_too_short",
            f"verification_secret must be at least {PACK_SECRET_MIN_LENGTH} characters",
        )

    pack_path_text = normalize_optional_string(args.get("pack_path"))
    if pack_path_text is None:
        payload = _pack_inspect_default()
        _pack_inspect_error(payload, "missing_pack_path", "pack_path is required")
        finalized = _pack_inspect_finalize(payload, include_samples=include_samples, sample_limit=sample_limit)
        return text_result(_pack_inspect_text(finalized, "missing"), finalized)

    pack_path = Path(pack_path_text).expanduser().resolve()
    if not pack_path.exists():
        payload = _pack_inspect_default()
        _pack_inspect_error(payload, "pack_path_not_found", f"pack path not found: {pack_path}")
        finalized = _pack_inspect_finalize(payload, include_samples=include_samples, sample_limit=sample_limit)
        return text_result(_pack_inspect_text(finalized, pack_path.name), finalized)
    if not pack_path.is_file():
        payload = _pack_inspect_default()
        _pack_inspect_error(payload, "pack_path_not_file", f"pack path is not a file: {pack_path}")
        finalized = _pack_inspect_finalize(payload, include_samples=include_samples, sample_limit=sample_limit)
        return text_result(_pack_inspect_text(finalized, pack_path.name), finalized)

    legacy_zip_suffix_warning, nonstandard_suffix_warning = _pack_suffix_flags(pack_path)
    try:
        snapshot = _load_pack_snapshot(pack_path)
    except PackSnapshotError as exc:
        payload = _pack_inspect_default()
        _pack_inspect_add_suffix_warning(
            payload,
            legacy_zip_suffix_warning=legacy_zip_suffix_warning,
            nonstandard_suffix_warning=nonstandard_suffix_warning,
        )
        _pack_inspect_error(payload, exc.code, exc.message)
        finalized = _pack_inspect_finalize(payload, include_samples=include_samples, sample_limit=sample_limit)
        return text_result(_pack_inspect_text(finalized, pack_path.name), finalized)

    finalized = _inspect_pack_snapshot(
        snapshot,
        include_samples=include_samples,
        sample_limit=sample_limit,
        verification_secret=verification_secret,
        legacy_zip_suffix_warning=legacy_zip_suffix_warning,
        nonstandard_suffix_warning=nonstandard_suffix_warning,
    )
    return text_result(_pack_inspect_text(finalized, pack_path.name), finalized)


def pack_import(args: dict[str, Any]) -> dict[str, Any]:
    if store_backend() != "sqlite":
        return tool_error("pack_import requires sqlite backend")
    allow_unsigned_quarantine = parse_bool(args.get("allow_unsigned_quarantine"), default=False)
    allow_trusted_import = parse_bool(args.get("allow_trusted_import"), default=False)
    if allow_unsigned_quarantine and allow_trusted_import:
        return tool_error_code(
            "ambiguous_import_target",
            "allow_unsigned_quarantine and allow_trusted_import cannot both be true.",
        )
    if not allow_unsigned_quarantine and not allow_trusted_import:
        return tool_error_code("import_target_not_allowed", PACK_IMPORT_TARGET_NOT_ALLOWED_ERROR)

    verification_secret = normalize_optional_string(args.get("verification_secret"))
    verification_secret_unused_for_quarantine = bool(
        not allow_trusted_import and verification_secret is not None
    )
    if allow_trusted_import:
        if verification_secret is None:
            return tool_error_code(
                "trusted_import_requires_verification_secret",
                "allow_trusted_import=true requires verification_secret.",
            )
        if len(verification_secret) < PACK_SECRET_MIN_LENGTH:
            return tool_error_code(
                "secret_too_short",
                f"verification_secret must be at least {PACK_SECRET_MIN_LENGTH} characters",
                details={"field": "verification_secret"},
            )

    pack_path_text = normalize_optional_string(args.get("pack_path"))
    if pack_path_text is None:
        return tool_error_code("missing_pack_path", "pack_path is required")
    pack_path = Path(pack_path_text).expanduser().resolve()
    if not pack_path.exists():
        return tool_error_code("pack_path_not_found", f"pack path not found: {pack_path}")
    if not pack_path.is_file():
        return tool_error_code("pack_path_not_file", f"pack path is not a file: {pack_path}")

    try:
        snapshot = _load_pack_snapshot(pack_path)
    except PackSnapshotError as exc:
        return tool_error_code(exc.code, exc.message)
    legacy_zip_suffix_warning, nonstandard_suffix_warning = _pack_suffix_flags(pack_path)

    inspection = _inspect_pack_snapshot(
        snapshot,
        include_samples=False,
        sample_limit=0,
        verification_secret=verification_secret if allow_trusted_import else None,
        legacy_zip_suffix_warning=legacy_zip_suffix_warning,
        nonstandard_suffix_warning=nonstandard_suffix_warning,
    )
    # verification_secret is sensitive and not needed after classification.
    verification_secret = None
    inspection_status = str(inspection.get("status", "invalid"))
    signature_payload = inspection.get("signature", {}) if isinstance(inspection.get("signature"), dict) else {}
    trust_classification = str(signature_payload.get("trust_classification", "unsigned") or "unsigned")
    trusted_import_available = bool(inspection.get("trusted_import_available", False))
    if allow_trusted_import:
        if (
            inspection_status != "valid"
            or
            signature_payload.get("present") is not True
            or signature_payload.get("verified") is not True
            or trust_classification != "trusted_signer"
            or not trusted_import_available
        ):
            return tool_error_code(
                "trusted_import_requires_verified_trusted_signer",
                "trusted import requires pack_inspect classification trusted_signer with verified signature.",
            )
    else:
        if inspection_status != "valid":
            return tool_error_code(
                "pack_validation_failed",
                f"pack validation failed with status={inspection_status}",
            )
        if str(inspection.get("import_recommendation", "reject")) != "quarantine_only":
            return tool_error_code(
                "pack_validation_failed",
                f"pack import recommendation is {inspection.get('import_recommendation')}; expected quarantine_only",
            )

    validation = inspection.get("validation", {}) if isinstance(inspection.get("validation"), dict) else {}
    required_true_flags = [
        "required_members_present",
        "json_members_parse",
        "jsonl_rows_parse",
        "row_count_matches_manifest",
        "no_source_memory_ids",
        "redaction_metadata_valid",
        "content_hash_valid",
        "covered_members_valid",
        "safe_zip_members",
        "supported_schema",
        "supported_signature_state",
        "signature_valid",
    ]
    for flag_name in required_true_flags:
        if not bool(validation.get(flag_name, False)):
            return tool_error_code("pack_validation_failed", f"validation flag {flag_name} is false")

    raw_member_bytes = snapshot.get("required_member_bytes", {})
    if not isinstance(raw_member_bytes, dict):
        return tool_error_code("pack_snapshot_invalid", "pack snapshot is missing required member bytes")
    try:
        manifest = _pack_inspect_parse_json(raw_member_bytes["manifest.json"], "manifest.json")
        pack_rows = _pack_inspect_rows_from_jsonl(raw_member_bytes["content/memories.jsonl"])
    except Exception as exc:
        return tool_error_code("pack_snapshot_parse_error", f"{type(exc).__name__}: {exc}")

    if not pack_rows:
        return tool_error_code("empty_pack_rows", "pack has no importable rows")

    pack_id = str(manifest.get("pack_id", "") or "")
    pack_name = str(manifest.get("pack_name", "") or "")
    if allow_trusted_import:
        trust_level = "trusted"
        target_namespace = f"{PACK_TRUSTED_PREFIX}{pack_id}"
        if trust_level != "trusted" or not target_namespace.startswith(PACK_TRUSTED_PREFIX):
            return tool_error_code(
                "namespace_trust_invariant",
                "trust_level=trusted requires namespace prefix pack:trusted:",
            )
    else:
        trust_level = "quarantine"
        target_namespace = f"{PACK_QUARANTINE_PREFIX}{pack_id}"
        if trust_level != "quarantine" or not target_namespace.startswith(PACK_QUARANTINE_PREFIX):
            return tool_error_code(
                "namespace_trust_invariant",
                "trust_level=quarantine requires namespace prefix pack:quarantine:",
            )

    source_label = pack_path.name
    received_zip_sha256 = str(snapshot.get("received_zip_sha256", "") or "")
    if not received_zip_sha256:
        return tool_error_code("missing_received_zip_sha256", "received_zip_sha256 could not be computed")

    warnings: list[dict[str, str]] = []
    if verification_secret_unused_for_quarantine:
        _pack_import_add_warning(
            warnings,
            "verification_secret_unused_for_quarantine_import",
            "verification_secret was supplied but ignored because allow_trusted_import is false.",
        )
    for item in inspection.get("warnings", []):
        if isinstance(item, dict):
            _pack_import_add_warning(warnings, str(item.get("code", "")), str(item.get("message", "")))

    imported_rows_all: list[dict[str, Any]] = []
    memory_count = 0
    topic_count = 0
    memory_file_count = 0
    mapping_count = 0
    by_memory = {name: 0 for name in PACK_IMPORT_FRESHNESS_VALUES}
    by_file = {name: 0 for name in PACK_IMPORT_FRESHNESS_VALUES}
    imported_at = now_iso()
    repo_root = _git_repo_root()

    try:
        with _sqlite_session() as conn:
            _sqlite_ensure_schema(conn)
            existing_row = conn.execute(
                "SELECT received_zip_sha256 FROM imported_packs WHERE pack_id = ?",
                (pack_id,),
            ).fetchone()
            if existing_row is not None:
                stored_sha = normalize_optional_string(
                    existing_row["received_zip_sha256"] if isinstance(existing_row, sqlite3.Row) else existing_row[0]
                )
                if stored_sha:
                    if stored_sha == received_zip_sha256:
                        raise PackImportError(
                            "pack_already_imported",
                            f"pack {pack_id} with matching received_zip_sha256 is already imported",
                        )
                    raise PackImportError(
                        "pack_id_collision_distinct_content",
                        f"pack_id {pack_id} already exists with different received_zip_sha256",
                    )
                raise PackImportError(
                    "pack_already_imported_legacy_unknown_hash",
                    f"pack {pack_id} already exists but stored hash is unavailable",
                )

            for row in pack_rows:
                row_id_in_pack = str(row.get("row_id_in_pack", ""))
                kind_name = str(row.get("kind", ""))
                if kind_name not in PACK_EXPORT_ALLOWED_KINDS:
                    raise PackImportError(
                        "non_exportable_kind",
                        f"kind '{kind_name}' is previewable but not importable in this phase",
                    )
                text_fields = row.get("text_fields")
                if not isinstance(text_fields, dict):
                    raise PackImportError("text_fields_type", f"row {row_id_in_pack} text_fields must be an object")

                for key in sorted(text_fields):
                    if str(key) not in PACK_REDACTION_TEXT_FIELDS:
                        _pack_import_add_warning(
                            warnings,
                            PACK_IMPORT_UNKNOWN_TEXT_FIELD_WARNING_CODE,
                            f"row {row_id_in_pack} skipped unknown text field: {key}",
                        )

                text_value_raw = text_fields.get("text")
                text_value = str(text_value_raw) if text_value_raw is not None else ""
                title_value = normalize_optional_string(text_fields.get("title")) if "title" in text_fields else None
                touched_files_raw = row.get("touched_files")
                if not isinstance(touched_files_raw, list):
                    raise PackImportError("touched_files_type", f"row {row_id_in_pack} touched_files must be a list")
                touched_files: list[dict[str, Any]] = [item for item in touched_files_raw if isinstance(item, dict)]
                memory_freshness = _pack_import_memory_freshness(repo_root, touched_files, by_file)
                by_memory[memory_freshness] = int(by_memory.get(memory_freshness, 0) + 1)

                metadata: dict[str, Any] = {
                    "pack_import": {
                        "pack_id": pack_id,
                        "row_id_in_pack": row_id_in_pack,
                        "pack_name": pack_name,
                        "created_at_in_source": normalize_optional_string(row.get("created_at_in_source")),
                        "redaction_applied": bool(row.get("redaction_applied", False)),
                    }
                }
                if "title" in text_fields:
                    metadata["title"] = title_value

                memory_id = make_id(f"{pack_id}:{row_id_in_pack}:{text_value}")
                memory = new_memory(
                    memory_id,
                    kind_name,
                    text_value,
                    source=f"pack_import:{pack_id}",
                    tags=[],
                    linked_ids=[],
                    git_sha=normalize_optional_string(row.get("git_sha_at_write")),
                    git_branch=normalize_optional_string(row.get("git_branch_at_write")),
                    git_dirty=normalize_git_dirty(row.get("git_dirty_at_write")),
                    namespace=target_namespace,
                    origin="imported",
                    import_freshness=memory_freshness,
                    metadata=metadata,
                )
                _sqlite_upsert_memory(
                    conn,
                    memory,
                    respect_provided_git_on_new=True,
                    store_touched_files=False,
                )
                memory_count += 1

                topics_value = row.get("topics")
                if not isinstance(topics_value, list):
                    raise PackImportError("topics_type", f"row {row_id_in_pack} topics must be a list")
                for topic_value_raw in topics_value:
                    topic_value = normalize_optional_string(topic_value_raw)
                    if topic_value is None:
                        continue
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO memory_topics(memory_id, topic, created_at, source)
                        VALUES(?, ?, ?, ?)
                        """,
                        (memory_id, topic_value, imported_at, "pack_import"),
                    )
                    topic_count += max(0, int(cur.rowcount))

                seen_file_keys: set[tuple[str, str]] = set()
                for file_item in touched_files:
                    path_text = normalize_optional_string(file_item.get("path"))
                    file_sha_text = normalize_optional_string(file_item.get("file_sha"))
                    if path_text is None or file_sha_text is None:
                        continue
                    file_key = (path_text, file_sha_text)
                    if file_key in seen_file_keys:
                        continue
                    seen_file_keys.add(file_key)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO memory_files(memory_table, memory_id, path, file_sha)
                        VALUES(?, ?, ?, ?)
                        """,
                        (kind_name, memory_id, path_text, file_sha_text),
                    )
                    memory_file_count += 1

                conn.execute(
                    """
                    INSERT INTO imported_pack_rows(pack_id, row_id_in_pack, memory_id, kind, imported_at)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (pack_id, row_id_in_pack, memory_id, kind_name, imported_at),
                )
                mapping_count += 1
                imported_rows_all.append(
                    {
                        "row_id_in_pack": row_id_in_pack,
                        "memory_id": memory_id,
                        "kind": kind_name,
                        "import_freshness": memory_freshness,
                    }
                )

            if memory_count <= 0:
                raise PackImportError("empty_pack_rows", "pack has no importable rows")

            freshness_summary = {
                "by_memory": {name: int(by_memory.get(name, 0)) for name in PACK_IMPORT_FRESHNESS_VALUES},
                "by_file": {name: int(by_file.get(name, 0)) for name in PACK_IMPORT_FRESHNESS_VALUES},
            }
            conn.execute(
                """
                INSERT INTO imported_packs(
                    pack_id, pack_name, source_label, trust_level, namespace,
                    imported_at, manifest_json, freshness_summary_json, received_zip_sha256
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pack_id,
                    pack_name,
                    source_label,
                    trust_level,
                    target_namespace,
                    imported_at,
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    json.dumps(freshness_summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    received_zip_sha256,
                ),
            )
    except PackImportError as exc:
        return tool_error_code(exc.code, exc.message)
    except sqlite3.IntegrityError as exc:
        return tool_error_code("pack_import_integrity_error", str(exc))
    except Exception as exc:
        return tool_error_code("pack_import_failed", f"{type(exc).__name__}: {exc}")

    output_rows = list(imported_rows_all)
    if len(output_rows) > PACK_IMPORT_OUTPUT_MAX_ROWS:
        output_rows = output_rows[:PACK_IMPORT_OUTPUT_MAX_ROWS]
        _pack_import_add_warning(
            warnings,
            PACK_IMPORT_OUTPUT_TRUNCATED_WARNING_CODE,
            f"imported_rows output truncated to {PACK_IMPORT_OUTPUT_MAX_ROWS}",
        )

    freshness_summary = {
        "by_memory": {name: int(by_memory.get(name, 0)) for name in PACK_IMPORT_FRESHNESS_VALUES},
        "by_file": {name: int(by_file.get(name, 0)) for name in PACK_IMPORT_FRESHNESS_VALUES},
    }
    structured = {
        "action": "pack_import",
        "status": "ok",
        "pack_id": pack_id,
        "pack_name": pack_name,
        "namespace": target_namespace,
        "trust_level": trust_level,
        "imported_at": imported_at,
        "received_zip_sha256": received_zip_sha256,
        "imported": {
            "memory_count": int(memory_count),
            "topic_count": int(topic_count),
            "memory_file_count": int(memory_file_count),
            "mapping_count": int(mapping_count),
        },
        "freshness": freshness_summary,
        "imported_rows": output_rows,
        "warnings": warnings,
    }
    lines = [
        f"Pack imported: {pack_id} ({pack_name or 'unknown'})",
        f"Namespace: {target_namespace}  Trust: {trust_level}",
        f"Imported rows: {memory_count} memories, {topic_count} topics, {memory_file_count} file links",
        f"received_zip_sha256: {received_zip_sha256}",
    ]
    return text_result("\n".join(lines), structured)

GATEWAY_ACTIONS: dict[str, Any] = {
    "doctor": mnemo_doctor,
    "search": search_memories,
    "salience_check": memory_salience_check,
    "memory_group_discover": memory_group_discover,
    "memory_group_preview": memory_group_preview,
    "pack_landing_list": pack_landing_list,
    "pack_list_imports": pack_list_imports,
    "pack_review_import": pack_review_import,
    "pack_promote_preview": pack_promote_preview,
    "pack_promote": pack_promote,
    "pack_preview": pack_preview,
    "pack_redaction_preview": pack_redaction_preview,
    "pack_export": pack_export,
    "pack_inspect": pack_inspect,
    "pack_import": pack_import,
    "signer_add": signer_add,
    "signer_list": signer_list,
    "signer_disable": signer_disable,
    "signer_enable": signer_enable,
    "record": record_memory,
    "alias_hint": memory_alias_hint,
    "topic_add": topic_add,
    "topic_remove": topic_remove,
    "topic_list": topic_list,
    "link": memory_link,
    "recall": memory_recall,
    "get": memory_get,
    "export": memory_export,
    "update": update_memory,
    "delete": delete_memory,
    "recent": recent_memories,
    "recent_events": recent_events,
    "search_events": search_events,
    "get_event": get_event,
    "memory_events": memory_events,
    "compact_context": compact_context,
    "inspect": memory_inspect,
    "maintenance": memory_maintenance,
    "backfill_signatures": memory_backfill_signatures_gateway,
    "consolidate_full": memory_consolidate_full_gateway,
    "lookup_symbol": lookup_symbol,
}
# v0.13.0: backfill_signatures and consolidate_full are available both as
# maintenance sub-actions and as top-level gateway aliases for schema discoverability.


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
        "title": "Mnemo Memory Gateway",
        "description": (
            "Mnemo project-memory gateway; not Copilot native memory. "
            "Actions: doctor, record, alias_hint, topic_add, topic_remove, topic_list, memory_group_discover, memory_group_preview, pack_landing_list, pack_list_imports, pack_review_import, pack_promote_preview, pack_promote, pack_preview, pack_redaction_preview, pack_export, pack_inspect, pack_import, signer_add, signer_list, signer_disable, signer_enable, search, recall, get, link, export, "
            "recent_events, search_events, get_event, memory_events, "
            "maintenance(compact_logs, consolidate, consolidate_full, import_json, backfill_signatures, propose_aliases, list_alias_proposals, approve_alias, reject_alias_proposal, list_aliases, disable_alias, disable_alias_concept), "
            "inspect, lookup_symbol, salience_check, update, delete, recent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": sorted(GATEWAY_ACTIONS),
                    "description": "Required action name. Use maintenance for compact_logs/consolidate/import_json/propose_aliases/list_alias_proposals/approve_alias/reject_alias_proposal/list_aliases/disable_alias/disable_alias_concept; topic_add/topic_remove/topic_list/memory_group_discover/memory_group_preview/pack_landing_list/pack_list_imports/pack_review_import/pack_promote_preview/pack_promote/pack_preview/pack_redaction_preview/pack_export/pack_inspect/pack_import/signer_add/signer_list/signer_disable/signer_enable are first-class actions.",
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
        "namespace": "Optional namespace filter. Cannot be combined with namespaces.",
        "namespaces": "Optional namespace list filter. Defaults to ['local'] when namespace filters are omitted.",
        "include_imported": "When true, trusted imported namespaces are added to scope (sqlite backend).",
        "include_quarantine": "When true, quarantine namespaces are added to scope (sqlite backend).",
        "origin": "Optional origin filter applied only when provided.",
        "origins": "Optional origin-list filter applied only when provided.",
        "max_tokens": "Range 1-100000 when provided.",
    },
    "mnemo_salience_check": {
        "limit": "Range 1-50. When omitted, 5 is used.",
        "include_deleted": "When omitted, false is used.",
        "include_superseded": "When omitted, false is used.",
        "namespace": "Optional namespace filter. Cannot be combined with namespaces.",
        "namespaces": "Optional namespace list filter. Defaults to ['local'] when namespace filters are omitted.",
        "include_imported": "When true, trusted imported namespaces are added to scope (sqlite backend).",
        "include_quarantine": "When true, quarantine namespaces are added to scope (sqlite backend).",
        "origin": "Optional origin filter applied only when provided.",
        "origins": "Optional origin-list filter applied only when provided.",
        "threshold": "When omitted, 0.70 is used. Range 0.0-1.0.",
    },
    "mnemo_record": {
        "kind": "When omitted, note is used.",
        "tags": "When omitted, an empty list is used.",
        "references": "When omitted, an empty list is used.",
        "linked_ids": "When omitted, an empty list is used.",
        "evidence_ids": "When omitted, an empty list is used.",
        "namespace": "When omitted, local is used.",
        "origin": "When omitted, local is used.",
        "touched_files": "Optional array of workspace file paths touched during this turn.",
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
        "namespace": "Optional namespace filter. Cannot be combined with namespaces.",
        "namespaces": "Optional namespace list filter. Defaults to ['local'] when namespace filters are omitted.",
        "include_imported": "When true, trusted imported namespaces are added to scope (sqlite backend).",
        "include_quarantine": "When true, quarantine namespaces are added to scope (sqlite backend).",
        "origin": "Optional origin filter applied only when provided.",
        "origins": "Optional origin-list filter applied only when provided.",
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
        "namespace": "Optional namespace filter. Cannot be combined with namespaces.",
        "namespaces": "Optional namespace list filter. Defaults to ['local'] when namespace filters are omitted.",
        "include_imported": "When true, trusted imported namespaces are added to scope (sqlite backend).",
        "include_quarantine": "When true, quarantine namespaces are added to scope (sqlite backend).",
        "origin": "Optional origin filter applied only when provided.",
        "origins": "Optional origin-list filter applied only when provided.",
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
        "action": "Allowed values: compact_logs, consolidate, consolidate_full, import_json, backfill_signatures, propose_aliases, list_alias_proposals, approve_alias, reject_alias_proposal, list_aliases, disable_alias, disable_alias_concept.",
        "older_than_count": "compact_logs: range 1-500. When omitted, 20 is used.",
        "max_logs": "compact_logs: range 1-200. When omitted, 50 is used.",
        "threshold": "Range 0.5-1.0. When omitted, env fallback 0.7 is used.",
        "dry_run": "When omitted, true is used.",
        "window_days": "propose_aliases: range 1-365. When omitted, 30 is used.",
        "min_recurrence": "propose_aliases: range 1-100. When omitted, 3 is used.",
        "min_loose_score": "propose_aliases: range 0.0-1.0. When omitted, 0.20 is used.",
        "max_candidates_per_cluster": "propose_aliases: range 1-20. When omitted, 5 is used.",
        "status": "list_alias_proposals/list_aliases: optional filter, defaults to pending/active.",
        "proposal_id": "approve_alias/reject_alias_proposal: proposal identifier.",
        "canonical": "approve_alias: required if concept_id does not resolve an existing concept.",
        "candidate_alias": "approve_alias: required when proposal_id is not supplied.",
        "concept_id": "approve_alias/list_aliases/disable_alias/disable_alias_concept: concept identifier.",
        "language": "Alias operations default to en.",
        "weight": "approve_alias: optional weight override, defaults to proposal score or 1.0.",
        "approved_by": "approve_alias: optional operator identity.",
        "notes": "approve_alias: optional concept notes.",
        "alias_id": "disable_alias: alias term identifier.",
        "term": "disable_alias: required with concept_id when alias_id is omitted.",
        "reason": "reject_alias_proposal/disable_alias/disable_alias_concept: optional reason text.",
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
                    "Common actions: doctor, search, record, alias_hint, topic_add, topic_remove, topic_list, memory_group_discover, memory_group_preview, pack_landing_list, pack_list_imports, pack_review_import, pack_promote_preview, pack_promote, pack_preview, pack_redaction_preview, pack_export, pack_inspect, pack_import, signer_add, signer_list, signer_disable, signer_enable, recall, get, link, export, "
                    "recent_events, search_events, get_event, memory_events, compact_context, "
                    "inspect, maintenance, salience_check, update, delete, recent, lookup_symbol. "
                    "Do not look for individual mnemo_* tools; "
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
