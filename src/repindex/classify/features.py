"""Per-seller features for the Phase A classifier (prompt task A4).

Rules and features only — no machine learning. Prompt task A4 says explicitly that
there is no ground truth to train on, and inventing one would make the classifier
unfalsifiable. Everything here is arithmetic over observed settlements.

**Where a literal reading of the prompt is unavailable, the proxy is named.**

| Prompt feature | What is actually computed here |
|---|---|
| unique payers per seller | exact |
| repeat-buyer rate | exact |
| payer-seller reciprocity | exact: a payer who also sold to this seller |
| amount repetition and entropy | Shannon entropy of the amount distribution |
| temporal burstiness | Goh-Barabasi burstiness of inter-arrival times |
| payer account age | **proxy**: first appearance in our own data |
| self-funding | **proxy**: payer is the seller, or shares a cluster |
| single-facilitator concentration | busiest relayer's share of the seller's settlements |

The last three are weak. An EOA has no on-chain age, so "age" means "first time we
saw it", which is bounded by our own data window. Funding traces are not implemented,
so "self-funding" only covers the self-loop and cluster cases. A reader is entitled
to discount both.

The two proxies are the weakest part of this module and are marked as such wherever
they appear in output, because a reader is entitled to discount them.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any

from repindex.common.version import METHODOLOGY_VERSION

SECONDS_PER_DAY = 86_400

# A payer counts as "fresh" when its first appearance in our data is within this
# window before it first paid the seller. One day is deliberately short: the point
# is to catch wallets that were funded and used almost immediately, not wallets that
# are simply new to our seven-day view.
FRESH_PAYER_WINDOW_SECONDS = SECONDS_PER_DAY


@dataclass(frozen=True, slots=True)
class SellerFeatures:
    seller: str
    settlement_count: int
    unique_payers: int
    repeat_buyer_rate: float
    reciprocity_rate: float
    amount_entropy: float
    burstiness: float
    peak_hour_share: float
    fresh_payer_share: float
    self_funding_flag: int
    # None when the transaction envelope was not fetched and no relayer is known.
    # The rule that uses it abstains rather than treating absence as zero.
    relayer_concentration: float | None
    active_days: int
    observation_window_hours: float = 0.0
    methodology_version: str = METHODOLOGY_VERSION

    def as_row(self) -> tuple[Any, ...]:
        return tuple(asdict(self).values())


def shannon_entropy_normalised(counts: dict[int, int]) -> float:
    """Entropy of a value distribution, scaled to [0, 1] by log2(number of values).

    Returns 0.0 when there is only one distinct value — every settlement at the same
    amount. Returns 1.0 when every settlement has a different amount. That ordering
    is the useful part: repetition scores low, variety scores high.
    """
    total = sum(counts.values())
    if total == 0 or len(counts) <= 1:
        return 0.0
    entropy = 0.0
    for count in counts.values():
        p = count / total
        entropy -= p * math.log2(p)
    ceiling = math.log2(len(counts))
    return entropy / ceiling if ceiling else 0.0


def peak_hour_share(timestamps: list[int]) -> float:
    """Largest share of a seller's settlements falling inside any one hour.

    This exists because Goh-Barabasi burstiness turned out to be blind to the pattern
    it needed to catch. Measured on the planted fixtures: thirty settlements one
    second apart are *perfectly regular*, so burstiness returns 0 even though the
    whole sequence occupies thirty seconds. Burstiness measures irregularity;
    concentration measures density. A fan-in attack is dense and regular.

    Returns 0.0 for fewer than three settlements, where an hour holds too little to
    mean anything.
    """
    if len(timestamps) < 3:
        return 0.0
    ordered = sorted(timestamps)
    total = len(ordered)
    best = 0
    start = 0
    for end in range(total):
        while ordered[end] - ordered[start] >= 3600:
            start += 1
        best = max(best, end - start + 1)
    return best / total


def burstiness(timestamps: list[int]) -> float:
    """Goh-Barabasi burstiness of the gaps between events, mapped to [0, 1).

    `B = (sigma - mu) / (sigma + mu)` lies in [-1, 1]: -1 for perfectly regular
    events, 0 for Poisson, +1 for a single burst. Only the positive half is useful
    here, so the result is clamped to [0, 1). A seller with fewer than three events
    has too few gaps to say anything and scores 0.0.
    """
    if len(timestamps) < 3:
        return 0.0
    ordered = sorted(timestamps)
    gaps = [ordered[i + 1] - ordered[i] for i in range(len(ordered) - 1)]
    mean = sum(gaps) / len(gaps)
    if mean <= 0:
        return 1.0  # every event at the same second
    variance = sum((g - mean) ** 2 for g in gaps) / len(gaps)
    sigma = math.sqrt(variance)
    b = (sigma - mean) / (sigma + mean) if (sigma + mean) else 0.0
    return max(0.0, min(0.999, b))


def compute_features(
    conn: sqlite3.Connection,
    *,
    methodology_version: str = METHODOLOGY_VERSION,
) -> list[SellerFeatures]:
    """Compute every seller's features in a handful of passes, not per-seller queries."""
    rows = conn.execute(
        """
        SELECT seller,
               COUNT(*) AS n,
               COUNT(DISTINCT payer) AS payers,
               COUNT(DISTINCT block_time / 86400) AS active_days
        FROM settlements GROUP BY seller
        """
    ).fetchall()
    if not rows:
        return []

    # How much time the data actually covers. Without this, a seller observed for
    # seventeen minutes looks maximally concentrated simply because every observation
    # falls inside one hour. Measured on a 500-block ingest: 82.63% of settlements
    # belonged to sellers flagged at 0.5, almost all of them on the concentration rule.
    window = conn.execute(
        "SELECT MIN(block_time) AS lo, MAX(block_time) AS hi FROM settlements"
    ).fetchone()
    window_hours = max(0.0, (int(window["hi"] or 0) - int(window["lo"] or 0)) / 3600.0)

    repeat_buyers: dict[str, int] = {}
    for row in conn.execute(
        "SELECT seller, COUNT(*) AS repeat_payers FROM ("
        "  SELECT seller, payer FROM settlements GROUP BY seller, payer HAVING COUNT(*) > 1"
        ") GROUP BY seller"
    ):
        repeat_buyers[row["seller"]] = int(row["repeat_payers"])

    reciprocal: dict[str, int] = {}
    for row in conn.execute(
        """
        SELECT s1.seller AS seller, COUNT(DISTINCT s1.payer) AS reciprocal
        FROM settlements s1
        WHERE EXISTS (
            SELECT 1 FROM settlements s2
            WHERE s2.seller = s1.payer AND s2.payer = s1.seller
        )
        GROUP BY s1.seller
        """
    ):
        reciprocal[row["seller"]] = int(row["reciprocal"])

    amounts: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for row in conn.execute(
        "SELECT seller, raw_amount, COUNT(*) AS c FROM settlements GROUP BY seller, raw_amount"
    ):
        amounts[row["seller"]][int(row["raw_amount"])] = int(row["c"])

    # Relayers are only present when the transaction envelope was fetched, which the
    # default backfill does not do. A NULL relayer must leave the map empty so the
    # rule abstains, rather than counting NULL as one relayer carrying everything.
    relayers: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in conn.execute(
        "SELECT seller, facilitator_relayer, COUNT(*) AS c "
        "FROM settlements WHERE facilitator_relayer IS NOT NULL "
        "GROUP BY seller, facilitator_relayer"
    ):
        relayers[row["seller"]][row["facilitator_relayer"]] = int(row["c"])

    times: dict[str, list[int]] = defaultdict(list)
    for row in conn.execute("SELECT seller, block_time FROM settlements"):
        times[row["seller"]].append(int(row["block_time"]))

    # Payer first appearance, and first payment to each seller, for the freshness
    # proxy. Two passes rather than a correlated subquery per row.
    payer_first: dict[str, int] = {
        row["payer"]: int(row["first_time"])
        for row in conn.execute(
            "SELECT payer, MIN(block_time) AS first_time FROM settlements GROUP BY payer"
        )
    }
    payer_seller_first: dict[tuple[str, str], int] = {}
    for row in conn.execute(
        "SELECT seller, payer, MIN(block_time) AS first_time "
        "FROM settlements GROUP BY seller, payer"
    ):
        payer_seller_first[(row["seller"], row["payer"])] = int(row["first_time"])

    # Self-funding means the seller literally paid itself. That is a fact about the
    # records.
    #
    # Cluster membership used to set this flag too, and that was wrong in a way the
    # blinded adjudication caught: of 18 sellers the classifier flagged while both
    # adjudicators called them independent, `self_loop` — the heaviest rule — had fired
    # on 17. The cause was membership in a proposed operator group, which is a
    # *hypothesis* built from shared payers. Sharing payers is normal for a marketplace
    # and is not evidence that anyone paid themselves. The rubric says a cluster is a
    # hypothesis, never a fact; treating it as proof of self-dealing contradicted that.
    #
    # Cluster membership is therefore recorded as its own fact and is not folded into
    # self-funding.
    self_funded: set[str] = set()
    for row in conn.execute("SELECT DISTINCT seller FROM settlements WHERE payer = seller"):
        self_funded.add(row["seller"])

    features: list[SellerFeatures] = []
    for row in rows:
        seller = row["seller"]
        n = int(row["n"])
        payers = int(row["payers"])

        seller_amounts = amounts.get(seller, {})
        seller_relayers = relayers.get(seller, {})
        busiest = max(seller_relayers.values()) if seller_relayers else 0

        fresh = sum(
            1
            for (s, payer), first_time in payer_seller_first.items()
            if s == seller
            and payer_first.get(payer, first_time) >= first_time - FRESH_PAYER_WINDOW_SECONDS
        )

        features.append(
            SellerFeatures(
                seller=seller,
                settlement_count=n,
                unique_payers=payers,
                repeat_buyer_rate=(repeat_buyers.get(seller, 0) / payers) if payers else 0.0,
                reciprocity_rate=(reciprocal.get(seller, 0) / payers) if payers else 0.0,
                amount_entropy=round(shannon_entropy_normalised(dict(seller_amounts)), 6),
                burstiness=round(burstiness(times.get(seller, [])), 6),
                peak_hour_share=round(peak_hour_share(times.get(seller, [])), 6),
                fresh_payer_share=round(fresh / payers, 6) if payers else 0.0,
                self_funding_flag=1 if seller in self_funded else 0,
                relayer_concentration=round(busiest / n, 6) if (n and seller_relayers) else None,
                active_days=int(row["active_days"]),
                observation_window_hours=round(window_hours, 4),
                methodology_version=methodology_version,
            )
        )

    features.sort(key=lambda f: f.settlement_count, reverse=True)
    return features


def write_features(conn: sqlite3.Connection, features: list[SellerFeatures]) -> int:
    """Replace the `features` table with this run's output."""
    try:
        conn.execute("DELETE FROM features")
        conn.executemany(
            "INSERT INTO features (seller, settlement_count, unique_payers, repeat_buyer_rate, "
            "reciprocity_rate, amount_entropy, burstiness, peak_hour_share, fresh_payer_share, "
            "self_funding_flag, relayer_concentration, active_days, observation_window_hours, "
            "methodology_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    f.seller,
                    f.settlement_count,
                    f.unique_payers,
                    f.repeat_buyer_rate,
                    f.reciprocity_rate,
                    f.amount_entropy,
                    f.burstiness,
                    f.peak_hour_share,
                    f.fresh_payer_share,
                    f.self_funding_flag,
                    f.relayer_concentration,
                    f.active_days,
                    f.observation_window_hours,
                    f.methodology_version,
                )
                for f in features
            ],
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    return len(features)
