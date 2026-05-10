#!/usr/bin/env python3
"""Mnemo: dependency-free local MCP memory server.

Transport: newline-delimited JSON-RPC on stdin/stdout.
Storage: a JSON memory file plus optional append-only query logs.

Environment variables:
- MNEMO_FILE: path to memory.json. Defaults to memory.json next to server.py.
- MNEMO_MAX_MEMORIES: total memory cap including retired entries. Defaults to 5000.
- MNEMO_LOG_QUERIES: set to 0 to disable queries.jsonl. Defaults to 1.
- MNEMO_WORKSPACE_ROOT: root for lookup_symbol. Defaults to the parent of
  the memory file's directory.
- MNEMO_SYMBOL_TTL_SECONDS: symbol-index walk TTL. Defaults to 5.
- MNEMO_DECAY: set to 0 to disable time-decay scoring. Defaults to 1.
- MNEMO_LOG_EVENTS: set to 0 to disable events.jsonl. Defaults to 1.
- MNEMO_LOG_ARCHIVE: set to 0 to disable permanent log archives. Defaults to 1.
- MNEMO_CONSOLIDATE_THRESHOLD: near-duplicate consolidation threshold. Defaults to 0.7.
- MNEMO_MAX_SEARCH_RESULTS: server-side cap for memory_search results. Defaults to 20.
- MNEMO_MAX_RECENT_RESULTS: server-side cap for memory_recent results. Defaults to 50.
- MNEMO_MAX_FILES_SCANNED: max files scanned by lookup_symbol. Defaults to 5000.
- MNEMO_MAX_TOTAL_BYTES: max total bytes scanned by lookup_symbol. Defaults to 52428800.
- MNEMO_MAX_FILE_BYTES: max single file bytes read by lookup_symbol. Defaults to 1048576.
- AGENT_SALIENCE_HOME: optional path to local agent-salience checkout for diagnostics when not installed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from salience_loader import load_optional_agent_salience


PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "mnemo"
SERVER_TITLE = "Mnemo Project Memory"
SERVER_VERSION = "0.7.0"
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
)
ORDERED_KINDS = (
    "invariant",
    "decision",
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
}
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
SALIENCE_UNAVAILABLE_MESSAGE = (
    "Configure AGENT_SALIENCE_HOME or install agent-salience to use salience diagnostics."
)


class LockTimeout(RuntimeError):
    """Raised when the memory write lock cannot be acquired in time."""


def memory_path() -> Path:
    configured = os.environ.get("MNEMO_FILE", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_MEMORY_FILE


def workspace_root() -> Path:
    configured = os.environ.get("MNEMO_WORKSPACE_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    return memory_path().parent.parent


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


def new_memory(
    memory_id: str,
    kind: str,
    text: str,
    source: str,
    tags: list[str],
    references: list[str] | None = None,
    pinned: bool = False,
) -> dict[str, Any]:
    return {
        "id": memory_id,
        "kind": kind,
        "text": text,
        "source": source,
        "tags": tags,
        "pinned": pinned,
        "references": references or [],
        "created_at": now_iso(),
        "updated_at": None,
        "deleted_at": None,
        "deletion_reason": None,
        "superseded_by": None,
    }


def migrate_memory(memory: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(memory)
    text = str(migrated.get("text", ""))
    migrated["id"] = str(migrated.get("id") or make_id(text))
    migrated["kind"] = str(migrated.get("kind") or "note")
    migrated["text"] = text
    migrated["source"] = str(migrated.get("source", ""))
    tags = migrated.get("tags", [])
    migrated["tags"] = tags if isinstance(tags, list) else []
    migrated["pinned"] = bool(migrated.get("pinned", False))
    references = migrated.get("references", [])
    migrated["references"] = [reference for reference in references if isinstance(reference, str)] if isinstance(references, list) else []
    migrated["created_at"] = str(migrated.get("created_at") or now_iso())
    migrated.setdefault("updated_at", None)
    migrated.setdefault("deleted_at", None)
    migrated.setdefault("deletion_reason", None)
    migrated.setdefault("superseded_by", None)
    return migrated


def load_store() -> dict[str, Any]:
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
    path = memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data["version"] = 1
    data["memories"] = [migrate_memory(m) for m in data.get("memories", []) if isinstance(m, dict)]
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


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


def search_rank(args: dict[str, Any], phase: str | None = None) -> list[dict[str, Any]]:
    query = str(args.get("query", "")).strip()
    kind_filter = str(args.get("kind", "")).strip().lower()
    include_deleted = parse_bool(args.get("include_deleted"), default=False)
    include_superseded = parse_bool(args.get("include_superseded"), default=False)
    pinned_filter = parse_bool(args.get("pinned"), default=False) if "pinned" in args else None
    limit = int(args.get("limit", 5))
    limit = max(1, min(limit, 20, max_search_results()))
    if kind_filter:
        validate_kind(kind_filter)
    if phase is None:
        _, phase = resolve_phase(args, query)

    store = load_store()
    query_tokens = tokenize(query)
    ranked: list[tuple[float, dict[str, Any]]] = []
    for memory in store.get("memories", []):
        if kind_filter and memory.get("kind") != kind_filter:
            continue
        if pinned_filter is not None and bool(memory.get("pinned")) != pinned_filter:
            continue
        if not visible_memory(memory, include_deleted, include_superseded):
            continue
        score = score_memory(query_tokens, memory, phase)
        if score > 0 or not query:
            ranked.append((score, memory))

    ranked.sort(key=lambda item: (item[0], str(item[1].get("created_at", ""))), reverse=True)
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
    return {
        "id": memory.get("id"),
        "kind": memory.get("kind"),
        "text": memory.get("text"),
        "source": memory.get("source", ""),
        "tags": memory.get("tags", []),
        "pinned": bool(memory.get("pinned", False)),
        "references": memory.get("references", []),
        "score": round(float(score), 3),
        "created_at": memory.get("created_at"),
        "updated_at": memory.get("updated_at"),
        "deleted_at": memory.get("deleted_at"),
        "deletion_reason": memory.get("deletion_reason"),
        "superseded_by": memory.get("superseded_by"),
    }


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
    path = query_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= QUERY_LOG_MAX_BYTES:
            _rotate_query_log(path)
        top_score = float(matches[0].get("score", 0.0)) if matches else 0.0
        row = {
            "ts": now_iso(),
            "tool": tool,
            "args": args,
            "top_ids": [str(match.get("id")) for match in matches if match.get("id")],
            "top_score": top_score,
            "n_results": len(matches),
        }
        if phase is not None or tool in {"memory_search", "memory_compact_context"}:
            row["phase"] = phase
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")
    except Exception:
        pass


def append_drift_query_log(args: dict[str, Any], drift: float) -> None:
    if not query_logging_enabled():
        return
    path = query_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= QUERY_LOG_MAX_BYTES:
            _rotate_query_log(path)
        row = {
            "ts": now_iso(),
            "tool": "memory_drift",
            "args": args,
            "top_ids": [],
            "top_score": float(drift),
            "n_results": 0,
        }
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
    except Exception as exc:
        return tool_error(str(exc))
    append_query_log("memory_search", args, matches, phase_label)
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
    text = str(args.get("text", "")).strip()
    if not text:
        return tool_error("text is required")
    try:
        kind = validate_kind(str(args.get("kind", "note")))
        tags = normalize_tags(args.get("tags", []))
        references = normalize_references(args.get("references", []))
        pinned = False
        if "pinned" in args and args.get("pinned") is not None:
            pinned = parse_strict_bool(args.get("pinned"), "pinned")
        supersedes = args.get("supersedes")
        supersedes_id = str(supersedes).strip() if supersedes is not None else None
        if supersedes_id == "":
            supersedes_id = None
    except ValueError as exc:
        return tool_error(str(exc))

    try:
        with MemoryFileLock(memory_path()):
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
        with MemoryFileLock(memory_path()):
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
                memory["references"] = normalize_references(args.get("references"))
                changed.append("references")
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
        with MemoryFileLock(memory_path()):
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

    append_query_log("memory_recent", args, recent)
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

    append_query_log("memory_compact_context", args, matches, phase_label)
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


def memory_history(args: dict[str, Any]) -> dict[str, Any]:
    memory_id = str(args.get("id", "")).strip()
    if not memory_id:
        return tool_error("id is required")
    limit = int(args.get("limit", 50))
    limit = max(1, min(limit, 200))
    include_archive = parse_bool(args.get("include_archive"), default=False)
    path = events_log_path()
    if (
        not path.exists()
        and not path.with_name("events.1.jsonl").exists()
        and (not include_archive or not events_archive_path().exists())
    ):
        return text_result(
            "No event log available; set MNEMO_LOG_EVENTS=1 to enable.",
            {"events": []},
        )
    events = [row for row in read_event_rows(include_archive) if row.get("id") == memory_id]
    events.sort(key=lambda row: str(row.get("ts", "")))
    events = events[-limit:]
    if not events:
        return text_result(f"History for {memory_id}:\nNo events found.", {"events": []})
    lines = [f"History for {memory_id}:"]
    lines.extend(render_history_event(row) for row in events)
    return text_result("\n".join(lines), {"events": events})


def memory_related(args: dict[str, Any]) -> dict[str, Any]:
    root_id = str(args.get("id", "")).strip()
    if not root_id:
        return tool_error("id is required")
    depth = int(args.get("depth", 1))
    depth = max(1, min(depth, 3))
    include_deleted = parse_bool(args.get("include_deleted"), default=False)
    include_superseded = parse_bool(args.get("include_superseded"), default=False)
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
        return text_result(f"No related memories found for {root_id}.", {"related": []})
    lines = [f"Related memories for {root_id}:"]
    for item in related:
        memory = item["memory"]
        lines.append(
            f"- {item['id']} ({item['direction']}, distance {item['distance']}): {memory.get('text')}"
        )
    return text_result("\n".join(lines), {"related": related})


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


def memory_drift(args: dict[str, Any]) -> dict[str, Any]:
    recent_count = int(args.get("recent_count", 50))
    older_count = int(args.get("older_count", 50))
    recent_count = max(2, min(recent_count, 200))
    older_count = max(2, min(older_count, 200))
    memories = [
        memory
        for memory in load_store().get("memories", [])
        if is_active(memory)
        and not memory.get("pinned")
        and str(memory.get("kind", "")) != "invariant"
    ]
    memories.sort(key=lambda memory: str(memory.get("created_at", "")))
    if len(memories) < 4:
        drift = 0.0
        append_drift_query_log(args, drift)
        return text_result(
            "Memory drift: 0.0 (low)\ninsufficient history (need \u2265 4 active non-pinned memories)",
            {
                "drift": drift,
                "recent_count": 0,
                "older_count": 0,
                "interpretation": "low",
            },
        )

    half = len(memories) // 2
    recent_n = min(recent_count, half)
    older_n = min(older_count, half)
    older = memories[:older_n]
    recent = memories[-recent_n:]
    drift = 1.0 - jaccard(group_tokens(recent), group_tokens(older))
    drift = max(0.0, min(1.0, drift))
    drift = round(drift, 3)
    interpretation = drift_interpretation(drift)
    structured = {
        "drift": drift,
        "recent_count": len(recent),
        "older_count": len(older),
        "interpretation": interpretation,
    }
    append_drift_query_log(args, drift)
    return text_result(
        "\n".join(
            [
                f"Memory drift: {drift} ({interpretation})",
                f"Recent group: {len(recent)} memories",
                f"Older group: {len(older)} memories",
            ]
        ),
        structured,
    )


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


def memory_consolidate(args: dict[str, Any]) -> dict[str, Any]:
    threshold = consolidate_threshold(args.get("threshold") if "threshold" in args else None)
    dry_run = parse_bool(args.get("dry_run"), default=True)

    if dry_run:
        clusters = build_consolidation_clusters(load_store(), threshold)
        structured = {"applied": False, "threshold": threshold, "clusters": clusters}
        return text_result(render_consolidation_text(clusters, threshold, False, 0), structured)

    retired = 0
    try:
        with MemoryFileLock(memory_path()):
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

    structured = {"applied": True, "threshold": threshold, "clusters": clusters}
    return text_result(render_consolidation_text(clusters, threshold, True, retired), structured)


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


TOOLS = [
    {
        "name": "memory_search",
        "title": "Search Project Memory",
        "description": "Search project memories relevant to a task, bug, file, command, or decision.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query or current task."},
                "kind": {"type": "string", "enum": list(MEMORY_KINDS)},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                "include_deleted": {"type": "boolean", "default": False},
                "include_superseded": {"type": "boolean", "default": False},
                "pinned": {"type": "boolean"},
                "phase": {"type": "string", "enum": list(PHASES)},
                "max_tokens": {"type": "integer", "minimum": 1, "maximum": 100000},
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "memory_salience_check",
        "title": "Memory Salience Check",
        "description": "Optional salience diagnostics for related or duplicate-like memory matches.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5},
                "include_deleted": {"type": "boolean", "default": False},
                "include_superseded": {"type": "boolean", "default": False},
                "threshold": {"type": ["number", "null"]},
            },
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "memory_record",
        "title": "Record Project Memory",
        "description": "Record a durable project memory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(MEMORY_KINDS), "default": "note"},
                "text": {"type": "string"},
                "source": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                "supersedes": {"type": ["string", "null"]},
                "references": {"type": "array", "items": {"type": "string"}, "default": []},
                "pinned": {"type": "boolean", "default": False},
            },
            "required": ["text"],
        },
    },
    {
        "name": "memory_update",
        "title": "Update Project Memory",
        "description": "Patch text, kind, source, or tags on an existing memory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "text": {"type": "string"},
                "kind": {"type": "string", "enum": list(MEMORY_KINDS)},
                "source": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "pinned": {"type": "boolean"},
                "references": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["id"],
        },
    },
    {
        "name": "memory_delete",
        "title": "Delete Project Memory",
        "description": "Soft-delete an existing memory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "reason": {"type": ["string", "null"]},
            },
            "required": ["id"],
        },
    },
    {
        "name": "memory_recent",
        "title": "Recent Project Memories",
        "description": "Return the most recently recorded project memories.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "include_deleted": {"type": "boolean", "default": False},
                "include_superseded": {"type": "boolean", "default": False},
            },
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "memory_compact_context",
        "title": "Build Compact Project Context",
        "description": "Return a prompt-ready context block grouped by memory kind.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                "include_deleted": {"type": "boolean", "default": False},
                "include_superseded": {"type": "boolean", "default": False},
                "phase": {"type": "string", "enum": list(PHASES)},
                "max_tokens": {"type": "integer", "minimum": 1, "maximum": 100000},
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "memory_history",
        "title": "Memory History",
        "description": "Return the lifecycle events for a single memory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                "include_archive": {"type": "boolean", "default": False},
            },
            "required": ["id"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "memory_related",
        "title": "Related Memories",
        "description": "Walk the reference graph from a starting memory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "depth": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
                "include_deleted": {"type": "boolean", "default": False},
                "include_superseded": {"type": "boolean", "default": False},
            },
            "required": ["id"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "memory_drift",
        "title": "Memory Drift",
        "description": "Return a scalar measuring vocabulary drift between recent and older memories.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "recent_count": {"type": "integer", "minimum": 2, "maximum": 200, "default": 50},
                "older_count": {"type": "integer", "minimum": 2, "maximum": 200, "default": 50},
            },
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "memory_consolidate",
        "title": "Consolidate Near-Duplicate Memories",
        "description": "Find clusters of near-duplicate memories within each kind. Optionally retire duplicates by superseding to the newest survivor.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "threshold": {"type": "number", "minimum": 0.5, "maximum": 1.0},
                "dry_run": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "lookup_symbol",
        "title": "Lookup Symbol",
        "description": "Find likely definition locations for a symbol in the workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "case_sensitive": {"type": "boolean", "default": False},
            },
            "required": ["name"],
        },
        "annotations": {"readOnlyHint": True},
    },
]


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
                    "Use memory_search before complex repo work, "
                    "memory_salience_check for optional deterministic salience diagnostics, "
                    "memory_compact_context when a short project brief is useful, "
                    "memory_record for durable decisions and outcomes, and "
                    "lookup_symbol for source locations."
                ),
            },
        )
        return

    if method == "shutdown":
        ok(request_id, {})
        _SHOULD_EXIT = True
        return

    if method == "tools/list":
        ok(request_id, {"tools": TOOLS})
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
            "memory_search": search_memories,
            "memory_salience_check": memory_salience_check,
            "memory_record": record_memory,
            "memory_update": update_memory,
            "memory_delete": delete_memory,
            "memory_recent": recent_memories,
            "memory_compact_context": compact_context,
            "memory_history": memory_history,
            "memory_related": memory_related,
            "memory_drift": memory_drift,
            "memory_consolidate": memory_consolidate,
            "lookup_symbol": lookup_symbol,
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
