"""Sensitivity analysis (prompt task A6, gate check GA.8).

Perturbs every classifier threshold and weight by ±25% and reports how far the
headline aggregate moves. The point is honesty about the weights: they are judgement,
so the report has to show how much the conclusion depends on them.

What is measured: the **share of settlements belonging to scored sellers with
signaling_likelihood at or above 0.5**. The baseline is read from the stored
classifications — the same rows the report counts — not recomputed from the rules
(review L1). Under perturbation the rules are recomputed, except where a recorded
`dispute_override` holds a seller's likelihood fixed, so a correction cannot make
this figure describe a different number than the headline. Sellers below the
evidence minimum are excluded because no likelihood is published for them (review
K1). The exclusions are the same `scores.status` and stored-classification
boundaries the report and the API use.

If the shift exceeds `phaseA.sensitivity_unstable_share_shift_pp`, the v0 report's
headline must say the estimate is unstable. Gate check GA.8 reads that.

Run:  uv run python scripts/sensitivity.py
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from repindex.classify import features as feat
from repindex.classify import rules
from repindex.common.db import connect_readonly
from repindex.common.provenance import (
    CommitObserver,
    DatabaseChangedError,
    PendingWALError,
    attest_identity,
    provenance,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "repindex.db"
OUT_PATH = REPO_ROOT / "reports" / "sensitivity.json"

FLAG_CUTOFF = 0.5
PERTURBATION = 0.25


def stored_classifications(
    conn: sqlite3.Connection,
) -> tuple[dict[str, float], set[str]]:
    """The published classifications and the sellers held by a recorded override.

    The report's headline is computed from `classifications`, so the sensitivity
    baseline must be too (review L1). A `dispute_override` contribution marks a
    seller whose stored value is fixed by a correction rather than by the rules.
    """
    stored: dict[str, float] = {}
    overrides: set[str] = set()
    for row in conn.execute(
        "SELECT seller, signaling_likelihood, contributions FROM classifications"
    ):
        seller = row["seller"]
        stored[seller] = float(row["signaling_likelihood"])
        try:
            contributions = json.loads(row["contributions"])
        except json.JSONDecodeError:
            contributions = []
        if any(
            isinstance(item, dict) and item.get("rule") == "dispute_override"
            for item in contributions
        ):
            overrides.add(seller)
    return stored, overrides


def stored_share(
    features: list[feat.SellerFeatures],
    scored: set[str],
    stored: dict[str, float],
) -> float:
    """The headline share, read from the stored classifications the report counts.

    The denominator stays the whole window: the headline is "share of settlements in
    this window", and only the numerator is restricted to sellers the index judges.
    A scored seller with no classification row is not counted, which is what the
    report's inner join does.
    """
    if not features:
        return 0.0
    total = sum(f.settlement_count for f in features)
    if total == 0:
        return 0.0
    flagged = sum(
        f.settlement_count
        for f in features
        if f.seller in scored and stored.get(f.seller, 0.0) >= FLAG_CUTOFF
    )
    return flagged / total


def perturbed_share(
    features: list[feat.SellerFeatures],
    thresholds: rules.RuleThresholds,
    scored: set[str],
    stored: dict[str, float],
    overrides: set[str],
) -> float:
    """The share after a rule perturbation, holding recorded overrides fixed."""
    if not features:
        return 0.0
    total = sum(f.settlement_count for f in features)
    if total == 0:
        return 0.0
    flagged = 0
    for f in features:
        if f.seller not in scored:
            continue
        if f.seller in overrides and f.seller in stored:
            likelihood = stored[f.seller]
        else:
            likelihood = rules.classify_seller(f, thresholds).signaling_likelihood
        if likelihood >= FLAG_CUTOFF:
            flagged += f.settlement_count
    return flagged / total


def main() -> int:
    if not DB_PATH.is_file():
        print(f"{DB_PATH} not found — run ingestion first. Not writing a report from no data.")
        return 1
    # M2/N1/P1: the artifact records the main-file hash, so hash the checkpointed
    # state *before* the reads and confirm it again before writing. The observer
    # connection opens before the hash and is checked after the confirmation, so a
    # writer that commits during the multi-second hash and restores the bytes before
    # the confirming hash is refused (review P1).
    try:
        with CommitObserver(DB_PATH):
            attested = attest_identity(DB_PATH)
            conn = connect_readonly(DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                features = feat.compute_features(conn)
                try:
                    scored = {
                        row[0]
                        for row in conn.execute(
                            "SELECT identifier FROM scores WHERE status = 'scored'"
                        )
                    }
                    stored, overrides = stored_classifications(conn)
                except sqlite3.OperationalError as exc:
                    print(
                        f"cannot read scores/classifications from {DB_PATH.name} ({exc}) — "
                        "run scoring first."
                    )
                    return 1
            finally:
                conn.close()
            # N1: the reads are complete and the perturbation arithmetic below uses
            # only the in-memory rows. This confirms the database still has the
            # identity taken before the reads and records that same hash.
            provenance_fields = provenance(DB_PATH, attested)
    except (PendingWALError, DatabaseChangedError) as exc:
        print(f"{exc} Sensitivity is not written from a state it cannot name.")
        return 1

    if not features:
        print("no settlements in the database — not writing a report from no data.")
        return 1

    baseline_thresholds = rules.DEFAULT_THRESHOLDS
    baseline = stored_share(features, scored, stored)

    results: dict[str, Any] = {
        **provenance_fields,
        "flag_cutoff": FLAG_CUTOFF,
        "perturbation_pct": int(PERTURBATION * 100),
        "scored_only": True,
        "baseline_from": "stored_classifications",
        "override_seller_count": len(overrides & scored),
        "baseline_share": round(baseline, 6),
        "seller_count": len(features),
        "scored_seller_count": len(scored),
        "settlement_count": sum(f.settlement_count for f in features),
        "perturbations": {},
    }

    # Global: every threshold and weight at once, in both directions. This is the
    # reading the prompt asks for — "perturb every classifier threshold".
    for label, factor in (("global_down_25", 1 - PERTURBATION), ("global_up_25", 1 + PERTURBATION)):
        share = perturbed_share(
            features, baseline_thresholds.perturbed(factor), scored, stored, overrides
        )
        results["perturbations"][label] = {
            "factor": round(factor, 4),
            "share": round(share, 6),
            "shift_pp": round((share - baseline) * 100, 4),
        }

    # One field at a time, so the report can name which cutoff the result leans on.
    for field_name in asdict(baseline_thresholds):
        for direction, factor in (("down", 1 - PERTURBATION), ("up", 1 + PERTURBATION)):
            perturbed = baseline_thresholds.perturbed(1.0)
            value = getattr(baseline_thresholds, field_name)
            if isinstance(value, int) and not isinstance(value, bool):
                new_value = max(1, round(value * factor))
            else:
                new_value = round(float(value) * factor, 6)
            perturbed = rules.replace_field(perturbed, field_name, new_value)
            share = perturbed_share(features, perturbed, scored, stored, overrides)
            results["perturbations"][f"{field_name}_{direction}_25"] = {
                "value": new_value,
                "share": round(share, 6),
                "shift_pp": round((share - baseline) * 100, 4),
            }

    shifts = [abs(entry["shift_pp"]) for entry in results["perturbations"].values()]
    results["max_share_shift_pp"] = round(max(shifts), 4) if shifts else 0.0
    results["baseline_share_pct"] = round(baseline * 100, 4)
    results["note"] = (
        "The baseline is the share of settlements from scored sellers at or above the flag "
        "cutoff in the stored classifications — the number the report publishes. Each "
        "perturbation changes one threshold or weight, or all at once, by 25% in each "
        "direction and recomputes the rules, except for sellers held by a recorded "
        "dispute_override, whose stored likelihood is held fixed. Sellers below the evidence "
        "minimum are excluded because no likelihood is published for them. The weights are "
        "judgement, not measurement, so this table is the honest bound on how much the "
        "headline depends on them."
    )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(f"baseline share        : {results['baseline_share_pct']:.2f}%")
    print(f"max shift             : {results['max_share_shift_pp']:.2f} pp")
    worst = max(results["perturbations"].items(), key=lambda kv: abs(kv[1]["shift_pp"]))
    print(f"most sensitive to     : {worst[0]} ({worst[1]['shift_pp']:+.2f} pp)")
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
