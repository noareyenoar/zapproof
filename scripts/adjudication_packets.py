"""Build blinded adjudication packets (prompt task A6, replacement spot-check).

Three files are written:

  reports/adjudication/packets.json        what the adjudicators see — evidence only
  reports/adjudication/truth.json          the classifier's bucket, for scoring only
  reports/adjudication/truth_broken.json   a deliberately wrong classifier's bucket

**The adjudicators receive `packets.json` and nothing else.** The two truth files
exist so the scoring step can compare; giving either to an adjudicator would destroy
the blinding and invalidate the run.

The packet is stripped of every classifier field. `scripts/validate_adjudication.py`
and the gate both grep for those field names, so a leak is caught mechanically rather
than trusted to care.

Run:  uv run python scripts/adjudication_packets.py
"""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

import yaml

from repindex.common.db import connect_readonly

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "repindex.db"
THRESHOLDS = REPO_ROOT / "config" / "thresholds.yaml"
OUT_DIR = REPO_ROOT / "reports" / "adjudication"

CUTOFF = 0.5
BORDERLINE_BAND = 0.10
RANDOM_SEED = 20261028

# Field names that must never appear in a packet. Checked mechanically downstream.
FORBIDDEN_FIELDS = (
    "signaling_likelihood",
    "likelihood",
    "contributions",
    "rules_fired",
    "classification",
    "confidence_band",
)


def classifier_bucket(likelihood: float) -> str:
    return "could_be_self_dealing" if likelihood >= CUTOFF else "requires_independent_customers"


def broken_bucket(unique_payers: int) -> str:
    """A deliberately wrong classifier: it treats success as suspicion.

    Used as the negative control. If the adjudication agrees with this as often as it
    agrees with the real classifier, the adjudication is not reading the evidence.
    """
    return "could_be_self_dealing" if unique_payers >= 50 else "requires_independent_customers"


def main() -> int:
    if not DB_PATH.is_file():
        print(f"{DB_PATH} missing — run scripts/run_phase_a.py first")
        return 1

    thresholds = yaml.safe_load(THRESHOLDS.read_text(encoding="utf-8")) or {}
    sample_size = int(thresholds.get("phaseA", {}).get("spot_check_sample_size", 50))

    conn = connect_readonly(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            # Only sellers the pipeline is willing to score. A seller below the
            # evidence minimum returns `insufficient_evidence`, and the project's own
            # rule is that unknown is not a verdict — so adjudicating one and counting
            # it as disagreement would punish the classifier for saying "I cannot
            # tell", which is the answer the whole project insists on.
            #
            # Measured in the third round: 30 of 100 sampled sellers were `cannot_tell`
            # for exactly this reason, all of them unscored, and they alone pushed
            # abstention to 36.5% and agreement down to 56% while the classifier's
            # precision on the sellers it did flag was 53 of 58.
            "SELECT f.seller, f.settlement_count, f.unique_payers, f.repeat_buyer_rate, "
            "f.reciprocity_rate, f.amount_entropy, f.peak_hour_share, "
            "f.relayer_concentration, f.active_days, f.observation_window_hours, "
            "f.self_funding_flag, "
            "COALESCE(c.signaling_likelihood, 0) AS likelihood "
            "FROM features f "
            "JOIN classifications c ON c.seller = f.seller "
            "JOIN scores s ON s.identifier = f.seller "
            "WHERE s.status = 'scored' "
            "ORDER BY f.settlement_count DESC"
        ).fetchall()

        if not rows:
            print("no sellers to sample")
            return 1

        # Operator-group membership, so the adjudicator can judge a fleet rather than
        # one address. Absent for most sellers today because clustering finds none;
        # the packet says so explicitly rather than implying the seller is alone.
        groups: dict[str, dict[str, object]] = {}
        for entity in conn.execute("SELECT entity_id, confidence FROM entities"):
            members = [
                r["address"]
                for r in conn.execute(
                    "SELECT address FROM entity_members WHERE entity_id = ?",
                    (entity["entity_id"],),
                )
            ]
            payer_total = conn.execute(
                f"SELECT COUNT(DISTINCT payer) FROM settlements "
                f"WHERE seller IN ({','.join('?' * len(members))})",
                members,
            ).fetchone()[0]
            for member in members:
                groups[member] = {
                    "group_id": entity["entity_id"],
                    "group_size": len(members),
                    "group_distinct_payers": int(payer_total),
                    "group_confidence": float(entity["confidence"]),
                }

        examples: dict[str, list[str]] = {}
        for row in rows:
            txs = [
                r["tx_hash"]
                for r in conn.execute(
                    "SELECT DISTINCT tx_hash FROM settlements WHERE seller = ? "
                    "AND tx_hash IS NOT NULL ORDER BY block_number DESC LIMIT 3",
                    (row["seller"],),
                )
            ]
            examples[row["seller"]] = txs
    finally:
        conn.close()

    total = len(rows)
    flagged = [r for r in rows if float(r["likelihood"]) >= CUTOFF]
    borderline = [r for r in rows if abs(float(r["likelihood"]) - CUTOFF) <= BORDERLINE_BAND]
    borderline.sort(key=lambda r: abs(float(r["likelihood"]) - CUTOFF))

    # Three groups, each sampled at random within its stratum:
    #
    #   flagged  — where the classifier makes a positive claim; agreement here is the
    #              headline number
    #   borderline — just either side of the cutoff, where a small change flips the
    #              answer
    #   the rest — a control, so a classifier that simply says "no" everywhere cannot
    #              score well by being right about nothing
    #
    # The first version took ALL flagged sellers, which produced 668 packets instead of
    # a sample. Adjudicating everything is not wrong, but it is not a sample either, and
    # it makes the run cost scale with how much the classifier flags.
    rng = random.Random(RANDOM_SEED)

    flagged_target = min(len(flagged), sample_size)
    borderline_target = min(len(borderline), sample_size // 2)
    random_target = sample_size // 2

    chosen: list[sqlite3.Row] = []
    seen: set[str] = set()

    for group, target in (
        (flagged, flagged_target),
        (borderline, borderline_target),
    ):
        pool = [r for r in group if r["seller"] not in seen]
        take = min(target, len(pool))
        if take:
            picked = rng.sample(pool, take)
            chosen.extend(picked)
            seen.update(r["seller"] for r in picked)

    rest = [r for r in rows if r["seller"] not in seen and r["seller"] not in set()]
    rest = [r for r in rest if float(r["likelihood"]) < CUTOFF]
    take = min(random_target, len(rest))
    if take:
        picked = rng.sample(rest, take)
        chosen.extend(picked)
        seen.update(r["seller"] for r in picked)

    packets = []
    truth = {}
    truth_broken = {}

    for row in chosen:
        seller = row["seller"]
        group = groups.get(seller)
        packet = {
            "seller": seller,
            "settlement_count": int(row["settlement_count"]),
            "distinct_payers": int(row["unique_payers"]),
            "repeat_buyer_rate": round(float(row["repeat_buyer_rate"]), 4),
            "reciprocity_rate": round(float(row["reciprocity_rate"]), 4),
            "amount_entropy": round(float(row["amount_entropy"]), 4),
            "peak_hour_share": round(float(row["peak_hour_share"]), 4),
            # None when no relayer was resolved. The packet says so rather than
            # presenting 0.0, which would read as "no concentration" instead of
            # "not measured" — the distinction the whole project rests on.
            "busiest_relayer_share": (
                round(float(row["relayer_concentration"]), 4)
                if row["relayer_concentration"] is not None
                else None
            ),
            "active_days": int(row["active_days"]),
            "observation_window_hours": round(float(row["observation_window_hours"]), 2),
            "sample_transactions": examples.get(seller, []),
            "operator_group": group
            if group
            else {
                "group_id": None,
                "group_size": 1,
                "group_distinct_payers": int(row["unique_payers"]),
                "note": "no operator group was formed for this seller; judged alone",
            },
        }
        packets.append(packet)
        truth[seller] = {
            "bucket": classifier_bucket(float(row["likelihood"])),
            "likelihood": round(float(row["likelihood"]), 4),
        }
        truth_broken[seller] = {"bucket": broken_bucket(int(row["unique_payers"]))}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "packets.json").write_text(json.dumps(packets, indent=2), encoding="utf-8")
    (OUT_DIR / "truth.json").write_text(json.dumps(truth, indent=2), encoding="utf-8")
    (OUT_DIR / "truth_broken.json").write_text(json.dumps(truth_broken, indent=2), encoding="utf-8")

    # Mechanical blinding check: a forbidden field name anywhere in the packet file is
    # a failure, not a warning.
    packet_text = (OUT_DIR / "packets.json").read_text(encoding="utf-8")
    leaked = [field for field in FORBIDDEN_FIELDS if field in packet_text]
    if leaked:
        print(f"BLINDING FAILED — packet contains classifier fields: {leaked}")
        return 1

    flagged_in_sample = sum(1 for p in packets if truth[p["seller"]]["likelihood"] >= CUTOFF)
    print(f"sellers in database : {total:,}")
    print(f"sample size         : {len(packets)} (configured {sample_size})")
    print(f"flagged in sample   : {flagged_in_sample} (need >= 10 to be discriminative)")
    print(f"with operator group : {sum(1 for p in packets if p['operator_group']['group_id'])}")
    print(f"wrote {OUT_DIR.relative_to(REPO_ROOT)}/packets.json, truth.json, truth_broken.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
