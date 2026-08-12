"""Pytest configuration: add project root to sys.path so ``import agt.*`` works.

The package lives at the repo root (``packages = ["agt"]`` in pyproject.toml),
not under ``src/``. We make the root importable regardless of how pytest is
invoked (``pytest`` from CLI, ``uv run pytest``, IDE runner, etc.).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))