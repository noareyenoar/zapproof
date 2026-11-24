"""Prompt task A5 / gate check GA.11.

The claim under test is narrow and absolute: **when evidence is below the minimum,
the answer is the literal `insufficient_evidence` and there is no number at all.**
Not zero, not a placeholder, not a low score. Zero would read as a judgement, and
there is nothing to judge.

These tests are the reason GA.11 can pass: it runs this file and nothing else.
"""

from __future__ import annotations

import itertools
import sqlite3
from pathlib import Path

import pytest

from repindex.ingest import schema
from repindex.score import rating

MINIMUM = rating.EvidenceMinimum(settlements=20, unique_counterparties=5, active_days=3)


def test_below_minimum_returns_the_literal_with_no_number() -> None:
    result = rating.score_seller(
        seller="0xunknown",
        settlements=3,
        counterparties=1,
        active_days=1,
        signaling_likelihood=0.0,
        window_start=0,
        window_end=100,
        as_of_block=100,
        minimum=MINIMUM,
    )
    assert result.status == "insufficient_evidence"
    assert result.score is None
    assert result.score != 0, "an unknown must never be rendered as zero"
    assert result.status != "scored"


def test_a_seller_with_no_settlements_is_not_scored_zero() -> None:
    result = rating.score_seller(
        seller="0xnever-seen",
        settlements=0,
        counterparties=0,
        active_days=0,
        signaling_likelihood=0.0,
        window_start=0,
        window_end=100,
        as_of_block=100,
        minimum=MINIMUM,
    )
    assert result.status == "insufficient_evidence"
    assert result.score is None


@pytest.mark.parametrize(
    ("settlements", "counterparties", "days"),
    [(19, 50, 30), (500, 4, 30), (500, 50, 2)],
)
def test_each_dimension_alone_can_keep_a_seller_below_the_minimum(
    settlements: int, counterparties: int, days: int
) -> None:
    """Failing any single dimension is enough; volume elsewhere does not compensate."""
    result = rating.score_seller(
        seller="0xpartial",
        settlements=settlements,
        counterparties=counterparties,
        active_days=days,
        signaling_likelihood=0.0,
        window_start=0,
        window_end=100,
        as_of_block=100,
        minimum=MINIMUM,
    )
    assert result.status == "insufficient_evidence"
    assert result.score is None


def test_at_the_minimum_a_score_appears() -> None:
    result = rating.score_seller(
        seller="0xknown",
        settlements=20,
        counterparties=5,
        active_days=3,
        signaling_likelihood=0.0,
        window_start=0,
        window_end=100,
        as_of_block=100,
        minimum=MINIMUM,
    )
    assert result.status == "scored"
    assert result.score is not None
    assert 0.0 <= result.score <= 100.0


def test_the_database_rejects_insufficient_evidence_with_a_number() -> None:
    """The guarantee is enforced one layer below the API, not only in Python."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ga11.db"
        schema.initialize(path)
        conn = schema.connect(path)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO scores (identifier, status, score, confidence_band, "
                "evidence_settlements, evidence_counterparties, evidence_active_days, "
                "data_window_start, data_window_end, as_of_block, methodology_version) "
                "VALUES ('0xa','insufficient_evidence',0.0,'low',0,0,0,0,0,0,'v0')"
            )
        conn.close()


def test_the_database_rejects_a_scored_row_with_no_number() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ga11.db"
        schema.initialize(path)
        conn = schema.connect(path)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO scores (identifier, status, score, confidence_band, "
                "evidence_settlements, evidence_counterparties, evidence_active_days, "
                "data_window_start, data_window_end, as_of_block, methodology_version) "
                "VALUES ('0xa','scored',NULL,'low',20,5,3,0,0,0,'v0')"
            )
        conn.close()


_SEQ = itertools.count(1)


def _add(db: sqlite3.Connection, seller: str, payer: str, t: int) -> None:
    unique = next(_SEQ)
    db.execute(
        "INSERT INTO settlements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"0x{unique:064x}",
            0,
            1000 + unique,
            f"0xblock{unique}",
            t,
            "0xtoken",
            6,
            2000,
            "0.002000",
            payer,
            seller,
            "0xrelayer",
            f"0xnonce{unique}",
            "eip3009_authorization_used",
        ),
    )


def test_end_to_end_a_thin_seller_gets_no_number(tmp_path: Path) -> None:
    """compute_scores must not invent a score for a seller with two payments."""
    path = tmp_path / "thin.db"
    schema.initialize(path)
    db = schema.connect(path)
    _add(db, "0xthin", "0xpayer1", 1_700_000_000)
    _add(db, "0xthin", "0xpayer2", 1_700_000_100)
    db.commit()

    from repindex.classify import features as feat
    from repindex.classify import rules

    computed = feat.compute_features(db)
    feat.write_features(db, computed)
    rules.write_classifications(db, rules.classify_all(computed))
    scores = rating.compute_scores(db, MINIMUM)
    rating.write_scores(db, scores)

    row = db.execute("SELECT status, score FROM scores WHERE identifier='0xthin'").fetchone()
    assert row["status"] == "insufficient_evidence"
    assert row["score"] is None
    db.close()


def test_a_seller_without_a_classification_is_refused_not_scored_as_clean(
    tmp_path: Path,
) -> None:
    """F7: the old LEFT JOIN + COALESCE(..., 0.0) scored an unclassified seller as if
    its signaling likelihood were zero — absence read as the most benign value."""
    path = tmp_path / "unclassified.db"
    schema.initialize(path)
    db = schema.connect(path)
    _add(db, "0xthin", "0xpayer1", 1_700_000_000)
    _add(db, "0xthin", "0xpayer2", 1_700_000_100)
    db.commit()

    from repindex.classify import features as feat

    feat.write_features(db, feat.compute_features(db))

    with pytest.raises(ValueError, match="no classification"):
        rating.compute_scores(db, MINIMUM)
    db.close()
