"""The published flagged counts speak the API's vocabulary (review K1).

`scores.status = 'scored'` is the boundary at which a likelihood is published. A
seller below the evidence minimum has a stored classification but the API returns
`signaling_likelihood: null` and `judged: false`, so the report must not count it in
a number phrased as "sellers with a signaling_likelihood of 0.5 or above". These
tests use two flagged sellers — one scored, one below the minimum — and pin both
sides of the split: the headline counts one, the separately published unjudged count
is one.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from types import ModuleType

import pytest

from repindex.classify import features as feat
from repindex.classify.rules import DEFAULT_THRESHOLDS
from repindex.common import provenance as provenance_module
from repindex.common.provenance import file_sha256
from repindex.ingest import schema

REPO_ROOT = Path(__file__).resolve().parent.parent
METRICS = REPO_ROOT / "scripts" / "metrics_report.py"
GENERATE = REPO_ROOT / "scripts" / "generate_report.py"
SENSITIVITY = REPO_ROOT / "scripts" / "sensitivity.py"

FLAGGED_SCORED = "0x" + "a1" * 20
FLAGGED_UNSCORED = "0x" + "b2" * 20
CLEAN_SCORED = "0x" + "c3" * 20

_TX = itertools.count(1)


def _settlements(conn: sqlite3.Connection, seller: str, count: int) -> None:
    for _ in range(count):
        tx_hash = f"0x{next(_TX):064x}"
        conn.execute(
            "INSERT INTO settlements (tx_hash, log_index, block_number, block_hash, "
            "block_time, token, decimals, raw_amount, normalized_amount, payer, seller, "
            "facilitator_relayer, nonce, mechanism) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tx_hash,
                0,
                1000,
                "0xblock",
                1,
                "0xtoken",
                6,
                2000,
                "0.002000",
                "0xpayer",
                seller,
                None,
                "0xnonce",
                "eip3009_authorization_used",
            ),
        )


def _seller(
    conn: sqlite3.Connection,
    seller: str,
    *,
    likelihood: float,
    status: str,
    settlements: int,
) -> None:
    conn.execute(
        "INSERT INTO features (seller, unique_payers, repeat_buyer_rate, reciprocity_rate, "
        "amount_entropy, burstiness, peak_hour_share, fresh_payer_share, self_funding_flag, "
        "relayer_concentration, settlement_count, active_days, methodology_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (seller, 3, 0.0, 0.0, 1.0, 0.1, 0.5, 0.0, 0, None, settlements, 4, "v1"),
    )
    conn.execute(
        "INSERT INTO classifications (seller, signaling_likelihood, contributions, "
        "methodology_version) VALUES (?,?,?,?)",
        (seller, likelihood, "[]", "v1"),
    )
    conn.execute(
        "INSERT INTO scores (identifier, status, score, confidence_band, "
        "evidence_settlements, evidence_counterparties, evidence_active_days, "
        "data_window_start, data_window_end, as_of_block, methodology_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (seller, status, 50.0 if status == "scored" else None, "low", 20, 5, 4, 1, 2, 3, "v1"),
    )
    _settlements(conn, seller, settlements)


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "flagged.db"
    schema.initialize(path)
    conn = schema.connect(path)
    try:
        _seller(conn, FLAGGED_SCORED, likelihood=0.8, status="scored", settlements=2)
        _seller(
            conn,
            FLAGGED_UNSCORED,
            likelihood=0.9,
            status="insufficient_evidence",
            settlements=3,
        )
        _seller(conn, CLEAN_SCORED, likelihood=0.0, status="scored", settlements=1)
        conn.commit()
    finally:
        conn.close()
    return path


def _metric(db_path: Path, name: str) -> str:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [sys.executable, str(METRICS), "--db", str(db_path), name],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_the_flagged_count_excludes_a_seller_below_the_evidence_minimum(
    tmp_path: Path,
) -> None:
    db_path = _db(tmp_path)

    # Two sellers are at or above 0.5 in the stored classifications; only the scored
    # one may be called "at the cutoff" by the public headline.
    assert _metric(db_path, "flagged_sellers") == "1"
    assert _metric(db_path, "flagged_settlement_share_pct") == "33.3333"
    # The unjudged half is published separately, not hidden.
    assert _metric(db_path, "flagged_below_minimum_sellers") == "1"
    assert _metric(db_path, "flagged_below_minimum_settlement_share_pct") == "50.0"
    assert _metric(db_path, "nonzero_likelihood_scored_sellers") == "1"


def _load_script(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sensitivity_starts_from_stored_classifications_and_holds_overrides(
    monkeypatch,
) -> None:
    module = _load_script(SENSITIVITY, "sensitivity_under_test")

    class _Verdict:
        signaling_likelihood = 0.1

    monkeypatch.setattr(module.rules, "classify_seller", lambda f, t: _Verdict())

    def _seller(seller: str, count: int) -> feat.SellerFeatures:
        return feat.SellerFeatures(
            seller=seller,
            settlement_count=count,
            unique_payers=3,
            repeat_buyer_rate=0.0,
            reciprocity_rate=0.0,
            amount_entropy=1.0,
            burstiness=0.1,
            peak_hour_share=0.5,
            fresh_payer_share=0.0,
            self_funding_flag=0,
            relayer_concentration=None,
            active_days=4,
        )

    features = [_seller("overridden", 2), _seller("rules_only", 3)]
    scored = {"overridden", "rules_only"}
    stored = {"overridden": 0.9, "rules_only": 0.1}

    # The baseline is the stored share the report publishes: 2 / 5 settlements.
    assert module.stored_share(features, scored, stored) == 0.4

    # The perturbed rules call every seller 0.1, but the recorded override is held at
    # 0.9, so the perturbation still counts only the overridden seller's settlements.
    assert (
        module.perturbed_share(
            features,
            module.rules.DEFAULT_THRESHOLDS,
            scored,
            stored,
            {"overridden"},
        )
        == 0.4
    )

    # Dropping the held override gives the rules-only answer, 0 — the two quantities
    # L1 showed being published beside each other as if they were one.
    assert (
        module.perturbed_share(
            features,
            module.rules.DEFAULT_THRESHOLDS,
            scored,
            stored,
            set(),
        )
        == 0.0
    )


def test_sensitivity_main_reads_stored_overrides_from_the_database(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = _db(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE classifications SET signaling_likelihood = 0.9, contributions = ? "
            "WHERE seller = ?",
            (
                json.dumps(
                    [{"rule": "dispute_override", "fired": True, "contribution": 0.9}]
                ),
                CLEAN_SCORED,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    module = _load_script(SENSITIVITY, "sensitivity_under_test")
    out_path = tmp_path / "sensitivity.json"
    monkeypatch.setattr(module, "DB_PATH", db_path)
    monkeypatch.setattr(module, "OUT_PATH", out_path)
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)

    assert module.main() == 0
    data = json.loads(out_path.read_text(encoding="utf-8"))

    assert data["scored_only"] is True
    assert data["baseline_from"] == "stored_classifications"
    assert data["override_seller_count"] == 1
    # Scored sellers hold 2 + 1 of the 6 settlements; both stored values are at or
    # above the cutoff, so the baseline is 50.0% — the number the report would print
    # from the same stored row, not a rules-only recomputation.
    assert data["baseline_share_pct"] == 50.0


def _report_setup(
    tmp_path: Path,
    monkeypatch,
    baseline_pct: float,
    *,
    scored_only: bool = True,
) -> tuple[ModuleType, Path, Path]:
    db_path = _db(tmp_path)
    module = _load_script(GENERATE, "generate_report_under_test")

    # A fully coherent artefact: every field the producer writes, the counts the
    # database actually has (6 settlements, 3 sellers, 2 scored), and a maximum that
    # equals its own perturbations. Tests remove or tamper with one field at a time.
    baseline_share = round(baseline_pct / 100.0, 6)
    # N3: the guard requires exactly the key set the producer writes for the current
    # classifier — both globals plus two directions per `RuleThresholds` field.
    perturbations: dict[str, dict[str, float]] = {
        f"{field_name}_{direction}_25": {"shift_pp": 0.0}
        for field_name in asdict(DEFAULT_THRESHOLDS)
        for direction in ("down", "up")
    }
    perturbations["global_down_25"] = {"shift_pp": -1.0}
    perturbations["global_up_25"] = {"shift_pp": 1.0}
    sensitivity = tmp_path / "sensitivity.json"
    sensitivity.write_text(
        json.dumps(
            {
                "db_sha256": file_sha256(db_path),
                "scored_only": scored_only,
                "baseline_from": "stored_classifications",
                "perturbation_pct": 25,
                "flag_cutoff": 0.5,
                "baseline_share": baseline_share,
                "baseline_share_pct": baseline_pct,
                "settlement_count": 6,
                "seller_count": 3,
                "scored_seller_count": 2,
                "override_seller_count": 0,
                "max_share_shift_pp": 1.0,
                "perturbations": perturbations,
            }
        ),
        encoding="utf-8",
    )
    census = tmp_path / "token_census.json"
    census.write_text(
        json.dumps(
            {
                "window": {"from_block": 1, "to_block": 2},
                "total_settlements_in_window": 100,
                "tokens": [{"resolved": False, "settlement_count": 25}],
                "mechanism_paths": {
                    "eip3009_authorization_used": 100,
                    "unattributed_events": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "report.md"
    out_repro = tmp_path / "report.reproduce.json"

    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "DB_PATH", db_path)
    monkeypatch.setattr(module, "SENSITIVITY", sensitivity)
    monkeypatch.setattr(module, "TOKEN_CENSUS", census)
    monkeypatch.setattr(module, "COVERAGE", tmp_path / "missing-coverage.json")
    monkeypatch.setattr(module, "OUT", out)
    monkeypatch.setattr(module, "OUT_REPRO", out_repro)
    return module, out, out_repro


def test_the_generated_report_uses_the_same_boundary(tmp_path: Path, monkeypatch) -> None:
    module, out, out_repro = _report_setup(
        tmp_path, monkeypatch, baseline_pct=33.3333
    )

    assert module.main() == 0
    text = out.read_text(encoding="utf-8")

    assert "33.33% of settlements in this window belong to scored sellers" in text
    assert "The evidence minimum excludes **1** seller whose stored classification" in text
    assert "Scored sellers at or above the 0.5 likelihood cutoff: **1**" in text
    assert "| `flagged_seller_count` | 1 |" in text
    assert "| `flagged_settlement_share_pct` | 33.3333 |" in text
    assert "| `flagged_below_minimum_seller_count` | 1 |" in text
    assert "| `flagged_below_minimum_settlement_share_pct` | 50.0 |" in text
    assert "| `nonzero_likelihood_scored_sellers` | 1 |" in text

    sidecar = json.loads(out_repro.read_text(encoding="utf-8"))
    labels = {entry["label"] for entry in sidecar["entries"]}
    assert {
        "flagged_seller_count",
        "flagged_settlement_share_pct",
        "flagged_below_minimum_seller_count",
        "flagged_below_minimum_settlement_share_pct",
        "nonzero_likelihood_scored_sellers",
    } <= labels


@pytest.mark.parametrize(
    ("baseline_pct", "scored_only"),
    [(1.0, True), (33.3333, False)],
)
def test_the_generator_refuses_a_sensitivity_figure_that_describes_another_number(
    tmp_path: Path,
    monkeypatch,
    capsys,
    baseline_pct: float,
    scored_only: bool,
) -> None:
    module, out, _ = _report_setup(
        tmp_path, monkeypatch, baseline_pct=baseline_pct, scored_only=scored_only
    )

    assert module.main() == 1
    assert not out.exists()
    assert "re-run" in capsys.readouterr().out.lower()


def _sensitivity_data(module: ModuleType) -> dict:
    return json.loads(module.SENSITIVITY.read_text(encoding="utf-8"))


def _write_sensitivity(module: ModuleType, data: dict) -> None:
    module.SENSITIVITY.write_text(json.dumps(data), encoding="utf-8")


def test_the_generator_refuses_a_rules_only_artifact_whose_baseline_matches(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """M3: the pre-L1 implementation can produce a matching baseline by accident."""
    module, out, _ = _report_setup(tmp_path, monkeypatch, baseline_pct=33.3333)
    data = _sensitivity_data(module)
    del data["baseline_from"]
    _write_sensitivity(module, data)

    assert module.main() == 1
    assert "rules-only" in capsys.readouterr().out
    assert not out.exists()


def test_the_generator_refuses_a_maximum_its_own_perturbations_do_not_support(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    module, out, _ = _report_setup(tmp_path, monkeypatch, baseline_pct=33.3333)
    data = _sensitivity_data(module)
    data["max_share_shift_pp"] = 99.0
    _write_sensitivity(module, data)

    assert module.main() == 1
    assert "own perturbations" in capsys.readouterr().out
    assert not out.exists()


def test_the_generator_refuses_an_artifact_missing_a_global_perturbation(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    module, out, _ = _report_setup(tmp_path, monkeypatch, baseline_pct=33.3333)
    data = _sensitivity_data(module)
    del data["perturbations"]["global_up_25"]
    _write_sensitivity(module, data)

    assert module.main() == 1
    assert "global_up_25" in capsys.readouterr().out
    assert not out.exists()


def test_the_generator_refuses_an_artifact_from_another_population(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    module, out, _ = _report_setup(tmp_path, monkeypatch, baseline_pct=33.3333)
    data = _sensitivity_data(module)
    data["settlement_count"] = 999
    _write_sensitivity(module, data)

    assert module.main() == 1
    assert "settlement_count" in capsys.readouterr().out
    assert not out.exists()


def _open_uncheckpointed_writer(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    conn.execute("INSERT INTO runs (kind, status) VALUES ('test', 'running')")
    conn.commit()
    return conn


def test_a_database_with_uncheckpointed_wal_frames_has_no_provenance(
    tmp_path: Path,
) -> None:
    """M2: the main-file hash does not describe a database whose WAL holds changes."""
    db_path = _db(tmp_path)
    conn = _open_uncheckpointed_writer(db_path)
    try:
        with pytest.raises(provenance_module.PendingWALError) as excinfo:
            provenance_module.provenance(db_path)
        assert "wal_checkpoint" in str(excinfo.value)
    finally:
        conn.close()


def test_the_generator_refuses_a_database_with_uncheckpointed_wal(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    module, out, _ = _report_setup(tmp_path, monkeypatch, baseline_pct=33.3333)
    conn = _open_uncheckpointed_writer(module.DB_PATH)
    try:
        assert module.main() == 1
    finally:
        conn.close()

    captured = capsys.readouterr().out
    assert "uncheckpointed" in captured
    assert "wal_checkpoint" in captured
    assert not out.exists()


def _commit_transient(db_path: Path) -> None:
    """Commit a different classification state, left in the WAL."""
    writer = sqlite3.connect(db_path)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("UPDATE classifications SET signaling_likelihood = 0.0")
        writer.commit()
    finally:
        writer.close()


def _restore_and_checkpoint(db_path: Path) -> None:
    """Put the original values back and checkpoint so the main file returns to H."""
    writer = sqlite3.connect(db_path)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            "UPDATE classifications SET signaling_likelihood = CASE "
            "WHEN seller = ? THEN 0.8 WHEN seller = ? THEN 0.9 ELSE 0.0 END",
            (FLAGGED_SCORED, FLAGGED_UNSCORED),
        )
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        writer.close()


def test_data_version_sees_a_commit_that_restores_the_prior_state(tmp_path: Path) -> None:
    """O1: the logical state recurs, but SQLite's change counter does not.

    Whether the restored bytes are identical depends on page layout; data_version
    moves for every commit by another connection, so the check cannot be defeated by
    a writer that puts the rows back (the tenth review's byte-identical case is the
    subset where the file hash also recurs).
    """
    db_path = _db(tmp_path)
    original = sqlite3.connect(db_path).execute(
        "SELECT seller, signaling_likelihood FROM classifications ORDER BY seller"
    ).fetchall()
    reader = sqlite3.connect(db_path)
    try:
        before = provenance_module.data_version(reader)
        _commit_transient(db_path)
        _restore_and_checkpoint(db_path)
        after = provenance_module.data_version(reader)
    finally:
        reader.close()

    restored = sqlite3.connect(db_path).execute(
        "SELECT seller, signaling_likelihood FROM classifications ORDER BY seller"
    ).fetchall()
    assert restored == original
    assert before != after
    with pytest.raises(provenance_module.DatabaseChangedError) as excinfo:
        provenance_module.require_no_commits(db_path, before, after)
    assert "another connection committed" in str(excinfo.value)


def test_sensitivity_refuses_a_commit_that_is_undone_before_the_confirmation(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """O1: features and classifications read across a transient state must refuse."""
    db_path = _db(tmp_path)
    original = db_path.read_bytes()
    module = _load_script(SENSITIVITY, "sensitivity_under_test")
    out_path = tmp_path / "sensitivity.json"
    monkeypatch.setattr(module, "DB_PATH", db_path)
    monkeypatch.setattr(module, "OUT_PATH", out_path)
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)

    real = module.stored_classifications

    def _transient_read(conn: sqlite3.Connection) -> tuple[dict[str, float], set[str]]:
        _commit_transient(db_path)
        result = real(conn)  # reads the transient state
        _restore_and_checkpoint(db_path)
        # Byte-identical restore: the confirming hash cannot see the commit, so only
        # the change counter can (reviews O1, P1).
        db_path.write_bytes(original)
        return result

    monkeypatch.setattr(module, "stored_classifications", _transient_read)

    assert module.main() == 1
    assert "another connection committed" in capsys.readouterr().out
    assert not out_path.exists()
    restored = sqlite3.connect(db_path).execute(
        "SELECT seller, signaling_likelihood FROM classifications ORDER BY seller"
    ).fetchall()
    assert restored == [
        (FLAGGED_SCORED, 0.8),
        (FLAGGED_UNSCORED, 0.9),
        (CLEAN_SCORED, 0.0),
    ]


def test_the_observer_refuses_a_commit_made_and_undone_while_it_is_open(
    tmp_path: Path,
) -> None:
    """P1: the observed window spans the attestation hash, not only the reads."""
    db_path = _db(tmp_path)
    observer = provenance_module.CommitObserver(db_path)
    try:
        _commit_transient(db_path)
        _restore_and_checkpoint(db_path)
        with pytest.raises(provenance_module.DatabaseChangedError) as excinfo:
            observer.require_quiet()
    finally:
        observer.close()
    assert "another connection committed" in str(excinfo.value)


def test_the_generator_refuses_a_commit_made_while_the_attestation_hash_runs(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """P1: a commit during the 1.6 GB hash cannot hide behind a restored hash.

    The wrapper hashes the checkpointed state, commits a transient classification
    state, restores the file to its exact original bytes and returns the original
    hash. Every hash check passes — the artefact's hash, the attestation and the
    confirming hash are the same bytes — and only the observer, opened before the
    hash, sees that the database moved.
    """
    module, out, _ = _report_setup(tmp_path, monkeypatch, baseline_pct=33.3333)
    db_path = module.DB_PATH
    original = db_path.read_bytes()
    real_attest = module.attest_identity

    def _attest_across_an_undone_commit(path: Path) -> str:
        attested = real_attest(path)
        _commit_transient(path)
        _restore_and_checkpoint(path)
        path.write_bytes(original)
        return attested

    monkeypatch.setattr(module, "attest_identity", _attest_across_an_undone_commit)

    assert module.main() == 1
    assert "another connection committed" in capsys.readouterr().out
    assert not out.exists()


def test_sensitivity_refuses_a_database_with_uncheckpointed_wal(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    db_path = _db(tmp_path)
    module = _load_script(SENSITIVITY, "sensitivity_under_test")
    out_path = tmp_path / "sensitivity.json"
    monkeypatch.setattr(module, "DB_PATH", db_path)
    monkeypatch.setattr(module, "OUT_PATH", out_path)
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)

    conn = _open_uncheckpointed_writer(db_path)
    try:
        assert module.main() == 1
    finally:
        conn.close()

    assert "uncheckpointed" in capsys.readouterr().out
    assert not out_path.exists()


# --------------------------------------------------------------------------- #
# N1 — the identity attested before the reads is confirmed after them
# --------------------------------------------------------------------------- #


def test_require_unchanged_refuses_a_checkpointed_change(tmp_path: Path) -> None:
    """A writer that commits and checkpoints rewrites the main file under the reads."""
    db_path = _db(tmp_path)
    attested = provenance_module.attest_identity(db_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE classifications SET signaling_likelihood = 0.0")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    assert file_sha256(db_path) != attested
    with pytest.raises(provenance_module.DatabaseChangedError):
        provenance_module.require_unchanged(db_path, attested)


def test_require_unchanged_refuses_a_wal_only_change(tmp_path: Path) -> None:
    """A commit that stays in the WAL leaves the main hash unchanged but not the state."""
    db_path = _db(tmp_path)
    attested = provenance_module.attest_identity(db_path)

    conn = _open_uncheckpointed_writer(db_path)
    try:
        with pytest.raises(provenance_module.PendingWALError):
            provenance_module.require_unchanged(db_path, attested)
    finally:
        conn.close()


def test_the_generator_refuses_a_database_that_changes_during_the_reads(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """N1: a commit between the attestation and the reads must refuse, not publish."""
    module, out, _ = _report_setup(tmp_path, monkeypatch, baseline_pct=33.3333)
    real_connect = module.connect_readonly

    def _connect_after_a_change(path: Path, **kwargs: object) -> sqlite3.Connection:
        writer = sqlite3.connect(path)
        try:
            writer.execute("UPDATE classifications SET signaling_likelihood = 0.0")
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            writer.close()
        return real_connect(path, **kwargs)

    monkeypatch.setattr(module, "connect_readonly", _connect_after_a_change)

    assert module.main() == 1
    assert "not generated from a state it cannot name" in capsys.readouterr().out
    assert not out.exists()


def test_sensitivity_refuses_a_database_that_changes_during_the_reads(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """N1: the artefact must not bind one state's data to another state's hash."""
    db_path = _db(tmp_path)
    module = _load_script(SENSITIVITY, "sensitivity_under_test")
    out_path = tmp_path / "sensitivity.json"
    monkeypatch.setattr(module, "DB_PATH", db_path)
    monkeypatch.setattr(module, "OUT_PATH", out_path)
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)

    real = module.stored_classifications

    def _read_then_change(conn: sqlite3.Connection) -> tuple[dict[str, float], set[str]]:
        result = real(conn)
        writer = sqlite3.connect(db_path)
        try:
            writer.execute("UPDATE classifications SET signaling_likelihood = 0.0")
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            writer.close()
        return result

    monkeypatch.setattr(module, "stored_classifications", _read_then_change)

    assert module.main() == 1
    assert "not written from a state it cannot name" in capsys.readouterr().out
    assert not out_path.exists()


# --------------------------------------------------------------------------- #
# N3 — the guard must require the whole perturbation set and finite numbers
# --------------------------------------------------------------------------- #


def test_the_generator_refuses_a_truncated_perturbation_table(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Globals alone cannot support 'every threshold and weight was scaled'."""
    module, out, _ = _report_setup(tmp_path, monkeypatch, baseline_pct=33.3333)
    data = _sensitivity_data(module)
    for key in [k for k in data["perturbations"] if k.startswith("peak_hour_share_")]:
        del data["perturbations"][key]
    _write_sensitivity(module, data)

    assert module.main() == 1
    assert "peak_hour_share" in capsys.readouterr().out
    assert not out.exists()


def test_the_generator_refuses_a_non_finite_shift(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    module, out, _ = _report_setup(tmp_path, monkeypatch, baseline_pct=33.3333)
    data = _sensitivity_data(module)
    data["perturbations"]["global_down_25"]["shift_pp"] = float("nan")
    _write_sensitivity(module, data)

    assert module.main() == 1
    assert "non-finite" in capsys.readouterr().out
    assert not out.exists()


def test_the_generator_refuses_a_non_finite_maximum(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    module, out, _ = _report_setup(tmp_path, monkeypatch, baseline_pct=33.3333)
    data = _sensitivity_data(module)
    data["max_share_shift_pp"] = float("inf")
    _write_sensitivity(module, data)

    assert module.main() == 1
    assert "non-finite maximum" in capsys.readouterr().out
    assert not out.exists()


def test_the_generator_requires_the_override_count(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    module, out, _ = _report_setup(tmp_path, monkeypatch, baseline_pct=33.3333)
    data = _sensitivity_data(module)
    del data["override_seller_count"]
    _write_sensitivity(module, data)

    assert module.main() == 1
    assert "override_seller_count" in capsys.readouterr().out
    assert not out.exists()
