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
- MNEMO_IDF_MODE: auto|off|force. Defaults to auto.
- MNEMO_IDF_MIN_DOCUMENTS: project corpus documents threshold. Defaults to 200.
- MNEMO_IDF_MIN_UNIQUE_TERMS: project corpus unique-terms threshold. Defaults to 1000.
- MNEMO_IDF_MIN_TOTAL_TOKENS: project corpus token threshold. Defaults to 10000.
- MNEMO_IDF_DOMAIN_MIN_DOCUMENTS: domain corpus documents threshold. Defaults to 50.
- MNEMO_IDF_DOMAIN_MIN_UNIQUE_TERMS: domain corpus unique-terms threshold. Defaults to 300.
- MNEMO_IDF_DOMAIN_MIN_TOTAL_TOKENS: domain corpus token threshold. Defaults to 3000.
- MNEMO_IDF_MIN_TEXT_TOKENS: per-memory minimum tokens for IDF corpus inclusion. Defaults to 5.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from salience_loader import load_optional_agent_salience


PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "mnemo"
SERVER_TITLE = "Mnemo Project Memory"
SERVER_VERSION = "0.13.2"
DEFAULT_MEMORY_FILE = Path(__file__).with_name("memory.json")
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
_SQLITE_FTS_CANDIDATE_LIMIT = 500
DEFAULT_EVENT_LIMIT = 20
MAX_EVENT_LIMIT = 200
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


def _event_payload_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return dict(payload)
    return {"value": payload}


def _event_int_value(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _event_query_text(payload: dict[str, Any]) -> str | None:
    query_text = normalize_optional_string(payload.get("query_text"))
    if query_text:
        return query_text
    query_text = normalize_optional_string(payload.get("query"))
    if query_text:
        return query_text
    args = payload.get("args")
    if isinstance(args, dict):
        return normalize_optional_string(args.get("query"))
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
    ):
        conn.execute(statement)
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
    _event_columns = [
        ("event_id", "TEXT"),
        ("ts", "TEXT"),
        ("action", "TEXT"),
        ("source_id", "TEXT"),
        ("target_id", "TEXT"),
        ("relation", "TEXT"),
        ("query_text", "TEXT"),
        ("result_count", "INTEGER"),
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
    _sqlite_set_meta(conn, "schema_version", "2")
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
            query_text, result_count, success, agent_id, role, domain, kind, summary,
            salience_text, include_in_salience, data_json, created_at, ts
        ) VALUES(
            :id, :event_id, :memory_id, :event_type, :action, :source_id, :target_id, :relation,
            :query_text, :result_count, :success, :agent_id, :role, :domain, :kind, :summary,
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
            created_at, updated_at, token_estimate, content_hash,
            normalized_hash, token_count, unique_token_count,
            top_terms_json, shingle_hashes_json,
            signature_version, normalizer_version, signature_updated_at
        ) VALUES(
            :id, :kind, :text, :title, :preview, :source, :tags_json, :linked_ids_json,
            :agent_id, :role, :scope, :domain, :authority, :retention, :confidence,
            :parent_id, :source_run_id, :metadata_json, :pinned, :deleted, :superseded_by,
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


def rank_against_query(
    memory: dict[str, Any],
    query: str,
    salience_module: Any | None = None,
    idf_profile: dict[str, Any] | None = None,
) -> float:
    if not query.strip():
        return 0.0
    if salience_module is not None:
        try:
            kwargs: dict[str, Any] = {}
            if isinstance(idf_profile, dict):
                kwargs = {
                    "mode": "auto",
                    "idf_profile": idf_profile,
                    "weights": dict(IDF_ACTIVE_WEIGHTS),
                }
            breakdown = salience_module.signal_score(query, str(memory.get("text", "")), **kwargs)
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
    idf_profile: dict[str, Any] | None = None,
) -> list[tuple[float, dict[str, Any]]]:
    scored: list[tuple[float, dict[str, Any]]] = []
    for memory in memories:
        score = rank_against_query(memory, query, salience_module, idf_profile)
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
        candidate_limit = max(1, min(int(args.get("candidate_limit") or 500), 5000))
        max_scored = max(1, min(int(args.get("max_scored") or 100), candidate_limit))
        min_token_count = max(1, int(args.get("min_token_count") or 5))
        use_fts = parse_bool(args.get("use_fts"), default=True)
        raw_shingle_overlap_threshold = args.get("shingle_overlap_threshold")
        shingle_overlap_threshold = 0.30 if raw_shingle_overlap_threshold is None else float(raw_shingle_overlap_threshold)
        shingle_overlap_threshold = max(0.0, min(1.0, shingle_overlap_threshold))
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

    input_sig = _build_memory_signature(text)
    input_shingles = _load_json_string_list(input_sig.get("shingle_hashes_json"))
    input_token_count = int(input_sig.get("token_count") or 0)

    candidate_source = "fallback"
    fts_used = False
    fts_available = _sqlite_fts_flag() if store_backend() == "sqlite" else False
    candidates: list[dict[str, Any]] = []

    if store_backend() == "sqlite" and use_fts and fts_available:
        fts_query = " ".join(_load_json_string_list(input_sig.get("top_terms_json")))
        candidates = _sqlite_fts_candidate_memories(
            args,
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
        if use_idf and active_idf_profile is not None:
            breakdown = salience.signal_score(
                text,
                memory_text,
                mode="auto",
                idf_profile=active_idf_profile,
                weights=dict(IDF_ACTIVE_WEIGHTS),
            )
        else:
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


def memory_maintenance(args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action", "")).strip().lower()
    valid_actions = {"compact_logs", "consolidate", "consolidate_full", "import_json", "backfill_signatures"}
    if action not in valid_actions:
        return tool_error(f"action must be one of: {', '.join(sorted(valid_actions))}")
    dry_run = parse_bool(args.get("dry_run"), default=True)
    if action == "compact_logs":
        return _compact_logs_maintenance(args, dry_run)
    if action == "import_json":
        return _import_json_maintenance(args, dry_run)
    if action == "backfill_signatures":
        return _backfill_signatures_maintenance(args, dry_run)
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
    salience_module: Any | None,
    idf_profile: dict[str, Any] | None = None,
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
        scored_blocks = select_memories_by_query(
            block_candidates,
            query,
            max_blocks * 2,
            salience_module,
            idf_profile,
        )
        blocks_by_id: dict[str, dict[str, Any]] = {}
        for memory in linked_blocks:
            blocks_by_id[str(memory.get("id"))] = memory
        for _, memory in scored_blocks:
            if len(blocks_by_id) >= max_blocks:
                break
            blocks_by_id.setdefault(str(memory.get("id")), memory)
        context_blocks = list(blocks_by_id.values())[:max_blocks]

        hippocampus = [memory for memory in visible if str(memory.get("kind", "")) == "hippocampus_entry"]
        scored_hippocampus = select_memories_by_query(
            hippocampus,
            query,
            max_hippocampus,
            salience_module,
            idf_profile,
        )
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
        score += rank_against_query(memory, task, salience_module, idf_profile) if task else 0.0
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
    scored_hippocampus = select_memories_by_query(
        hippocampus_candidates,
        task,
        max_hippocampus,
        salience_module,
        idf_profile,
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
    )
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
        salience_module=salience,
        idf_profile=recall_idf_profile,
    )
    structured = _apply_recall_output_caps(structured)
    structured["idf_used"] = bool(recall_idf_profile)
    structured["idf_scope_used"] = str(idf_choice.get("scope", "none")) if recall_idf_profile else "none"
    structured["idf_profile_status"] = str(idf_choice.get("status", "not_requested"))
    structured["score_weights"] = dict(IDF_ACTIVE_WEIGHTS) if recall_idf_profile else {}
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
            "Actions: doctor, record, search, recall, get, link, export, "
            "recent_events, search_events, get_event, memory_events, "
            "maintenance(compact_logs, consolidate, consolidate_full, import_json, backfill_signatures), "
            "inspect, lookup_symbol, salience_check, update, delete, recent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": sorted(GATEWAY_ACTIONS),
                    "description": "Required action name. Use maintenance for compact_logs/consolidate/import_json, or the top-level aliases backfill_signatures and consolidate_full for those v0.12 actions.",
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
