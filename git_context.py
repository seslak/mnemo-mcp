from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

_GIT_TIMEOUT_SECONDS = 2.0


def _safe_repo_path(repo_root: str | None) -> Path | None:
    if not repo_root:
        return None
    try:
        root = Path(repo_root).expanduser()
    except Exception:
        return None
    return root


def _run_git(repo_root: str, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(Path(repo_root).expanduser()),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:
        return None


def _normalize_repo_relative_path(repo_root: str, path: str) -> str | None:
    try:
        root = Path(repo_root).expanduser().resolve()
        raw = Path(path).expanduser()
        candidate = raw if raw.is_absolute() else (root / raw)
        resolved = candidate.resolve()
    except Exception:
        return None
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return None
    rel_text = rel.as_posix().strip()
    return rel_text or None


def capture_git_context(repo_root: str | None) -> dict[str, Any]:
    """
    Returns {'sha': str|None, 'branch': str|None, 'dirty': int|None}.
    Never raises. Returns all-None when not a git repo or git unavailable.
    """
    root = _safe_repo_path(repo_root)
    if root is None:
        return {"sha": None, "branch": None, "dirty": None}

    inside = _run_git(str(root), ["rev-parse", "--is-inside-work-tree"])
    if inside is None or inside.returncode != 0 or inside.stdout.strip().lower() != "true":
        return {"sha": None, "branch": None, "dirty": None}

    sha_proc = _run_git(str(root), ["rev-parse", "HEAD"])
    branch_proc = _run_git(str(root), ["rev-parse", "--abbrev-ref", "HEAD"])
    dirty_proc = _run_git(str(root), ["status", "--porcelain"])
    if (
        sha_proc is None
        or sha_proc.returncode != 0
        or branch_proc is None
        or branch_proc.returncode != 0
        or dirty_proc is None
        or dirty_proc.returncode != 0
    ):
        return {"sha": None, "branch": None, "dirty": None}

    sha = sha_proc.stdout.strip() or None
    branch = branch_proc.stdout.strip() or None
    dirty = 1 if dirty_proc.stdout.strip() else 0
    if sha is None:
        return {"sha": None, "branch": None, "dirty": None}
    return {"sha": sha, "branch": branch, "dirty": dirty}


def file_sha_at_head(repo_root: str, path: str) -> str | None:
    """git blob sha of HEAD:path. None if not tracked or git fails."""
    rel = _normalize_repo_relative_path(repo_root, path)
    if not rel:
        return None
    proc = _run_git(repo_root, ["rev-parse", f"HEAD:{rel}"])
    if proc is None or proc.returncode != 0:
        return None
    digest = proc.stdout.strip()
    return digest or None


def _blake2b_128_hex(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except Exception:
        return None
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def current_file_sha(repo_root: str, path: str) -> str | None:
    """git blob sha of the file as it exists on disk right now (git hash-object).
    Falls back to BLAKE2b-128 hex of file bytes if git is unavailable.
    None if the file is missing."""
    rel = _normalize_repo_relative_path(repo_root, path)
    if not rel:
        return None
    try:
        root = Path(repo_root).expanduser().resolve()
        abs_path = (root / Path(rel)).resolve()
    except Exception:
        return None
    if not abs_path.exists() or not abs_path.is_file():
        return None

    proc = _run_git(repo_root, ["hash-object", str(abs_path)])
    if proc is not None and proc.returncode == 0:
        digest = proc.stdout.strip()
        if digest:
            return digest
    return _blake2b_128_hex(abs_path)

