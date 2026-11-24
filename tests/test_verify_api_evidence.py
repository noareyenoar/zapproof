"""The API evidence producer writes nothing until its identity is confirmed (Q1).

The twelfth review found that `scripts/verify_api.py` wrote all five artefacts
before the final `require_unchanged`/`require_quiet`, so a run that observed a
commit exited 1 yet left files that individually passed the gate's freshness checks.
The producer now buffers every artefact in memory and writes them only after the
identity confirmation succeeds.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from repindex.common import provenance as provenance_module
from repindex.ingest import schema

REPO_ROOT = Path(__file__).resolve().parent.parent
VERIFY = REPO_ROOT / "scripts" / "verify_api.py"

SELLER = "0x" + "a1" * 20
OTHER = "0x" + "b2" * 20
DISCLAIMER = "not independently validated"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _db(tmp_path: Path) -> Path:
    db_path = tmp_path / "evidence.db"
    schema.initialize(db_path)
    conn = schema.connect(db_path)
    try:
        for seller, settlements in ((SELLER, 3), (OTHER, 2)):
            conn.execute(
                "INSERT INTO features (seller, unique_payers, repeat_buyer_rate, "
                "reciprocity_rate, amount_entropy, burstiness, peak_hour_share, "
                "fresh_payer_share, self_funding_flag, relayer_concentration, "
                "settlement_count, active_days, methodology_version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (seller, 3, 0.0, 0.0, 1.0, 0.1, 0.5, 0.0, 0, None, settlements, 4, "v1"),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


class _FakeClient:
    """Answers every request without touching the app or a database."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def get(self, path: str, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(status_code=200, text=DISCLAIMER, headers={})

    def post(self, path: str, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(status_code=405, text=DISCLAIMER, headers={})

    def request(self, method: str, path: str, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(status_code=405, text=DISCLAIMER, headers={})


def test_a_refused_run_writes_no_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load(VERIFY, "verify_api_under_test")
    reports = tmp_path / "reports"
    monkeypatch.setattr(module, "DB", _db(tmp_path))
    monkeypatch.setattr(module, "REPORTS", reports)
    monkeypatch.setattr(module, "TestClient", _FakeClient)
    monkeypatch.setattr(module, "app", SimpleNamespace(openapi=lambda: {"paths": {}}))
    monkeypatch.setattr(
        module, "_probe", lambda identifier: {"sha256": "00", "status": 200}
    )

    def _refuse(path: Path, attested: str) -> str:
        raise provenance_module.DatabaseChangedError("another connection committed (test)")

    monkeypatch.setattr(module, "require_unchanged", _refuse)

    assert module.main() == 1
    assert "another connection committed" in capsys.readouterr().out
    assert reports.is_dir()
    assert list(reports.iterdir()) == []
