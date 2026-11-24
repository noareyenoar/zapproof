"""Shared pytest configuration.

`gates/` is a directory of scripts rather than an installed package, so put it on
sys.path to make the gate modules importable from tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GATES_DIR = REPO_ROOT / "gates"

for path in (REPO_ROOT, GATES_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@pytest.fixture(autouse=True)
def _no_usage_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite hermetic (I4).

    The score app's middleware appends to `data/usage.db` (or `USAGE_DB`) on every
    response unless `USAGE_METRICS=0`. Tests that call the real app were therefore
    writing to a developer's database — the fourth review measured 8099 -> 8143 rows
    over one run. Telemetry is off by default; the usage-metric tests turn it back on
    explicitly.
    """
    monkeypatch.setenv("USAGE_METRICS", "0")
