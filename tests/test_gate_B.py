"""Tests for the Gate B checks added after the independent review.

As with `test_gate_0.py`, the point is not coverage: it is to prove the checks can say
no. The review (`reports/review_B.md`) found the gate reading evidence it had not
produced, so most of these tests ask the new checks to reject a plausible-looking
artefact.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import _shared
import gate_B
from _shared import CheckResult

THRESHOLDS: dict[str, Any] = {"phaseB": {"market_window_days": 30}}

COMMIT = "a" * 40
DB_HASH = "b" * 64
ARTIFACTS = (
    "FUZZ_JSON",
    "WRITE_PROBE",
    "DETERMINISM_JSON",
    "MCP_TEST_JSON",
    "LOADTEST_JSON",
    "DISCLAIMER_LOG",
    "BINDING_E2E",
)


@pytest.fixture
def clean_provenance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A clean tree whose artefacts all name HEAD and the database on disk."""
    monkeypatch.setattr(gate_B, "git_dirty_paths", lambda _paths=None: [])
    monkeypatch.setattr(gate_B, "git_commit", lambda: COMMIT)
    monkeypatch.setattr(gate_B, "_db_hash", lambda: DB_HASH)
    monkeypatch.setattr(gate_B, "DATA_DB", tmp_path / "repindex.db")
    return tmp_path


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# the checks list
# --------------------------------------------------------------------------- #


def test_every_check_is_registered_with_its_own_id() -> None:
    ids = [check.__name__.replace("check_", "").replace("_", ".") for check in gate_B.CHECKS]
    assert ids == [f"GB.{n}" for n in range(1, 18)]


# --------------------------------------------------------------------------- #
# the shared snapshot (second review, G5)
# --------------------------------------------------------------------------- #


def test_the_snapshot_hash_is_taken_after_the_checks_regenerate_the_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G5: a pre-run hash cannot be reproduced from the evidence the run commits."""
    artifact = tmp_path / "artifact.json"
    artifact.write_text("before", encoding="utf-8")
    pre_run = _shared.snapshot_hash([artifact])
    monkeypatch.setattr(_shared, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(_shared, "git_commit", lambda: COMMIT)
    monkeypatch.setattr(_shared, "git_dirty_paths", lambda _paths=None: [])

    def rewrite(_thresholds: dict[str, Any]) -> CheckResult:
        artifact.write_text("after", encoding="utf-8")
        return CheckResult(id="T.1", title="t", status="PASS", measured="", threshold="")

    assert _shared.main_wrapper("T", [rewrite], [artifact], source_paths=[]) == 0

    payload = json.loads((tmp_path / "gate_T.json").read_text(encoding="utf-8"))
    assert payload["data_snapshot_hash"] != pre_run
    assert payload["data_snapshot_hash"] == _shared.snapshot_hash([artifact])


def test_newest_review_orders_by_number_not_lexicographically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """I5: `sorted()` puts `review_B10.md` before `review_B2.md`.

    Once ten reviews existed, GB.11 would have parsed a superseded report, so a newer
    review with unresolved findings could be ignored.
    """
    for name in ("review_B.md", "review_B2.md", "review_B10.md"):
        (tmp_path / name).write_text("unresolved findings: 1\n", encoding="utf-8")
    monkeypatch.setattr(_shared, "REPORTS_DIR", tmp_path)

    newest, found = _shared.newest_review("B")

    assert newest.name == "review_B10.md"
    assert [path.name for path in found] == [
        "review_B.md",
        "review_B2.md",
        "review_B10.md",
    ]


# --------------------------------------------------------------------------- #
# GB.14 — provenance
# --------------------------------------------------------------------------- #


def test_gb_14_fails_when_the_measured_source_is_uncommitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gate_B, "git_dirty_paths", lambda _paths=None: [" M src/repindex/api/app.py"]
    )

    result = gate_B.check_GB_14(THRESHOLDS)

    assert result.status == "FAIL"
    assert "not what a commit records" in " ".join(result.notes)


def test_gb_14_fails_when_an_artefact_names_another_commit(
    clean_provenance: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ARTIFACTS:
        payload = {"commit": "c" * 40, "db_sha256": DB_HASH}
        monkeypatch.setattr(gate_B, name, _write_json(clean_provenance / f"{name}.json", payload))

    result = gate_B.check_GB_14(THRESHOLDS)

    assert result.status == "FAIL"
    assert "another database" not in " ".join(result.notes)
    assert "from commit" in result.notes[0]


def test_gb_14_fails_when_an_artefact_was_produced_from_another_database(
    clean_provenance: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ARTIFACTS:
        payload = {"commit": COMMIT, "db_sha256": "d" * 64}
        monkeypatch.setattr(gate_B, name, _write_json(clean_provenance / f"{name}.json", payload))

    result = gate_B.check_GB_14(THRESHOLDS)

    assert result.status == "FAIL"
    assert any("another database" in note for note in result.notes)


def test_gb_14_fails_when_an_artefact_is_missing(
    clean_provenance: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ARTIFACTS:
        monkeypatch.setattr(gate_B, name, clean_provenance / f"{name}.json")

    result = gate_B.check_GB_14(THRESHOLDS)

    assert result.status == "FAIL"
    assert any("missing" in note for note in result.notes)


def test_gb_14_passes_when_every_artefact_names_head_and_this_database(
    clean_provenance: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ARTIFACTS:
        payload = {"commit": COMMIT, "db_sha256": DB_HASH}
        monkeypatch.setattr(gate_B, name, _write_json(clean_provenance / f"{name}.json", payload))

    result = gate_B.check_GB_14(THRESHOLDS)

    assert result.status == "PASS", result.notes


# --------------------------------------------------------------------------- #
# GB.13 — identity binding
# --------------------------------------------------------------------------- #


def _binding_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cases: dict[str, bool]) -> None:
    test_file = tmp_path / "test_binding.py"
    test_file.write_text("", encoding="utf-8")
    script = tmp_path / "binding_e2e.py"
    script.write_text("", encoding="utf-8")
    artefact = _write_json(tmp_path / "binding_e2e.json", {"cases": cases})
    monkeypatch.setattr(gate_B, "BINDING_TEST", test_file)
    monkeypatch.setattr(gate_B, "BINDING_E2E_SCRIPT", script)
    monkeypatch.setattr(gate_B, "BINDING_E2E", artefact)
    monkeypatch.setattr(gate_B, "run", lambda *a, **k: (0, "ok"))
    monkeypatch.setattr(gate_B, "_producer", lambda *a, **k: (0, "ok"))


def test_gb_13_passes_when_every_required_case_is_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = dict.fromkeys(gate_B.REQUIRED_BINDING_CASES, True)
    _binding_files(tmp_path, monkeypatch, cases)

    result = gate_B.check_GB_13(THRESHOLDS)

    assert result.status == "PASS", result.notes


def test_gb_13_rejects_a_run_where_one_case_is_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = dict.fromkeys(gate_B.REQUIRED_BINDING_CASES, True)
    cases["replay_rejected"] = False
    _binding_files(tmp_path, monkeypatch, cases)

    result = gate_B.check_GB_13(THRESHOLDS)

    assert result.status == "FAIL"
    assert "replay_rejected" in " ".join(result.notes)


def test_gb_13_rejects_a_run_that_omits_a_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = dict.fromkeys(gate_B.REQUIRED_BINDING_CASES, True)
    del cases["rotation_accepted"]
    _binding_files(tmp_path, monkeypatch, cases)

    result = gate_B.check_GB_13(THRESHOLDS)

    assert result.status == "FAIL"
    assert "rotation_accepted" in " ".join(result.notes)


def test_freshness_rejects_a_stale_artefact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate_B, "_db_hash", lambda: DB_HASH)
    monkeypatch.setattr(gate_B, "git_commit", lambda: COMMIT)
    result = CheckResult(id="X", title="x", status="FAIL", measured="", threshold="")

    assert gate_B._freshness({"commit": COMMIT, "db_sha256": DB_HASH}, result) is True
    assert gate_B._freshness({"commit": COMMIT, "db_sha256": "e" * 64}, result) is False
    assert result.notes


def test_a_non_empty_wal_blocks_freshness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """M2: a main-file hash cannot identify a database whose WAL holds changes."""
    db = tmp_path / "repindex.db"
    db.write_bytes(b"main file")
    (tmp_path / "repindex.db-wal").write_bytes(b"uncheckpointed frames")
    monkeypatch.setattr(gate_B, "DATA_DB", db)
    monkeypatch.setattr(gate_B, "_DB_IDENTITY", [])

    result = CheckResult(id="X", title="x", status="FAIL", measured="", threshold="")
    assert gate_B._db_hash() is None
    assert gate_B._freshness({"commit": COMMIT, "db_sha256": DB_HASH}, result) is False
    assert "WAL" in " ".join(result.notes)


def test_db_hash_is_recomputed_after_the_database_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """N1: an identity cached before a change must not outlive the change."""
    db = tmp_path / "repindex.db"
    db.write_bytes(b"state one")
    monkeypatch.setattr(gate_B, "DATA_DB", db)
    monkeypatch.setattr(gate_B, "_DB_IDENTITY", [])

    first = gate_B._db_hash()
    assert first is not None
    assert gate_B._db_hash() == first  # cached only while the file is unchanged

    db.write_bytes(b"state two, and longer")
    second = gate_B._db_hash()
    assert second is not None
    assert second != first


def test_db_hash_rechecks_the_wal_on_every_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """N1: a cached hash must not hide WAL frames committed after the first call."""
    db = tmp_path / "repindex.db"
    db.write_bytes(b"main file")
    monkeypatch.setattr(gate_B, "DATA_DB", db)
    monkeypatch.setattr(gate_B, "_DB_IDENTITY", [])
    assert gate_B._db_hash() is not None

    (tmp_path / "repindex.db-wal").write_bytes(b"uncheckpointed frames")
    assert gate_B._db_hash() is None


def test_gb_14_names_a_pending_wal_instead_of_a_missing_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N2: a present database blocked by its WAL must name the WAL and the repair."""
    db = tmp_path / "repindex.db"
    db.write_bytes(b"main file")
    (tmp_path / "repindex.db-wal").write_bytes(b"uncheckpointed frames")
    monkeypatch.setattr(gate_B, "DATA_DB", db)
    monkeypatch.setattr(gate_B, "_DB_IDENTITY", [])
    monkeypatch.setattr(gate_B, "git_dirty_paths", lambda _paths=None: [])

    result = gate_B.check_GB_14(THRESHOLDS)

    assert result.status == "FAIL"
    notes = " ".join(result.notes)
    assert "uncheckpointed WAL frames" in notes
    assert "is missing" not in notes


# --------------------------------------------------------------------------- #
# GB.15 — packaging
# --------------------------------------------------------------------------- #


def _packaging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compose: str) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.11-slim\n", encoding="utf-8")
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(compose, encoding="utf-8")
    deploy = tmp_path / "deploy_local.md"
    deploy.write_text("# deploy\n", encoding="utf-8")
    monkeypatch.setattr(gate_B, "DOCKERFILE", dockerfile)
    monkeypatch.setattr(gate_B, "COMPOSE_FILE", compose_file)
    monkeypatch.setattr(gate_B, "DEPLOY_DOC", deploy)


def test_gb_15_fails_when_packaging_was_never_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("DOCKERFILE", "COMPOSE_FILE", "DEPLOY_DOC"):
        monkeypatch.setattr(gate_B, name, tmp_path / "missing")

    result = gate_B.check_GB_15(THRESHOLDS)

    assert result.status == "FAIL"
    assert "B6" in " ".join(result.notes)


def test_gb_15_fails_when_compose_has_no_api_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _packaging(tmp_path, monkeypatch, "services:\n  db:\n    image: busybox\n")

    result = gate_B.check_GB_15(THRESHOLDS)

    assert result.status == "FAIL"
    assert "no `api` service" in " ".join(result.notes)


def test_gb_15_passes_without_docker_but_records_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _packaging(tmp_path, monkeypatch, "services:\n  api:\n    build: .\n")
    monkeypatch.setattr(gate_B.shutil, "which", lambda _name: None)

    result = gate_B.check_GB_15(THRESHOLDS)

    assert result.status == "PASS", result.notes
    assert any("docker CLI is not installed" in note for note in result.notes)


# --------------------------------------------------------------------------- #
# GB.16 — public analysis
# --------------------------------------------------------------------------- #


def _analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: str, charts: int = 2
) -> None:
    analysis_dir = tmp_path / "analysis"
    analysis_dir.mkdir()
    for index in range(charts):
        (analysis_dir / f"chart_{index}.png").write_bytes(b"\x89PNG\r\n")
    public = tmp_path / "phase_B_public_analysis.md"
    public.write_text(report, encoding="utf-8")
    provenance = _write_json(
        analysis_dir / "provenance.json", {"commit": COMMIT, "db_sha256": DB_HASH}
    )
    monkeypatch.setattr(gate_B, "ANALYSIS_DIR", analysis_dir)
    monkeypatch.setattr(gate_B, "PUBLIC_ANALYSIS", public)
    monkeypatch.setattr(gate_B, "PUBLIC_ANALYSIS_PROV", provenance)
    monkeypatch.setattr(gate_B, "git_commit", lambda: COMMIT)
    monkeypatch.setattr(gate_B, "_db_hash", lambda: DB_HASH)
    monkeypatch.setattr(gate_B, "_producer", lambda *args, **kwargs: (0, "ok"))


def test_gb_16_fails_when_the_analysis_was_never_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(gate_B, "PUBLIC_ANALYSIS", tmp_path / "missing.md")
    monkeypatch.setattr(gate_B, "ANALYSIS_DIR", tmp_path / "missing")

    result = gate_B.check_GB_16(THRESHOLDS)

    assert result.status == "FAIL"
    assert "B4" in " ".join(result.notes)


def test_gb_16_fails_with_fewer_than_two_charts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _analysis(tmp_path, monkeypatch, "![a](analysis/chart_0.png)\n", charts=1)

    result = gate_B.check_GB_16(THRESHOLDS)

    assert result.status == "FAIL"
    assert "at least two" in " ".join(result.notes)


def test_gb_16_fails_when_the_report_ignores_its_charts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _analysis(tmp_path, monkeypatch, "# Analysis\n\nNo figures cited.\n")

    result = gate_B.check_GB_16(THRESHOLDS)

    assert result.status == "FAIL"
    assert "does not reference" in " ".join(result.notes)


def test_gb_16_fails_when_the_report_publishes_an_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = "![a](analysis/chart_0.png)\n\nSeller 0x" + "ab" * 20 + " dominates.\n"
    _analysis(tmp_path, monkeypatch, report)

    result = gate_B.check_GB_16(THRESHOLDS)

    assert result.status == "FAIL"
    assert "full address" in " ".join(result.notes)


def test_gb_16_rejects_an_analysis_from_another_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _analysis(
        tmp_path,
        monkeypatch,
        "![a](analysis/chart_0.png)\n\n![b](analysis/chart_1.png)\n",
    )
    _write_json(
        tmp_path / "analysis" / "provenance.json",
        {"commit": "c" * 40, "db_sha256": DB_HASH},
    )

    result = gate_B.check_GB_16(THRESHOLDS)

    assert result.status == "FAIL"
    assert "different commit" in " ".join(result.notes)


def test_gb_16_rejects_an_analysis_the_producer_could_not_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _analysis(
        tmp_path,
        monkeypatch,
        "![a](analysis/chart_0.png)\n\n![b](analysis/chart_1.png)\n",
    )
    monkeypatch.setattr(gate_B, "_producer", lambda *args, **kwargs: (1, "boom"))

    result = gate_B.check_GB_16(THRESHOLDS)

    assert result.status == "FAIL"
    assert "producer failed" in " ".join(result.notes)


def test_gb_16_passes_with_cited_charts_and_no_addresses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _analysis(
        tmp_path,
        monkeypatch,
        "![a](analysis/chart_0.png)\n\n![b](analysis/chart_1.png)\n",
    )

    result = gate_B.check_GB_16(THRESHOLDS)

    assert result.status == "PASS", result.notes


# --------------------------------------------------------------------------- #
# GB.17 — usage metrics
# --------------------------------------------------------------------------- #


def _metrics_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, makefile: str) -> Path:
    script = tmp_path / "metrics.py"
    script.write_text("print('{}')\n", encoding="utf-8")
    make = tmp_path / "Makefile"
    make.write_text(makefile, encoding="utf-8")
    monkeypatch.setattr(gate_B, "METRICS_SCRIPT", script)
    monkeypatch.setattr(gate_B, "MAKEFILE", make)
    metrics_json = tmp_path / "usage_metrics.json"
    monkeypatch.setattr(gate_B, "METRICS_JSON", metrics_json)
    return metrics_json


def test_gb_17_fails_when_the_metrics_script_was_never_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(gate_B, "METRICS_SCRIPT", tmp_path / "missing.py")
    monkeypatch.setattr(gate_B, "MAKEFILE", tmp_path / "Makefile")

    result = gate_B.check_GB_17(THRESHOLDS)

    assert result.status == "FAIL"
    assert "B7" in " ".join(result.notes)


def test_gb_17_fails_while_make_metrics_is_still_a_stub(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _metrics_setup(
        tmp_path,
        monkeypatch,
        "metrics:\n\techo 'not implemented yet (Phase B, task B7)'\n",
    )

    result = gate_B.check_GB_17(THRESHOLDS)

    assert result.status == "FAIL"
    assert "stub" in " ".join(result.notes)


def test_gb_17_fails_when_the_script_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _metrics_setup(tmp_path, monkeypatch, "metrics:\n\tuv run python scripts/metrics.py\n")
    monkeypatch.setattr(gate_B, "_producer", lambda *_a, **_k: (1, "boom"))

    result = gate_B.check_GB_17(THRESHOLDS)

    assert result.status == "FAIL"
    assert "boom" in " ".join(result.notes)


def test_gb_17_fails_when_the_window_is_too_short(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    metrics_json = _metrics_setup(
        tmp_path, monkeypatch, "metrics:\n\tuv run python scripts/metrics.py\n"
    )
    monkeypatch.setattr(gate_B, "_producer", lambda *_a, **_k: (0, ""))
    _write_json(metrics_json, {"unique_external_callers": 3, "window_days": 7})

    result = gate_B.check_GB_17(THRESHOLDS)

    assert result.status == "FAIL"
    assert "shorter than the market-gate window" in " ".join(result.notes)


def test_gb_17_passes_with_unique_callers_over_the_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    metrics_json = _metrics_setup(
        tmp_path, monkeypatch, "metrics:\n\tuv run python scripts/metrics.py\n"
    )
    monkeypatch.setattr(gate_B, "_producer", lambda *_a, **_k: (0, ""))
    _write_json(metrics_json, {"unique_external_callers": 0, "window_days": 30})

    result = gate_B.check_GB_17(THRESHOLDS)

    assert result.status == "PASS", result.notes
