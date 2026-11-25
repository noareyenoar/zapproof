"""Explainable rules that turn features into a signaling likelihood (prompt task A4).

The prompt requires that "every contribution must be visible in the output", so the
classifier is a weighted sum of named rules and each rule reports its own
contribution, whether it fired, and a sentence saying why. Nothing is hidden in a
model.

**What the number means, stated precisely.** `signaling_likelihood` is the degree to
which a seller's *payment record* resembles patterns that do not require independent
customers. It is not a probability that the seller is dishonest, and it is not a
finding about any entity. The project's language rules apply: a high value is
"signaling-like pattern", never "scam" or "fake".

**Weights and cutoffs are judgement, not measurement.** There is no ground truth to
fit them to (prompt task A4 says so). Every cutoff lives in `RuleThresholds` so the
sensitivity analysis required by task A6 can perturb them and show how much the
headline moves.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from typing import Any

from repindex.classify.features import SellerFeatures
from repindex.common.version import METHODOLOGY_VERSION


@dataclass(frozen=True, slots=True)
class RuleThresholds:
    """Every numeric cutoff the rules use, in one place so it can be perturbed."""

    min_settlements: int = 20
    thin_payer_max_payers: int = 3
    thin_payer_min_settlements: int = 20
    repeat_buyer_rate: float = 0.8
    repeat_buyer_min_payers: int = 3
    # Repeat custom is only a signal when the payer base is thin. With a hundred
    # payers, most of whom buy again, the record describes a successful business --
    # and the rubric says exactly that. Measured against the v2 adjudication: the five
    # false positives had a median of 106 distinct payers, and repeat_buyer_dominant
    # fired on all five, because the rule only asked whether repeaters were common,
    # never whether there were few payers to begin with.
    repeat_buyer_max_payers: int = 10
    fixed_amount_entropy: float = 0.15
    burstiness: float = 0.7
    peak_hour_share: float = 0.5
    min_observation_hours: float = 24.0
    # When distinct payers are at least this fraction of settlements, a fixed price is
    # a posted price and a burst is a campaign, not evidence of a closed loop.
    #
    # Measured against the blinded adjudication of 2026-10-28: 56 sellers were flagged
    # by the classifier and called independent by both adjudicators, with a median of
    # 26 distinct payers for 28 settlements. `fixed_amount` and `concentrated_in_time`
    # fired on nearly all of them. The rubric had already said payer diversity is strong
    # evidence of independent customers, so the classifier was contradicting its own
    # published method — a defect in the rules, not in the test.
    min_payer_diversity_for_fixed_price: float = 0.5
    relayer_concentration: float = 0.9
    # The rubric's step 1 says any reciprocal payer settles the case. The classifier
    # required half of all payers to be reciprocal, which let through ten sellers the
    # readers flagged: measured median reciprocity 0.083, and self_loop plus
    # fixed_amount together reached only 0.45, just under the cutoff.
    reciprocity_rate: float = 0.05
    reciprocity_min_payers: int = 3
    weight_self_loop: float = 0.45
    weight_thin_payer_base: float = 0.30
    weight_repeat_buyer_dominant: float = 0.20
    weight_fixed_amount: float = 0.20
    weight_bursty: float = 0.15
    weight_concentrated_in_time: float = 0.30
    weight_single_relayer: float = 0.10
    weight_reciprocity: float = 0.15

    def perturbed(self, factor: float) -> RuleThresholds:
        """Scale every field by `factor`, keeping ints integer where they are counts."""
        changes: dict[str, Any] = {}
        for name, value in asdict(self).items():
            if isinstance(value, int) and not isinstance(value, bool):
                changes[name] = max(1, round(value * factor))
            else:
                changes[name] = round(float(value) * factor, 6)
        return replace(self, **changes)


DEFAULT_THRESHOLDS = RuleThresholds()


def replace_field(thresholds: RuleThresholds, name: str, value: Any) -> RuleThresholds:
    """Return a copy with one field replaced. Used by the sensitivity analysis."""
    if name not in asdict(thresholds):
        raise KeyError(f"{name!r} is not a field of RuleThresholds")
    return replace(thresholds, **{name: value})


# Below this many settlements the features are too thin to say much.
MIN_SETTLEMENTS_FOR_CONFIDENT_FLAG = 5


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    rule: str
    fired: bool
    contribution: float
    detail: str


@dataclass(frozen=True, slots=True)
class Classification:
    seller: str
    signaling_likelihood: float
    contributions: list[RuleOutcome]
    methodology_version: str = METHODOLOGY_VERSION


def _self_loop(f: SellerFeatures, t: RuleThresholds) -> RuleOutcome:
    fired = f.self_funding_flag == 1
    return RuleOutcome(
        "self_loop",
        fired,
        t.weight_self_loop if fired else 0.0,
        "payer and seller are the same address in at least one settlement",
    )


def _thin_payer_base(f: SellerFeatures, t: RuleThresholds) -> RuleOutcome:
    fired = (
        f.settlement_count >= t.thin_payer_min_settlements
        and f.unique_payers <= t.thin_payer_max_payers
    )
    return RuleOutcome(
        "thin_payer_base",
        fired,
        t.weight_thin_payer_base if fired else 0.0,
        f"{f.settlement_count} settlements from only {f.unique_payers} distinct payers",
    )


def _repeat_buyer_dominant(f: SellerFeatures, t: RuleThresholds) -> RuleOutcome:
    if f.unique_payers > t.repeat_buyer_max_payers:
        return RuleOutcome(
            "repeat_buyer_dominant",
            False,
            0.0,
            f"not judged: {f.unique_payers} distinct payers is a customer base, and "
            "repeat custom inside a large base is a successful business",
        )
    fired = (
        f.unique_payers >= t.repeat_buyer_min_payers and f.repeat_buyer_rate >= t.repeat_buyer_rate
    )
    return RuleOutcome(
        "repeat_buyer_dominant",
        fired,
        t.weight_repeat_buyer_dominant if fired else 0.0,
        f"{f.repeat_buyer_rate:.0%} of payers bought more than once",
    )


def _payer_diversity(f: SellerFeatures) -> float:
    """Distinct payers as a fraction of settlements. Near 1.0 means almost every payer
    bought once, which is a customer list rather than a loop."""
    return (f.unique_payers / f.settlement_count) if f.settlement_count else 0.0


def _fixed_amount(f: SellerFeatures, t: RuleThresholds) -> RuleOutcome:
    diversity = _payer_diversity(f)
    if diversity >= t.min_payer_diversity_for_fixed_price:
        return RuleOutcome(
            "fixed_amount",
            False,
            0.0,
            f"not judged: {diversity:.0%} of settlements have a distinct payer, so a "
            "fixed amount is a posted price rather than a closed loop",
        )
    fired = f.settlement_count >= t.min_settlements and f.amount_entropy <= t.fixed_amount_entropy
    return RuleOutcome(
        "fixed_amount",
        fired,
        t.weight_fixed_amount if fired else 0.0,
        f"amount entropy {f.amount_entropy:.3f} (0 = every settlement identical)",
    )


def _bursty(f: SellerFeatures, t: RuleThresholds) -> RuleOutcome:
    fired = f.settlement_count >= t.min_settlements and f.burstiness >= t.burstiness
    return RuleOutcome(
        "bursty",
        fired,
        t.weight_bursty if fired else 0.0,
        f"burstiness {f.burstiness:.3f} (0 = regular, 1 = one burst)",
    )


def _concentrated_in_time(f: SellerFeatures, t: RuleThresholds) -> RuleOutcome:
    """Density, not irregularity: most settlements inside one hour.

    Added after the planted-pattern test showed the classifier missing a fan-in
    pattern. `burstiness` could not see it, because thirty settlements one second
    apart are *perfectly regular* — Goh-Barabasi measures irregularity, and a burst
    that runs on a metronome scores zero. Concentration measures the thing that
    actually distinguishes the pattern.
    """
    # The observation window must be long enough for "all in one hour" to mean
    # something. Without this guard, every seller in a short ingest looks maximally
    # concentrated: measured on a 500-block window (about seventeen minutes), 82.63%
    # of settlements belonged to flagged sellers and almost all of them fired here.
    # That is a property of the window, not of the sellers.
    diversity = _payer_diversity(f)
    if f.observation_window_hours < t.min_observation_hours:
        return RuleOutcome(
            "concentrated_in_time",
            False,
            0.0,
            f"not judged: the data covers only {f.observation_window_hours:.1f} hours, "
            "too short to tell concentration from a short window",
        )
    if diversity >= t.min_payer_diversity_for_fixed_price:
        return RuleOutcome(
            "concentrated_in_time",
            False,
            0.0,
            f"not judged: {diversity:.0%} of settlements have a distinct payer, so a "
            "burst is a campaign rather than a closed loop",
        )
    fired = f.settlement_count >= t.min_settlements and f.peak_hour_share >= t.peak_hour_share
    detail = f"{f.peak_hour_share:.0%} of settlements fall inside a single hour"
    return RuleOutcome(
        "concentrated_in_time",
        fired,
        t.weight_concentrated_in_time if fired else 0.0,
        detail,
    )


def _single_relayer(f: SellerFeatures, t: RuleThresholds) -> RuleOutcome:
    # Abstain when no relayer was resolved. The default backfill does not fetch the
    # transaction envelope, so this is the normal case rather than an error; treating
    # "unknown" as "not concentrated" would quietly bias every seller downwards.
    if f.relayer_concentration is None:
        return RuleOutcome(
            "single_relayer",
            False,
            0.0,
            "not judged: no facilitator relayer was resolved for this seller",
        )
    fired = (
        f.settlement_count >= t.min_settlements
        and f.relayer_concentration >= t.relayer_concentration
    )
    return RuleOutcome(
        "single_relayer",
        fired,
        t.weight_single_relayer if fired else 0.0,
        f"{f.relayer_concentration:.0%} of settlements carried by one relayer",
    )


def _reciprocity(f: SellerFeatures, t: RuleThresholds) -> RuleOutcome:
    fired = f.unique_payers >= t.reciprocity_min_payers and f.reciprocity_rate >= t.reciprocity_rate
    return RuleOutcome(
        "round_trip",
        fired,
        t.weight_reciprocity if fired else 0.0,
        f"{f.reciprocity_rate:.0%} of payers also received payment from this seller",
    )


# `fresh_payer_share` is computed but deliberately NOT used as a rule. From
# settlement data alone a first-time genuine customer and a purpose-created wallet
# look identical: both appear once, at the payment. The planted test confirmed it —
# the proxy flagged both synthetic genuine sellers. A rule that cannot tell those
# apart is worse than no rule, so the feature is reported as unreliable instead.
# Measuring "payer account age and prior activity" honestly needs funding traces,
# which are not implemented.

RULES = (
    _self_loop,
    _thin_payer_base,
    _repeat_buyer_dominant,
    _fixed_amount,
    _bursty,
    _concentrated_in_time,
    _single_relayer,
    _reciprocity,
)

# The names the rules report in `RuleOutcome.rule`. Callers that separate corrections
# from classifier output (the MCP tool, the public analysis) need this set; deriving it
# from `RULES` is not possible because the function names do not all match the reported
# names (`_reciprocity` reports `round_trip`).
RULE_NAMES = frozenset(
    {
        "self_loop",
        "thin_payer_base",
        "repeat_buyer_dominant",
        "fixed_amount",
        "bursty",
        "concentrated_in_time",
        "single_relayer",
        "round_trip",
    }
)


def classify_seller(
    features: SellerFeatures,
    thresholds: RuleThresholds = DEFAULT_THRESHOLDS,
) -> Classification:
    """Apply every rule and sum the contributions, clamped to [0, 1].

    The clamp matters: weights total 1.85, so several rules firing at once would
    otherwise produce a number above 1. Clamping is a statement that anything above
    1 is "as signaling-like as this method can express", not a measurement.
    """
    outcomes = [rule(features, thresholds) for rule in RULES]
    total = sum(outcome.contribution for outcome in outcomes)
    return Classification(
        seller=features.seller,
        signaling_likelihood=round(max(0.0, min(1.0, total)), 6),
        contributions=outcomes,
    )


def classify_all(
    features: list[SellerFeatures],
    thresholds: RuleThresholds = DEFAULT_THRESHOLDS,
) -> list[Classification]:
    return [classify_seller(f, thresholds) for f in features]


def classify_one(
    conn: sqlite3.Connection,
    seller: str,
    thresholds: RuleThresholds = DEFAULT_THRESHOLDS,
) -> Classification | None:
    """Classify one seller from the stored features, or None when there are none.

    Used when a correction changes a single seller and the whole derived set is not
    being rebuilt (an override, or the reversal of one). It reads the same feature row
    a rebuild would, so the result is what a rebuild would produce from the current
    evidence -- not whatever the classification stored at the time of the dispute.
    """
    row = conn.execute(
        "SELECT seller, unique_payers, repeat_buyer_rate, reciprocity_rate, "
        "amount_entropy, burstiness, peak_hour_share, fresh_payer_share, "
        "self_funding_flag, relayer_concentration, settlement_count, active_days, "
        "observation_window_hours, methodology_version FROM features WHERE seller = ?",
        (seller,),
    ).fetchone()
    if row is None:
        return None
    (
        seller,
        unique_payers,
        repeat_buyer_rate,
        reciprocity_rate,
        amount_entropy,
        burstiness,
        peak_hour_share,
        fresh_payer_share,
        self_funding_flag,
        relayer_concentration,
        settlement_count,
        active_days,
        observation_window_hours,
        methodology_version,
    ) = row
    return classify_seller(
        SellerFeatures(
            seller=seller,
            settlement_count=settlement_count,
            unique_payers=unique_payers,
            repeat_buyer_rate=repeat_buyer_rate,
            reciprocity_rate=reciprocity_rate,
            amount_entropy=amount_entropy,
            burstiness=burstiness,
            peak_hour_share=peak_hour_share,
            fresh_payer_share=fresh_payer_share,
            self_funding_flag=self_funding_flag,
            relayer_concentration=relayer_concentration,
            active_days=active_days,
            observation_window_hours=observation_window_hours,
            methodology_version=methodology_version,
        ),
        thresholds,
    )


def write_classifications(conn: sqlite3.Connection, results: list[Classification]) -> int:
    try:
        conn.execute("DELETE FROM classifications")
        conn.executemany(
            "INSERT INTO classifications (seller, signaling_likelihood, contributions, "
            "methodology_version) VALUES (?,?,?,?)",
            [
                (
                    c.seller,
                    c.signaling_likelihood,
                    json.dumps([asdict(o) for o in c.contributions]),
                    c.methodology_version,
                )
                for c in results
            ],
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    return len(results)


def flag_settlements(conn: sqlite3.Connection) -> int:
    """Per-settlement pattern flags, as prompt task A4 requires alongside the score.

    These are observations about a single payment, not verdicts: `self_loop` says the
    payer and seller are the same address in that row, which is a fact about the row.
    """
    try:
        conn.execute("DELETE FROM settlement_flags")

        conn.execute(
            "INSERT INTO settlement_flags (tx_hash, log_index, flag, detail) "
            "SELECT tx_hash, log_index, 'self_loop', 'payer equals seller' "
            "FROM settlements WHERE payer = seller"
        )

        # A settlement is part of a round trip when the seller has also paid the payer
        # at some point. Marked on both directions so either row is findable.
        conn.execute(
            "INSERT OR IGNORE INTO settlement_flags (tx_hash, log_index, flag, detail) "
            "SELECT s1.tx_hash, s1.log_index, 'round_trip', "
            "       'the seller also paid this payer in another settlement' "
            "FROM settlements s1 "
            "WHERE EXISTS (SELECT 1 FROM settlements s2 "
            "              WHERE s2.seller = s1.payer AND s2.payer = s1.seller)"
        )
        written = int(conn.execute("SELECT COUNT(*) FROM settlement_flags").fetchone()[0])
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    return written
