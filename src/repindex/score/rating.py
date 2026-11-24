"""Rating v0 (prompt Section 7, task A5).

The output is deliberately narrow. A score exists only when there is enough evidence
to produce one; otherwise the answer is the literal string `insufficient_evidence`
and no number at all. Prompt task A5 is explicit that unknown is never zero, and gate
check GA.11 tests it.

What the score is
-----------------
A number from 0 to 100 expressing **how much settled payment evidence a seller has,
discounted by how signaling-like that evidence looks**. It is not a probability the
seller is honest. A seller nobody has paid has no score, not a bad one.

Determinism
-----------
Nothing here reads the clock or uses randomness. Every input is a column in the
database, so the same database state produces byte-identical output. Gate check GA.5
rebuilds twice and compares hashes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

from repindex.common.version import METHODOLOGY_VERSION

# Fallbacks only. The real values are human-owned in config/thresholds.yaml
# (prompt rule 3.4.4), and load_thresholds() below prefers them.
DEFAULT_MIN_SETTLEMENTS = 20
DEFAULT_MIN_COUNTERPARTIES = 5
DEFAULT_MIN_ACTIVE_DAYS = 3


@dataclass(frozen=True, slots=True)
class Score:
    identifier: str
    status: str  # 'scored' | 'insufficient_evidence'
    score: float | None
    confidence_band: str  # 'low' | 'medium' | 'high'
    evidence_settlements: int
    evidence_counterparties: int
    evidence_active_days: int
    data_window_start: int
    data_window_end: int
    as_of_block: int
    methodology_version: str = METHODOLOGY_VERSION

    def as_row(self) -> tuple[Any, ...]:
        values = asdict(self)
        return tuple(values.values())


@dataclass(frozen=True, slots=True)
class EvidenceMinimum:
    settlements: int = DEFAULT_MIN_SETTLEMENTS
    unique_counterparties: int = DEFAULT_MIN_COUNTERPARTIES
    active_days: int = DEFAULT_MIN_ACTIVE_DAYS

    @classmethod
    def from_thresholds(cls, thresholds: dict[str, Any]) -> EvidenceMinimum:
        block = (
            thresholds.get("phaseA", {}).get("min_evidence_for_point_score", {})
            if thresholds
            else {}
        )
        return cls(
            settlements=int(block.get("settlements", DEFAULT_MIN_SETTLEMENTS)),
            unique_counterparties=int(
                block.get("unique_counterparties", DEFAULT_MIN_COUNTERPARTIES)
            ),
            active_days=int(block.get("active_days", DEFAULT_MIN_ACTIVE_DAYS)),
        )


def evidence_strength(
    settlements: int, counterparties: int, active_days: int, minimum: EvidenceMinimum
) -> float:
    """How far past the minimum the evidence goes, in [0, 1].

    Each dimension is capped at 4x the minimum, so a seller with ten thousand
    settlements does not simply max out the score — volume alone should not buy
    reputation.
    """

    def ratio(value: int, floor: int) -> float:
        if floor <= 0:
            return 1.0
        return min(1.0, (value / floor) / 4.0)

    parts = [
        ratio(settlements, minimum.settlements),
        ratio(counterparties, minimum.unique_counterparties),
        ratio(active_days, minimum.active_days),
    ]
    return sum(parts) / len(parts)


def confidence_band(
    settlements: int, counterparties: int, active_days: int, minimum: EvidenceMinimum
) -> str:
    """Coarse band derived from how many times over the minimum the evidence is."""

    def multiple(value: int, floor: int) -> float:
        return (value / floor) if floor > 0 else 1.0

    weakest = min(
        multiple(settlements, minimum.settlements),
        multiple(counterparties, minimum.unique_counterparties),
        multiple(active_days, minimum.active_days),
    )
    if weakest >= 4:
        return "high"
    if weakest >= 2:
        return "medium"
    return "low"


def score_seller(
    *,
    seller: str,
    settlements: int,
    counterparties: int,
    active_days: int,
    signaling_likelihood: float,
    window_start: int,
    window_end: int,
    as_of_block: int,
    minimum: EvidenceMinimum,
    methodology_version: str = METHODOLOGY_VERSION,
) -> Score:
    """Produce a score, or refuse to.

    The refusal is the important branch. Below the evidence minimum there is no
    number at all — not zero, not a placeholder — because zero would read as a
    judgement and there is nothing to judge.
    """
    below = (
        settlements < minimum.settlements
        or counterparties < minimum.unique_counterparties
        or active_days < minimum.active_days
    )
    band = confidence_band(settlements, counterparties, active_days, minimum)

    if below:
        return Score(
            identifier=seller,
            status="insufficient_evidence",
            score=None,
            confidence_band=band,
            evidence_settlements=settlements,
            evidence_counterparties=counterparties,
            evidence_active_days=active_days,
            data_window_start=window_start,
            data_window_end=window_end,
            as_of_block=as_of_block,
            methodology_version=methodology_version,
        )

    strength = evidence_strength(settlements, counterparties, active_days, minimum)
    genuine = max(0.0, min(1.0, 1.0 - signaling_likelihood))
    score = round(100.0 * strength * genuine, 2)

    return Score(
        identifier=seller,
        status="scored",
        score=score,
        confidence_band=band,
        evidence_settlements=settlements,
        evidence_counterparties=counterparties,
        evidence_active_days=active_days,
        data_window_start=window_start,
        data_window_end=window_end,
        as_of_block=as_of_block,
        methodology_version=methodology_version,
    )


def compute_scores(
    conn: sqlite3.Connection,
    minimum: EvidenceMinimum,
    *,
    methodology_version: str = METHODOLOGY_VERSION,
) -> list[Score]:
    """Score every seller that has features, using stored features and classifications."""
    window = conn.execute(
        "SELECT MIN(block_time) AS lo, MAX(block_time) AS hi, MAX(block_number) AS head "
        "FROM settlements"
    ).fetchone()
    window_start = int(window["lo"] or 0)
    window_end = int(window["hi"] or 0)
    as_of_block = int(window["head"] or 0)

    # F7: a seller with no classification must not be scored as if its likelihood
    # were 0.0 — the most benign value the field can take. An inner join makes the
    # missing row visible instead of silently treating absence as innocence, and the
    # count check turns it into an error the caller cannot miss.
    rows = conn.execute(
        "SELECT f.seller, f.settlement_count, f.unique_payers, f.active_days, "
        "       c.signaling_likelihood AS likelihood "
        "FROM features f JOIN classifications c ON c.seller = f.seller"
    ).fetchall()
    feature_count = int(conn.execute("SELECT COUNT(*) FROM features").fetchone()[0])
    if len(rows) != feature_count:
        raise ValueError(
            f"{feature_count - len(rows)} seller(s) have features but no classification; "
            "refusing to score an unclassified seller"
        )

    results = [
        score_seller(
            seller=row["seller"],
            settlements=int(row["settlement_count"]),
            counterparties=int(row["unique_payers"]),
            active_days=int(row["active_days"]),
            signaling_likelihood=float(row["likelihood"]),
            window_start=window_start,
            window_end=window_end,
            as_of_block=as_of_block,
            minimum=minimum,
            methodology_version=methodology_version,
        )
        for row in rows
    ]
    results.sort(key=lambda s: s.identifier)
    return results


def score_one(
    conn: sqlite3.Connection,
    seller: str,
    minimum: EvidenceMinimum,
    *,
    methodology_version: str = METHODOLOGY_VERSION,
) -> Score | None:
    """Recompute one seller's score from its stored features and classification.

    The third independent review (H1) found that an upheld classification override
    changed `classifications` immediately while `scores` kept the value computed from
    the rules' likelihood, so the API could publish a likelihood and a score that
    cannot both be true under the documented formula until someone ran a rebuild.
    `corrections` calls this in the same transaction as the override or its reversal.

    Returns None when the seller has no feature or classification row: no evidence
    means no score, and a caller must not invent one.
    """
    row = conn.execute(
        "SELECT f.settlement_count, f.unique_payers, f.active_days, "
        "       c.signaling_likelihood AS likelihood "
        "FROM features f JOIN classifications c ON c.seller = f.seller "
        "WHERE f.seller = ?",
        (seller,),
    ).fetchone()
    if row is None:
        return None
    window = conn.execute(
        "SELECT MIN(block_time) AS lo, MAX(block_time) AS hi, MAX(block_number) AS head "
        "FROM settlements"
    ).fetchone()
    return score_seller(
        seller=seller,
        settlements=int(row["settlement_count"]),
        counterparties=int(row["unique_payers"]),
        active_days=int(row["active_days"]),
        signaling_likelihood=float(row["likelihood"]),
        window_start=int(window["lo"] or 0),
        window_end=int(window["hi"] or 0),
        as_of_block=int(window["head"] or 0),
        minimum=minimum,
        methodology_version=methodology_version,
    )


def write_score(conn: sqlite3.Connection, score: Score) -> None:
    """Replace one seller's score row. The caller owns the transaction."""
    conn.execute("DELETE FROM scores WHERE identifier = ?", (score.identifier,))
    conn.execute(
        "INSERT INTO scores (identifier, status, score, confidence_band, "
        "evidence_settlements, evidence_counterparties, evidence_active_days, "
        "data_window_start, data_window_end, as_of_block, methodology_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        score.as_row(),
    )


def write_scores(conn: sqlite3.Connection, scores: list[Score]) -> int:
    try:
        conn.execute("DELETE FROM scores")
        conn.executemany(
            "INSERT INTO scores (identifier, status, score, confidence_band, "
            "evidence_settlements, evidence_counterparties, evidence_active_days, "
            "data_window_start, data_window_end, as_of_block, methodology_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    s.identifier,
                    s.status,
                    s.score,
                    s.confidence_band,
                    s.evidence_settlements,
                    s.evidence_counterparties,
                    s.evidence_active_days,
                    s.data_window_start,
                    s.data_window_end,
                    s.as_of_block,
                    s.methodology_version,
                )
                for s in scores
            ],
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    return len(scores)
