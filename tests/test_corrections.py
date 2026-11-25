"""Corrections apply, and their audit trail is append-only (task B5, GB.10).

The independent review (F17) found this file inserted rows with SQL and never ran
`scripts/apply_correction.py`, so it proved nothing about the script or its effect.
Every behavioural test below drives the CLI in a subprocess and then checks the
database it wrote: a correction must change the data *and* leave a complete audit row.

The audit trail property is tested on the table, not trusted from a docstring: a
reversal is a new row, the original row is byte-identical, and nothing updates or
deletes a correction. The correction's *effect* lives in the derived tables, so
undoing the effect is allowed; editing the record is not.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from eth_utils import to_checksum_address
from fastapi.testclient import TestClient

from repindex.api import app as app_module
from repindex.api import queries
from repindex.common.version import METHODOLOGY_VERSION
from repindex.ingest import schema
from repindex.mcp import server
from repindex.score import rating

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "apply_correction.py"
REBUILD = REPO_ROOT / "scripts" / "rebuild_derived.py"

SETTLEMENT = {
    "tx_hash": "0x" + "ab" * 32,
    "log_index": 7,
    "block_number": 53_200_001,
    "block_hash": "0x" + "cd" * 32,
    "block_time": 1_793_000_000,
    "token": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "decimals": 6,
    "raw_amount": 2_500_000,
    "normalized_amount": "2.5",
    "payer": "0x" + "11" * 20,
    "seller": "0x" + "22" * 20,
    "facilitator_relayer": None,
    "nonce": "0x" + "33" * 32,
    "mechanism": "eip3009_authorization_used",
}


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "corrections.db"
    schema.initialize(path)
    return path


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _run(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db_path), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
    )


def _record(
    db_path: Path, *extra: str, subject: str = "0x" + "aa" * 20
) -> subprocess.CompletedProcess[str]:
    return _run(
        db_path,
        "--subject",
        subject,
        "--claim",
        "these three addresses are not one operator",
        "--evidence",
        "signed message from each key",
        "--decision",
        "upheld",
        "--reason",
        "the cluster was formed from shared payers alone",
        "--applied-by",
        "owner",
        *extra,
    )


def _seed_label(
    path: Path, *, label_type: str = "operator_group", value: str = "cluster:61"
) -> None:
    conn = _connect(path)
    try:
        conn.execute(
            "INSERT INTO labels (address, label_type, label_value, method, confidence, "
            "evidence, methodology_version) VALUES (?,?,?,?,?,?,?)",
            ("0x" + "aa" * 20, label_type, value, "shared_payers", 0.6885, "three addresses", "v1"),
        )
        conn.commit()
    finally:
        conn.close()


SELLER = "0x" + "aa" * 20
PAYERS = tuple("0x" + f"{n:040x}" for n in range(1, 7))


def _seed_activity(path: Path, count: int = 24) -> None:
    """Enough settlements for the rules to classify *and score* one seller.

    G2's scenario needs a real classification row: the override replaces a row the
    recompute produces, and a rebuild of a database with no features would rightly
    have nothing to re-apply the override to. Six payers over four days also clear
    the evidence minimum, so the score itself is published and the override's effect
    on it can be asserted.
    """
    conn = _connect(path)
    try:
        for index in range(count):
            digest = f"{index + 1:064x}"
            conn.execute(
                "INSERT INTO settlements (tx_hash, log_index, block_number, block_hash, "
                "block_time, token, decimals, raw_amount, normalized_amount, payer, "
                "seller, facilitator_relayer, nonce, mechanism) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "0x" + digest,
                    0,
                    53_200_000 + index,
                    "0x" + "cd" * 32,
                    1_793_000_000 + (index // len(PAYERS)) * 86_400 + (index % len(PAYERS)) * 60,
                    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                    6,
                    1_000_000,
                    "1.0",
                    PAYERS[index % len(PAYERS)],
                    SELLER,
                    None,
                    "0x" + digest,
                    "eip3009_authorization_used",
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _rebuild(path: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(REBUILD), "--db", str(path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
    )


def _classification(path: Path) -> sqlite3.Row:
    conn = _connect(path)
    try:
        row = conn.execute("SELECT * FROM classifications").fetchone()
    finally:
        conn.close()
    assert row is not None, "the rebuild produced no classification"
    return row


def _labels(path: Path) -> list[sqlite3.Row]:
    conn = _connect(path)
    try:
        return list(conn.execute("SELECT * FROM labels"))
    finally:
        conn.close()


def test_the_cli_records_a_full_provenance(db_path: Path) -> None:
    result = _record(db_path)

    assert result.returncode == 0, result.stdout + result.stderr
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM corrections").fetchone()
    finally:
        conn.close()
    assert row["subject"] == "0x" + "aa" * 20
    assert row["decision"] == "upheld"
    assert row["evidence"]
    assert row["reason"]
    assert row["methodology_version"] == METHODOLOGY_VERSION
    assert row["applied_at"], "an audit trail without a timestamp is not an audit trail"
    assert json.loads(row["new_state"])["action"] == "none"


def test_remove_label_changes_the_data_and_records_the_previous_state(db_path: Path) -> None:
    _seed_label(db_path)

    result = _record(
        db_path,
        "--action",
        "remove_label",
        "--label-type",
        "operator_group",
        "--label-value",
        "cluster:61",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _labels(db_path) == [], "an upheld correction must actually remove the label"
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM corrections").fetchone()
    finally:
        conn.close()
    previous = json.loads(row["previous_state"])
    assert previous["method"] == "shared_payers"
    assert json.loads(row["new_state"]) == {
        "action": "remove_label",
        "label_type": "operator_group",
        "label_value": "cluster:61",
    }


def test_add_label_records_a_missing_interpretation(db_path: Path) -> None:
    result = _record(
        db_path,
        "--action",
        "add_label",
        "--label-type",
        "role",
        "--label-value",
        "seller",
        "--confidence",
        "0.9",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    rows = _labels(db_path)
    assert len(rows) == 1
    assert rows[0]["label_type"] == "role"
    assert rows[0]["label_value"] == "seller"
    assert rows[0]["method"] == "dispute"
    assert rows[0]["confidence"] == pytest.approx(0.9)
    assert rows[0]["methodology_version"] == METHODOLOGY_VERSION


def test_a_checksummed_subject_addresses_the_same_identity(db_path: Path) -> None:
    """H2: the read path lowercases, so a checksummed subject missed the stored row."""
    _seed_label(db_path)
    checksummed = to_checksum_address(SELLER)
    assert checksummed != SELLER

    result = _record(
        db_path,
        "--action",
        "remove_label",
        "--label-type",
        "operator_group",
        "--label-value",
        "cluster:61",
        subject=checksummed,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _labels(db_path) == [], "a checksummed subject must remove the stored label"
    conn = _connect(db_path)
    try:
        stored = conn.execute("SELECT subject FROM corrections").fetchone()["subject"]
    finally:
        conn.close()
    assert stored == SELLER, "the audit row stores the identity the index can look up"


def test_a_label_added_under_a_checksummed_subject_is_visible_to_the_lowercase_reader(
    db_path: Path,
) -> None:
    result = _record(
        db_path,
        "--action",
        "add_label",
        "--label-type",
        "role",
        "--label-value",
        "seller",
        subject=to_checksum_address(SELLER),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    rows = _labels(db_path)
    assert [(row["address"], row["label_value"]) for row in rows] == [(SELLER, "seller")]


def test_a_did_subject_is_stored_as_the_address_it_names(db_path: Path) -> None:
    result = _record(
        db_path,
        "--action",
        "add_label",
        "--label-type",
        "role",
        "--label-value",
        "seller",
        subject=f"did:ethr:8453:{SELLER}",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    rows = _labels(db_path)
    assert [row["address"] for row in rows] == [SELLER]


def test_a_subject_with_a_broken_checksum_is_rejected(db_path: Path) -> None:
    checksummed = to_checksum_address(SELLER)
    broken = checksummed.replace("A", "a", 1)
    assert broken != checksummed

    result = _record(
        db_path,
        "--action",
        "add_label",
        "--label-type",
        "role",
        "--label-value",
        "seller",
        subject=broken,
    )

    assert result.returncode == 1
    assert "checksum" in result.stdout
    conn = _connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM corrections").fetchone()[0] == 0
    finally:
        conn.close()


def test_add_settlement_normalises_addresses_to_the_stored_identity(
    db_path: Path, tmp_path: Path
) -> None:
    """H2: a checksummed payer/seller split the seller into a second identity."""
    payload_data = {
        **SETTLEMENT,
        "token": to_checksum_address(SETTLEMENT["token"]),
        "payer": to_checksum_address("0x" + "ab" * 20),
        "seller": to_checksum_address("0x" + "cd" * 20),
        "facilitator_relayer": to_checksum_address("0x" + "ef" * 20),
    }
    payload = tmp_path / "settlement.json"
    payload.write_text(json.dumps(payload_data), encoding="utf-8")

    result = _record(db_path, "--action", "add_settlement", "--settlement", str(payload))

    assert result.returncode == 0, result.stdout + result.stderr
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT token, payer, seller, facilitator_relayer FROM settlements"
        ).fetchone()
    finally:
        conn.close()
    assert (row["token"], row["payer"], row["seller"], row["facilitator_relayer"]) == (
        SETTLEMENT["token"].lower(),
        "0x" + "ab" * 20,
        "0x" + "cd" * 20,
        "0x" + "ef" * 20,
    )


def test_add_settlement_inserts_the_missing_record(db_path: Path, tmp_path: Path) -> None:
    payload = tmp_path / "settlement.json"
    payload.write_text(json.dumps(SETTLEMENT), encoding="utf-8")

    result = _record(db_path, "--action", "add_settlement", "--settlement", str(payload))

    assert result.returncode == 0, result.stdout + result.stderr
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM settlements").fetchone()
    finally:
        conn.close()
    assert row["tx_hash"] == SETTLEMENT["tx_hash"]
    assert row["raw_amount"] == 2_500_000
    assert row["normalized_amount"] == "2.5"


def test_add_settlement_treats_a_case_variant_of_a_known_log_as_duplicate(
    db_path: Path, tmp_path: Path
) -> None:
    """I1: SQLite's BINARY comparison made '0XAB..' a *different* tx_hash.

    The fourth review inserted the same on-chain log twice this way: 25 settlement
    rows for 24 logs, and the API reported evidence that does not exist.
    """
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO settlements (tx_hash, log_index, block_number, block_hash, "
            "block_time, token, decimals, raw_amount, normalized_amount, payer, seller, "
            "facilitator_relayer, nonce, mechanism) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(
                SETTLEMENT[field]
                for field in (
                    "tx_hash",
                    "log_index",
                    "block_number",
                    "block_hash",
                    "block_time",
                    "token",
                    "decimals",
                    "raw_amount",
                    "normalized_amount",
                    "payer",
                    "seller",
                    "facilitator_relayer",
                    "nonce",
                    "mechanism",
                )
            ),
        )
        conn.commit()
    finally:
        conn.close()

    variant = {**SETTLEMENT, "tx_hash": SETTLEMENT["tx_hash"].upper()}
    payload = tmp_path / "variant.json"
    payload.write_text(json.dumps(variant), encoding="utf-8")

    result = _record(db_path, "--action", "add_settlement", "--settlement", str(payload))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "already present" in result.stdout
    conn = _connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 1, (
            "a case variant of a stored tx_hash inserted a duplicate log"
        )
    finally:
        conn.close()


def test_add_settlement_stores_canonical_lowercase_hashes(
    db_path: Path, tmp_path: Path
) -> None:
    variant = {
        **SETTLEMENT,
        "tx_hash": SETTLEMENT["tx_hash"].upper(),
        "block_hash": SETTLEMENT["block_hash"].upper(),
        "nonce": SETTLEMENT["nonce"].upper(),
    }
    payload = tmp_path / "settlement.json"
    payload.write_text(json.dumps(variant), encoding="utf-8")

    result = _record(db_path, "--action", "add_settlement", "--settlement", str(payload))

    assert result.returncode == 0, result.stdout + result.stderr
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT tx_hash, block_hash, nonce FROM settlements").fetchone()
    finally:
        conn.close()
    assert (row["tx_hash"], row["block_hash"], row["nonce"]) == (
        SETTLEMENT["tx_hash"],
        SETTLEMENT["block_hash"],
        SETTLEMENT["nonce"],
    )


def test_the_settlement_duplicate_check_uses_an_index(db_path: Path) -> None:
    """J2: `tx_hash = ? COLLATE NOCASE` full-scanned the settlements table.

    Without a collation-aware index the duplicate check took 18-32 s on the production
    database for every `add_settlement` correction, against 1.1 ms for the BINARY key.
    """
    conn = _connect(db_path)
    try:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM settlements "
            "WHERE tx_hash = ? COLLATE NOCASE AND log_index = ?",
            (SETTLEMENT["tx_hash"], SETTLEMENT["log_index"]),
        ).fetchall()
    finally:
        conn.close()
    detail = " ".join(str(row["detail"]) for row in plan)

    assert "SCAN settlements" not in detail, detail
    assert "idx_settlements_tx_nocase" in detail, detail


def test_add_settlement_rejects_a_malformed_tx_hash(db_path: Path, tmp_path: Path) -> None:
    payload = tmp_path / "settlement.json"
    payload.write_text(json.dumps({**SETTLEMENT, "tx_hash": "0x1234"}), encoding="utf-8")

    result = _record(db_path, "--action", "add_settlement", "--settlement", str(payload))

    assert result.returncode == 1
    assert "tx_hash" in result.stdout
    conn = _connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM corrections").fetchone()[0] == 0
    finally:
        conn.close()


def test_add_settlement_rejects_an_amount_that_does_not_match_the_raw_value(
    db_path: Path, tmp_path: Path
) -> None:
    payload = tmp_path / "settlement.json"
    payload.write_text(json.dumps({**SETTLEMENT, "normalized_amount": "25.0"}), encoding="utf-8")

    result = _record(db_path, "--action", "add_settlement", "--settlement", str(payload))

    assert result.returncode == 1
    assert "does not equal" in result.stdout
    conn = _connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM corrections").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_rejected_dispute_changes_nothing(db_path: Path) -> None:
    _seed_label(db_path)
    result = _run(
        db_path,
        "--subject",
        "0x" + "aa" * 20,
        "--claim",
        "not one operator",
        "--evidence",
        "an unsigned assertion",
        "--decision",
        "not_upheld",
        "--reason",
        "no checkable evidence",
        "--action",
        "remove_label",
        "--label-type",
        "operator_group",
        "--label-value",
        "cluster:61",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(_labels(db_path)) == 1
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM corrections").fetchone()
    finally:
        conn.close()
    assert json.loads(row["new_state"]) == {"action": "none"}


def test_a_reversal_adds_a_row_and_restores_the_removed_label(db_path: Path) -> None:
    _seed_label(db_path)
    assert (
        _record(
            db_path,
            "--action",
            "remove_label",
            "--label-type",
            "operator_group",
            "--label-value",
            "cluster:61",
        ).returncode
        == 0
    )

    result = _run(
        db_path, "--reverse", "1", "--reason", "evidence retracted", "--applied-by", "owner"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    rows = _labels(db_path)
    assert len(rows) == 1 and rows[0]["method"] == "shared_payers"
    conn = _connect(db_path)
    try:
        original = dict(conn.execute("SELECT * FROM corrections").fetchone())
        reversals = conn.execute("SELECT * FROM correction_reversals").fetchall()
    finally:
        conn.close()
    assert original["decision"] == "upheld", "a reversal must not modify the original row"
    assert len(reversals) == 1
    assert reversals[0]["reverses"] == 1


def test_a_reversal_of_an_added_label_removes_it(db_path: Path) -> None:
    assert (
        _record(
            db_path, "--action", "add_label", "--label-type", "role", "--label-value", "seller"
        ).returncode
        == 0
    )
    assert len(_labels(db_path)) == 1

    result = _run(db_path, "--reverse", "1", "--reason", "added in error", "--applied-by", "owner")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _labels(db_path) == []


def test_a_reversal_must_point_at_a_real_correction(db_path: Path) -> None:
    result = _run(
        db_path, "--reverse", "9999", "--reason", "no such correction", "--applied-by", "owner"
    )

    assert result.returncode == 1
    assert "no correction with id 9999" in result.stdout


def test_a_settlement_stays_when_its_correction_is_reversed(db_path: Path, tmp_path: Path) -> None:
    payload = tmp_path / "settlement.json"
    payload.write_text(json.dumps(SETTLEMENT), encoding="utf-8")
    assert (
        _record(db_path, "--action", "add_settlement", "--settlement", str(payload)).returncode == 0
    )

    result = _run(db_path, "--reverse", "1", "--reason", "disputed later", "--applied-by", "owner")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "stays" in result.stdout
    conn = _connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 1
    finally:
        conn.close()


def test_a_rebuild_re_applies_active_corrections_instead_of_erasing_them(db_path: Path) -> None:
    """The rebuild wipes `labels`; an active correction must survive the wipe."""
    assert (
        _record(
            db_path, "--action", "add_label", "--label-type", "role", "--label-value", "seller"
        ).returncode
        == 0
    )
    assert len(_labels(db_path)) == 1

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    rebuild = subprocess.run(
        [sys.executable, str(REBUILD), "--db", str(db_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
    )

    assert rebuild.returncode == 0, rebuild.stdout + rebuild.stderr
    rows = _labels(db_path)
    assert len(rows) == 1, "the rebuild erased a correction instead of re-applying it"
    assert rows[0]["method"] == "dispute"


def test_an_upheld_classification_override_changes_the_published_value(db_path: Path) -> None:
    """G2: a classification was listed as disputable and nothing could change it."""
    _seed_activity(db_path)
    assert _rebuild(db_path).returncode == 0
    computed = float(_classification(db_path)["signaling_likelihood"])
    target = 0.1 if computed > 0.5 else 0.9

    result = _record(
        db_path, "--action", "override_classification", "--likelihood", str(target)
    )

    assert result.returncode == 0, result.stdout + result.stderr
    row = _classification(db_path)
    assert float(row["signaling_likelihood"]) == pytest.approx(target)
    contributions = json.loads(row["contributions"])
    overrides = [c for c in contributions if c["rule"] == "dispute_override"]
    assert len(overrides) == 1
    assert overrides[0]["computed_before"] == pytest.approx(computed)
    assert "the rules computed" in overrides[0]["detail"]

    conn = _connect(db_path)
    try:
        previous = json.loads(conn.execute("SELECT previous_state FROM corrections").fetchone()[0])
    finally:
        conn.close()
    assert previous["signaling_likelihood"] == pytest.approx(computed)


def _score(path: Path) -> float | None:
    conn = _connect(path)
    try:
        row = conn.execute("SELECT score FROM scores WHERE identifier = ?", (SELLER,)).fetchone()
    finally:
        conn.close()
    assert row is not None, "the rebuild produced no score row for the seller"
    return row["score"]


def test_a_classification_override_survives_a_rebuild_and_moves_the_score(db_path: Path) -> None:
    """The published score must use the disputed value after a rebuild, not the rules'."""
    _seed_activity(db_path)
    assert _rebuild(db_path).returncode == 0
    computed_score = _score(db_path)
    computed_likelihood = float(_classification(db_path)["signaling_likelihood"])
    assert computed_score is not None, "the seeded seller must clear the evidence minimum"
    assert (
        _record(
            db_path, "--action", "override_classification", "--likelihood", "0.9"
        ).returncode
        == 0
    )

    rebuild = _rebuild(db_path)

    assert rebuild.returncode == 0, rebuild.stdout + rebuild.stderr
    row = _classification(db_path)
    assert float(row["signaling_likelihood"]) == pytest.approx(0.9), (
        "the rebuild recomputed the classification and erased the upheld dispute"
    )
    overrides = [c for c in json.loads(row["contributions"]) if c["rule"] == "dispute_override"]
    assert len(overrides) == 1, "re-application must not append a second override entry"
    # score = 100 * evidence_strength * (1 - likelihood), so the override's effect on
    # the published score is proportional. Deriving it from the computed score keeps
    # the assertion about the override rather than about the strength factor.
    expected = round(computed_score * (1.0 - 0.9) / (1.0 - computed_likelihood), 2)
    assert _score(db_path) == pytest.approx(expected, abs=0.02), (
        "the published score was computed from the rules' value, not the override"
    )
    assert _score(db_path) != pytest.approx(computed_score)


def test_reversing_a_classification_override_restores_the_rules_value(db_path: Path) -> None:
    _seed_activity(db_path)
    assert _rebuild(db_path).returncode == 0
    computed = float(_classification(db_path)["signaling_likelihood"])
    assert (
        _record(
            db_path, "--action", "override_classification", "--likelihood", "0.9"
        ).returncode
        == 0
    )

    result = _run(
        db_path, "--reverse", "1", "--reason", "evidence retracted", "--applied-by", "owner"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    row = _classification(db_path)
    assert float(row["signaling_likelihood"]) == pytest.approx(computed)
    overrides = [c for c in json.loads(row["contributions"]) if c["rule"] == "dispute_override"]
    assert overrides == []


def test_reversing_an_override_after_the_evidence_changed_recomputes_the_rules(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I3: the reversal restored a snapshot that described older evidence.

    The fourth review recorded an override, then more evidence arrived, then reversed
    the override without a rebuild. The restored classification said 24 settlements
    and 6 payers while the API's evidence block said 25 and 7, and its likelihood came
    from rules that no longer matched the features.
    """
    _seed_activity(db_path)
    assert _rebuild(db_path).returncode == 0
    stale = float(_classification(db_path)["signaling_likelihood"])
    assert (
        _record(
            db_path, "--action", "override_classification", "--likelihood", "0.9"
        ).returncode
        == 0
    )

    # Evidence arrives after the dispute: a seventh payer and an amount unlike the
    # seeded rows, which are all 1.0.
    extra = {**SETTLEMENT, "payer": "0x" + "77" * 20, "seller": SELLER}
    payload = tmp_path / "extra.json"
    payload.write_text(json.dumps(extra), encoding="utf-8")
    assert (
        _record(db_path, "--action", "add_settlement", "--settlement", str(payload)).returncode
        == 0
    )
    assert _rebuild(db_path).returncode == 0
    assert float(_classification(db_path)["signaling_likelihood"]) == pytest.approx(0.9), (
        "the override must survive the rebuild for this scenario to test anything"
    )

    result = _run(
        db_path, "--reverse", "1", "--reason", "dispute withdrawn", "--applied-by", "owner"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "current evidence" in result.stdout
    payload_after = _api_score(monkeypatch, db_path)
    assert payload_after["evidence"]["settlements"] == 25
    assert payload_after["evidence"]["counterparties"] == 7
    likelihood = float(payload_after["interpretation"]["signaling_likelihood"])
    assert likelihood != pytest.approx(stale), (
        "the reversal restored the classification computed from the older evidence"
    )
    thin = next(
        rule
        for rule in payload_after["interpretation"]["rules"]
        if rule["rule"] == "thin_payer_base"
    )
    assert "25 settlements" in thin["detail"]
    assert "7 distinct payers" in thin["detail"], (
        "the restored rules contradict the settlement counts in the same response"
    )

    # The reversal must leave exactly what a rebuild would compute: a later rebuild
    # finds no active correction and recomputes from the same features.
    assert _rebuild(db_path).returncode == 0
    payload_after_rebuild = _api_score(monkeypatch, db_path)
    assert float(payload_after_rebuild["interpretation"]["signaling_likelihood"]) == (
        pytest.approx(likelihood)
    )
    assert payload_after_rebuild["score"] == pytest.approx(payload_after["score"])


def test_an_override_on_an_unscored_seller_publishes_no_likelihood(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """J1: the rules block said "set to 0.9" while the response served null.

    A seller below the evidence minimum is not judged, so the API publishes no
    likelihood and no score. The correction must not smuggle a number into the same
    response through its detail or contribution.
    """
    _seed_activity(db_path, count=6)
    assert _rebuild(db_path).returncode == 0
    assert (
        _record(
            db_path, "--action", "override_classification", "--likelihood", "0.9"
        ).returncode
        == 0
    )

    payload = _api_score(monkeypatch, db_path)
    assert payload["status"] == "insufficient_evidence"
    assert payload["score"] is None
    assert payload["interpretation"]["signaling_likelihood"] is None
    override = next(
        rule
        for rule in payload["interpretation"]["rules"]
        if rule["rule"] == "dispute_override"
    )
    assert override["fired"] is False, "an override is not a fired classifier rule"
    assert override["contribution"] == 0.0
    assert "not scored" in override["detail"]

    body = server.check_counterparty(SELLER)
    assert body["signaling_likelihood"] is None
    assert body["judged"] is False
    assert body["correction"]["fired"] is False
    assert body["correction"]["contribution"] == 0.0
    assert "not scored" in body["correction"]["detail"]


def test_reversing_the_later_override_restores_the_earlier_one(db_path: Path) -> None:
    """A seller can carry a second dispute that replaced the first.

    Reversing the later one must leave the earlier upheld override in place -- which
    is also what a rebuild would leave -- rather than falling back to the rules.
    """
    _seed_activity(db_path)
    assert _rebuild(db_path).returncode == 0
    assert (
        _record(
            db_path, "--action", "override_classification", "--likelihood", "0.2"
        ).returncode
        == 0
    )
    assert (
        _record(
            db_path, "--action", "override_classification", "--likelihood", "0.8"
        ).returncode
        == 0
    )
    assert float(_classification(db_path)["signaling_likelihood"]) == pytest.approx(0.8)

    result = _run(
        db_path, "--reverse", "2", "--reason", "later dispute withdrawn", "--applied-by", "owner"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "earlier upheld override" in result.stdout
    assert float(_classification(db_path)["signaling_likelihood"]) == pytest.approx(0.2), (
        "the earlier upheld override is still active but was not restored"
    )


THRESHOLDS = REPO_ROOT / "config" / "thresholds.yaml"


def _api_score(monkeypatch: pytest.MonkeyPatch, path: Path) -> dict:
    """Call the real `/v1/score` route against the throwaway database."""
    monkeypatch.setattr(queries, "DEFAULT_DB", path)
    monkeypatch.setattr(app_module, "_USAGE_STORE", None)
    monkeypatch.setattr(app_module, "_USAGE_READY", True)
    response = TestClient(app_module.app).get(f"/v1/score/{SELLER}")
    assert response.status_code == 200, response.text
    return response.json()


def _score_from_the_published_pair(payload: dict) -> float:
    """The score the documented formula produces from the response's own inputs."""
    minimum = rating.EvidenceMinimum.from_thresholds(
        yaml.safe_load(THRESHOLDS.read_text(encoding="utf-8")) or {}
    )
    evidence = payload["evidence"]
    expected = rating.score_seller(
        seller=SELLER,
        settlements=int(evidence["settlements"]),
        counterparties=int(evidence["counterparties"]),
        active_days=int(evidence["active_days"]),
        signaling_likelihood=float(payload["interpretation"]["signaling_likelihood"]),
        window_start=0,
        window_end=0,
        as_of_block=0,
        minimum=minimum,
    )
    assert expected.score is not None
    return expected.score


def test_the_published_score_follows_an_override_without_a_rebuild(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1: the API must not publish a likelihood and a score that cannot both be true.

    The reviewer observed a likelihood of 0.9 beside a score of 18.67, which implies
    an evidence strength of 1.867 against a documented cap of 1.0, and after the
    reversal a likelihood of 0.4 beside the score computed from 0.9.
    """
    _seed_activity(db_path)
    assert _rebuild(db_path).returncode == 0
    computed_score = _score(db_path)
    computed_likelihood = float(_classification(db_path)["signaling_likelihood"])
    target = 0.9 if computed_likelihood < 0.5 else 0.1
    assert computed_score is not None

    assert (
        _record(
            db_path, "--action", "override_classification", "--likelihood", str(target)
        ).returncode
        == 0
    )

    payload = _api_score(monkeypatch, db_path)
    likelihood = float(payload["interpretation"]["signaling_likelihood"])
    assert likelihood == pytest.approx(target)
    assert payload["score"] == pytest.approx(_score_from_the_published_pair(payload)), (
        "the score was computed from the rules' likelihood, not the upheld override"
    )
    # implied evidence strength = score / (100 * (1 - likelihood)) must stay in [0, 1]
    assert payload["score"] <= 100.0 * (1.0 - likelihood) + 1e-9
    assert payload["score"] != pytest.approx(computed_score)

    reverse = _run(
        db_path, "--reverse", "1", "--reason", "evidence retracted", "--applied-by", "owner"
    )

    assert reverse.returncode == 0, reverse.stdout + reverse.stderr
    payload = _api_score(monkeypatch, db_path)
    assert float(payload["interpretation"]["signaling_likelihood"]) == pytest.approx(
        computed_likelihood
    )
    assert payload["score"] == pytest.approx(computed_score), (
        "the reversal restored the classification but left the score from the override"
    )
    assert payload["score"] == pytest.approx(_score_from_the_published_pair(payload))


def test_an_override_of_a_seller_with_no_classification_is_rejected(db_path: Path) -> None:
    result = _record(db_path, "--action", "override_classification", "--likelihood", "0.5")

    assert result.returncode == 1
    assert "no classification row" in result.stdout
    conn = _connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM corrections").fetchone()[0] == 0
    finally:
        conn.close()


def test_an_override_likelihood_outside_the_published_range_is_rejected(db_path: Path) -> None:
    result = _record(db_path, "--action", "override_classification", "--likelihood", "1.5")

    assert result.returncode == 1
    assert "signaling_likelihood" in result.stdout
    conn = _connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM corrections").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_script_has_no_update_or_delete_path() -> None:
    """The append-only claim is checked against the code, not trusted."""
    for path in (SCRIPT, REPO_ROOT / "src" / "repindex" / "corrections.py"):
        text = path.read_text(encoding="utf-8").upper()
        assert "UPDATE CORRECTIONS" not in text
        assert "DELETE FROM CORRECTIONS" not in text


def test_the_schema_rejects_an_unknown_decision(db_path: Path) -> None:
    conn = _connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO corrections (subject, claim, evidence, decision, reason, "
                "methodology_version, applied_by) VALUES ('0xa','c','e','maybe','r','v1','owner')"
            )
    finally:
        conn.close()


def test_the_schema_rejects_a_correction_without_reasoning(db_path: Path) -> None:
    """A decision with no stated reason cannot be reviewed later."""
    conn = _connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO corrections (subject, claim, evidence, decision, reason, "
                "methodology_version, applied_by) VALUES ('0xa','c','e','upheld','','v1','owner')"
            )
    finally:
        conn.close()
