"""Tests for the Phase A classifier (prompt task A4).

The important properties are not accuracy — there is no ground truth to measure
accuracy against. They are: the arithmetic is right, a genuine-looking seller scores
low, a signaling-looking seller scores high, and every contribution is visible.
"""

from __future__ import annotations

import itertools
import sqlite3
from pathlib import Path

import pytest

from repindex.classify import features as feat
from repindex.classify import rules
from repindex.ingest import schema


def make_features(**overrides: object) -> feat.SellerFeatures:
    base: dict[str, object] = {
        "seller": "0xseller",
        "settlement_count": 100,
        "unique_payers": 40,
        "repeat_buyer_rate": 0.2,
        "reciprocity_rate": 0.0,
        "amount_entropy": 0.8,
        "burstiness": 0.2,
        "peak_hour_share": 0.05,
        "fresh_payer_share": 0.1,
        "self_funding_flag": 0,
        "relayer_concentration": 0.3,
        "active_days": 20,
    }
    base.update(overrides)
    return feat.SellerFeatures(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# the arithmetic
# --------------------------------------------------------------------------- #


def test_entropy_is_zero_for_a_single_distinct_amount() -> None:
    assert feat.shannon_entropy_normalised({2000: 500}) == 0.0


def test_entropy_is_one_when_every_amount_differs() -> None:
    assert feat.shannon_entropy_normalised({i: 1 for i in range(10)}) == pytest.approx(1.0)


def test_entropy_orders_repetition_below_variety() -> None:
    repetitive = feat.shannon_entropy_normalised({2000: 9, 5000: 1})
    varied = feat.shannon_entropy_normalised({2000: 5, 5000: 3, 9000: 2})
    assert repetitive < varied


def test_burstiness_needs_at_least_three_events() -> None:
    assert feat.burstiness([1, 2]) == 0.0


def test_burstiness_is_higher_for_a_burst_than_for_a_regular_seller() -> None:
    regular = feat.burstiness(list(range(0, 1000, 100)))
    burst = feat.burstiness([0, 1, 2, 3, 4, 100_000])
    assert burst > regular


def test_burstiness_stays_in_range() -> None:
    assert 0.0 <= feat.burstiness([0, 0, 0, 0]) <= 1.0


# --------------------------------------------------------------------------- #
# the rules
# --------------------------------------------------------------------------- #


def test_a_genuine_looking_seller_scores_low() -> None:
    """Many distinct payers, varied amounts, no round trips, no self loops."""
    result = rules.classify_seller(make_features())
    assert result.signaling_likelihood == 0.0
    assert not any(outcome.fired for outcome in result.contributions)


def test_a_self_looping_seller_scores_high() -> None:
    result = rules.classify_seller(
        make_features(
            self_funding_flag=1,
            unique_payers=2,
            repeat_buyer_rate=1.0,
            amount_entropy=0.0,
            burstiness=0.9,
            fresh_payer_share=1.0,
            relayer_concentration=1.0,
            reciprocity_rate=1.0,
        )
    )
    assert result.signaling_likelihood == pytest.approx(1.0)
    fired = {o.rule for o in result.contributions if o.fired}
    assert {"self_loop", "thin_payer_base", "fixed_amount"}.issubset(fired)


def test_every_contribution_is_visible_with_a_reason() -> None:
    result = rules.classify_seller(make_features(self_funding_flag=1))
    assert len(result.contributions) == len(rules.RULES)
    for outcome in result.contributions:
        assert outcome.detail, f"{outcome.rule} has no explanation"


def test_the_sum_of_contributions_explains_the_score() -> None:
    result = rules.classify_seller(make_features(self_funding_flag=1, amount_entropy=0.0))
    total = sum(o.contribution for o in result.contributions)
    assert result.signaling_likelihood == pytest.approx(min(1.0, total))


def test_the_score_cannot_exceed_one() -> None:
    """Weights total 1.75, so several rules firing must clamp rather than overshoot."""
    result = rules.classify_seller(
        make_features(
            self_funding_flag=1,
            unique_payers=1,
            repeat_buyer_rate=1.0,
            amount_entropy=0.0,
            burstiness=1.0,
            fresh_payer_share=1.0,
            relayer_concentration=1.0,
            reciprocity_rate=1.0,
            settlement_count=100,
        )
    )
    assert result.signaling_likelihood <= 1.0


def test_thin_payer_base_needs_enough_settlements_to_mean_anything() -> None:
    """Three payers across four settlements is not evidence of anything."""
    small = rules.classify_seller(make_features(settlement_count=4, unique_payers=1))
    assert "thin_payer_base" not in {o.rule for o in small.contributions if o.fired}


def test_classification_is_deterministic() -> None:
    a = rules.classify_seller(make_features(self_funding_flag=1))
    b = rules.classify_seller(make_features(self_funding_flag=1))
    assert a == b


# --------------------------------------------------------------------------- #
# features from a database
# --------------------------------------------------------------------------- #


_SEQ = itertools.count(1)


def add_settlement(
    db: sqlite3.Connection, payer: str, seller: str, relayer: str, amount: int, t: int
) -> None:
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
            amount,
            f"{amount / 1e6:.6f}",
            payer,
            seller,
            relayer,
            f"0xnonce{unique}",
            "eip3009_authorization_used",
        ),
    )


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "classify.db"
    schema.initialize(path)
    return schema.connect(path)


def test_features_distinguish_variety_from_repetition(db: sqlite3.Connection) -> None:
    # Genuine: 30 distinct payers, distinct amounts.
    for i in range(30):
        add_settlement(
            db, f"0xgenuine{i:02d}", "0xgood", "0xr1", 1000 + i * 37, 1_700_000_000 + i * 4000
        )
    # Repetitive: two payers, one amount.
    for i in range(30):
        add_settlement(db, f"0xloop{i % 2}", "0xodd", "0xr2", 2000, 1_700_000_000 + i * 60)

    computed = {f.seller: f for f in feat.compute_features(db)}

    assert computed["0xgood"].unique_payers == 30
    assert computed["0xgood"].amount_entropy > computed["0xodd"].amount_entropy
    assert computed["0xodd"].unique_payers == 2
    assert computed["0xodd"].repeat_buyer_rate == 1.0


def test_self_loop_is_visible_in_features(db: sqlite3.Connection) -> None:
    for i in range(10):
        add_settlement(db, "0xsame", "0xsame", "0xr", 2000, 1_700_000_000 + i)
    computed = {f.seller: f for f in feat.compute_features(db)}
    assert computed["0xsame"].self_funding_flag == 1


def test_features_are_written_and_replaced_on_rebuild(db: sqlite3.Connection) -> None:
    for i in range(12):
        add_settlement(db, f"0xp{i:02d}", "0xs", "0xr", 2000 + i, 1_700_000_000 + i * 600)
    first = feat.compute_features(db)
    feat.write_features(db, first)
    second = feat.compute_features(db)
    feat.write_features(db, second)

    rows = db.execute("SELECT COUNT(*) FROM features").fetchone()[0]
    assert rows == 1, "a rebuild must replace, not accumulate"


def test_settlement_flags_mark_self_loops_and_round_trips(db: sqlite3.Connection) -> None:
    add_settlement(db, "0xsame", "0xsame", "0xr", 2000, 1_700_000_000)
    add_settlement(db, "0xa", "0xb", "0xr", 2000, 1_700_000_001)
    add_settlement(db, "0xb", "0xa", "0xr", 2000, 1_700_000_002)
    db.commit()

    rules.flag_settlements(db)

    flags = {row["flag"] for row in db.execute("SELECT flag FROM settlement_flags")}
    assert "self_loop" in flags
    assert "round_trip" in flags
