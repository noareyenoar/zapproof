"""Operator-group clustering (prompt task A3).

**A cluster is a hypothesis, never a fact.** Prompt task A3 says so directly, and it
is why every cluster carries `method`, `confidence`, `evidence` and
`methodology_version`, and why the wording throughout this module is "suggests"
rather than "is".

The signal implemented here is **shared payers**: if two sellers are paid by largely
the same set of addresses, they may be run by one operator. It is a weak signal on
its own — a marketplace with many sellers and few buyers would light up everywhere —
so the thresholds are deliberately conservative and every cluster records the exact
payers that produced it, letting a reader disagree.

**Not implemented:** "shared funding source within a bounded number of hops". That
requires tracing address funding on chain, which is expensive and would need its own
request budget. It is recorded here as absent rather than approximated, because a
made-up funding signal would look stronger than it is.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict

from repindex.common.version import METHODOLOGY_VERSION

METHOD = "shared_payers_jaccard"

# A payer who paid more than this many sellers produces O(k^2) pairs and is more
# likely a marketplace or an aggregator than evidence of one operator. Skipped, and
# the count of skipped payers is written into the evidence of nothing — it is
# returned instead, so the caller can see how much was excluded.
MAX_SELLERS_PER_PAYER = 60


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def find_clusters(
    conn: sqlite3.Connection,
    *,
    min_shared_payers: int = 5,
    min_jaccard: float = 0.6,
    min_settlements_per_seller: int = 10,
) -> tuple[list[list[str]], int]:
    """Return (clusters, skipped_payers).

    Clusters are lists of seller addresses, sorted by size descending. A pair is
    linked when they share at least `min_shared_payers` payers AND their payer sets
    overlap by at least `min_jaccard`.
    """
    seller_payers: dict[str, set[str]] = defaultdict(set)
    seller_counts: Counter[str] = Counter()
    for row in conn.execute("SELECT seller, payer FROM settlements"):
        seller_payers[row["seller"]].add(row["payer"])
        seller_counts[row["seller"]] += 1

    eligible = {
        seller for seller, count in seller_counts.items() if count >= min_settlements_per_seller
    }

    # Inverted index: which eligible sellers paid by this payer.
    by_payer: dict[str, set[str]] = defaultdict(set)
    skipped = 0
    for seller, payers in seller_payers.items():
        if seller not in eligible:
            continue
        for payer in payers:
            by_payer[payer].add(seller)

    pairs: Counter[tuple[str, str]] = Counter()
    for _payer, sellers in by_payer.items():
        if len(sellers) > MAX_SELLERS_PER_PAYER:
            skipped += 1
            continue
        ordered = sorted(sellers)
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                pairs[(ordered[i], ordered[j])] += 1

    parent: dict[str, str] = {seller: seller for seller in eligible}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for (a, b), shared in pairs.items():
        if shared < min_shared_payers:
            continue
        if _jaccard(seller_payers[a], seller_payers[b]) < min_jaccard:
            continue
        union(a, b)

    groups: dict[str, list[str]] = defaultdict(list)
    for seller in eligible:
        groups[find(seller)].append(seller)

    clusters = [sorted(members) for members in groups.values() if len(members) > 1]
    clusters.sort(key=len, reverse=True)
    return clusters, skipped


def write_clusters(
    conn: sqlite3.Connection,
    clusters: list[list[str]],
    *,
    methodology_version: str = METHODOLOGY_VERSION,
) -> int:
    """Persist clusters as `entities`, `entity_members` and `labels` rows."""
    # Payer sets are read once for the whole run. An earlier version queried inside
    # a double loop over members, which is O(n^2) round trips to SQLite for every
    # cluster — correct, but it would have made a large run look hung.
    all_sellers: set[str] = {member for members in clusters for member in members}
    payer_sets: dict[str, set[str]] = defaultdict(set)
    if all_sellers:
        placeholders = ",".join("?" * len(all_sellers))
        for row in conn.execute(
            f"SELECT seller, payer FROM settlements WHERE seller IN ({placeholders})",
            sorted(all_sellers),
        ):
            payer_sets[row["seller"]].add(row["payer"])

    written = 0
    try:
        # Clear previous clusters first. Independent review found a second run on the
        # same database produced two entity sets instead of replacing the first, which
        # would make GA.5's determinism comparison fail as soon as any cluster exists.
        conn.execute("DELETE FROM entity_members")
        conn.execute("DELETE FROM entities")
        conn.execute("DELETE FROM labels WHERE label_type = 'operator_group'")

        for members in clusters:
            shared = conn.execute(
                "SELECT payer, COUNT(DISTINCT seller) AS sellers FROM settlements "
                f"WHERE seller IN ({','.join('?' * len(members))}) "
                "GROUP BY payer ORDER BY sellers DESC",
                members,
            ).fetchall()
            shared_payers = [row["payer"] for row in shared]

            # Confidence is the mean best-Jaccard between each member and the others
            # in its cluster. It summarises how tightly the group holds together; it
            # is not a probability that the group is one operator.
            overlap_scores: list[float] = []
            for member in members:
                best = 0.0
                for other in members:
                    if other == member:
                        continue
                    best = max(best, _jaccard(payer_sets[member], payer_sets[other]))
                if best:
                    overlap_scores.append(best)
            confidence = sum(overlap_scores) / len(overlap_scores) if overlap_scores else 0.0
            confidence = max(0.0, min(1.0, round(confidence, 4)))

            evidence = json.dumps(
                {
                    "signal": "shared_payers",
                    "member_count": len(members),
                    "shared_payers": shared_payers[:20],
                    "note": "hypothesis, not a fact: overlapping payers may reflect a "
                    "marketplace with few buyers rather than one operator",
                }
            )

            cursor = conn.execute(
                "INSERT INTO entities (method, confidence, evidence, methodology_version) "
                "VALUES (?,?,?,?)",
                (METHOD, confidence, evidence, methodology_version),
            )
            entity_id = cursor.lastrowid

            for member in members:
                conn.execute(
                    "INSERT OR IGNORE INTO entity_members (entity_id, address) VALUES (?,?)",
                    (entity_id, member),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO labels "
                    "(address, label_type, label_value, method, confidence, evidence, "
                    " methodology_version) VALUES (?,?,?,?,?,?,?)",
                    (
                        member,
                        "operator_group",
                        f"cluster:{entity_id}",
                        METHOD,
                        confidence,
                        evidence,
                        methodology_version,
                    ),
                )
                written += 1
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    return written
