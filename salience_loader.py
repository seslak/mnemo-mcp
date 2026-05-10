"""Optional loader for agent_salience.

Resolution order:
1. Normal import (installed/editable package).
2. AGENT_SALIENCE_HOME fallback with src-aware path insertion.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional, Tuple


def _import_agent_salience() -> ModuleType:
    return importlib.import_module("agent_salience")


def load_optional_agent_salience() -> Tuple[Optional[ModuleType], Optional[str]]:
    """Try to load agent_salience without making it a hard dependency."""
    try:
        return _import_agent_salience(), None
    except Exception as exc:
        direct_reason = f"normal import failed: {type(exc).__name__}: {exc}"

    home_raw = os.environ.get("AGENT_SALIENCE_HOME", "").strip()
    if not home_raw:
        return None, f"{direct_reason}; AGENT_SALIENCE_HOME is not set"

    try:
        home = Path(home_raw).expanduser().resolve()
    except Exception as exc:
        return None, f"{direct_reason}; invalid AGENT_SALIENCE_HOME: {exc}"

    candidate_src = home / "src"
    load_path = candidate_src if candidate_src.exists() else home
    path_str = str(load_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

    try:
        return _import_agent_salience(), None
    except Exception as exc:
        return (
            None,
            (
                f"{direct_reason}; AGENT_SALIENCE_HOME import failed from {path_str}: "
                f"{type(exc).__name__}: {exc}"
            ),
        )
