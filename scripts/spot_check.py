"""Build the human spot-check pack (prompt task A6).

Produces two files:

  reports/spot_check.md                  one card per seller, with the evidence
  reports/A_spot_check_template.csv      an empty sheet for the owner to fill

**This is the one artefact that names addresses**, and it is deliberately kept out of
the public report. Prompt task A6 needs the owner to look at individual sellers and
disagree; that is impossible with aggregates. The file is for review, not publication.

The sample is stratified so the review is not just the biggest sellers:
  - the top sellers by volume
  - a random sample from everyone else
  - borderline cases, whose likelihood sits near the 0.5 cutoff

Run:  uv run python scripts/spot_check.py
"""

from __future__ import annotations

import csv
import json
import random
import sqlite3
from pathlib import Path

import yaml

from repindex.common.db import connect_readonly

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "repindex.db"
THRESHOLDS = REPO_ROOT / "config" / "thresholds.yaml"
OUT_MD = REPO_ROOT / "reports" / "spot_check.md"
OUT_CSV = REPO_ROOT / "reports" / "A_spot_check_template.csv"

CUTOFF = 0.5
BORDERLINE_BAND = 0.12
RANDOM_SEED = 20261023


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
            "SELECT f.seller, f.settlement_count, f.unique_payers, f.repeat_buyer_rate, "
            "f.reciprocity_rate, f.amount_entropy, f.peak_hour_share, "
            "f.relayer_concentration, f.active_days, "
            "COALESCE(c.signaling_likelihood, 0) AS likelihood, "
            "COALESCE(c.contributions, '[]') AS contributions "
            "FROM features f LEFT JOIN classifications c ON c.seller = f.seller "
            "ORDER BY f.settlement_count DESC"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("no sellers to sample")
        return 1

    total = len(rows)
    # Split the configured sample across three strata and top up from whatever is
    # left if a stratum is short, so the count equals `spot_check_sample_size`.
    # Independent review found the earlier version produced 33 against a configured
    # 50, because a third of the sample was capped independently per stratum.
    want_top = sample_size // 3
    want_borderline = sample_size // 3
    want_random = sample_size - want_top - want_borderline

    top = list(rows[:want_top])

    borderline_pool = [r for r in rows if abs(float(r["likelihood"]) - CUTOFF) <= BORDERLINE_BAND]
    borderline_pool.sort(key=lambda r: abs(float(r["likelihood"]) - CUTOFF))
    borderline = borderline_pool[:want_borderline]

    chosen = {r["seller"] for r in top} | {r["seller"] for r in borderline}
    rest = [r for r in rows if r["seller"] not in chosen]
    rng = random.Random(RANDOM_SEED)
    random_pick = rng.sample(rest, min(want_random, len(rest))) if rest else []

    # Deduplicate BEFORE topping up. A seller can qualify for two strata — a high
    # volume seller can also sit near the cutoff — and removing the duplicate after
    # the fact silently shrank the sample: measured at 47 against a configured 50.
    def dedupe(items: list[sqlite3.Row]) -> list[sqlite3.Row]:
        seen_sellers: set[str] = set()
        out: list[sqlite3.Row] = []
        for item in items:
            if item["seller"] not in seen_sellers:
                seen_sellers.add(item["seller"])
                out.append(item)
        return out

    combined = dedupe(top + random_pick + borderline)

    shortfall = sample_size - len(combined)
    if shortfall > 0:
        already = {r["seller"] for r in combined}
        spare = [r for r in rows if r["seller"] not in already]
        combined.extend(rng.sample(spare, min(shortfall, len(spare))))

    sample = combined

    # Prompt task A6 asks for transaction links. A reviewer cannot check a judgement
    # against an aggregate; they need to open a payment. Three per seller keeps the
    # file readable while making the evidence checkable.
    conn = connect_readonly(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        examples: dict[str, list[sqlite3.Row]] = {}
        for row in sample:
            examples[row["seller"]] = conn.execute(
                "SELECT tx_hash, block_number, raw_amount, payer FROM settlements "
                "WHERE seller = ? ORDER BY block_number DESC LIMIT 3",
                (row["seller"],),
            ).fetchall()
    finally:
        conn.close()

    lines: list[str] = []
    lines.append("# Spot-check pack")
    lines.append("")
    lines.append(
        "**For the owner's review. This file names individual addresses and must not be "
        "published.** Prompt task A6 requires a human to check whether the classifier's "
        "judgements match their own reading of the evidence."
    )
    lines.append("")
    lines.append(f"- Sellers in the database: **{total:,}**")
    lines.append(f"- Sampled: **{len(sample)}** (`spot_check_sample_size` = {sample_size})")
    lines.append(
        f"- Stratification: {len(top)} top by volume, {len(random_pick)} random, "
        f"{len(borderline)} borderline (likelihood within ±{BORDERLINE_BAND} of {CUTOFF})"
    )
    lines.append("")
    lines.append(
        "Fill `A_spot_check_template.csv` with `agree` or `disagree` per seller. "
        "Gate A computes agreement and requires at least "
        "`spot_check_agreement_min`."
    )
    lines.append("")

    for index, row in enumerate(sample, 1):
        contributions = json.loads(row["contributions"])
        fired = [c for c in contributions if c.get("fired")]
        lines.append(f"## {index}. `{row['seller']}`")
        lines.append("")
        lines.append(f"- Settlements: **{row['settlement_count']:,}**")
        lines.append(f"- Distinct payers: **{row['unique_payers']:,}**")
        lines.append(f"- Repeat-buyer rate: {row['repeat_buyer_rate']:.1%}")
        lines.append(f"- Reciprocity: {row['reciprocity_rate']:.1%}")
        lines.append(f"- Amount entropy: {row['amount_entropy']:.3f}")
        lines.append(f"- Peak-hour share: {row['peak_hour_share']:.1%}")
        lines.append(f"- Busiest relayer share: {row['relayer_concentration']:.1%}")
        lines.append(f"- Active days: {row['active_days']}")
        lines.append(f"- **signaling_likelihood: {float(row['likelihood']):.3f}**")
        lines.append("")
        if fired:
            lines.append("Rules that fired:")
            for contribution in fired:
                lines.append(
                    f"  - `{contribution['rule']}` (+{contribution['contribution']:.2f}) — "
                    f"{contribution['detail']}"
                )
        else:
            lines.append("No rule fired.")
        lines.append("")

        seller_txs = examples.get(row["seller"], [])
        if seller_txs:
            lines.append("Recent payments (open these on basescan.org/tx/…):")
            for tx in seller_txs:
                lines.append(
                    f"  - [`{tx['tx_hash'][:18]}…`](https://basescan.org/tx/{tx['tx_hash']}) "
                    f"block {tx['block_number']:,}, {tx['raw_amount']:,} base units"
                )
            lines.append("")

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "seller",
                "settlements",
                "unique_payers",
                "signaling_likelihood",
                "evidence_summary",
                "your_verdict_agree_or_disagree",
                "your_note",
            ]
        )
        for row in sample:
            writer.writerow(
                [
                    row["seller"],
                    row["settlement_count"],
                    row["unique_payers"],
                    round(float(row["likelihood"]), 4),
                    f"{row['settlement_count']} settlements from {row['unique_payers']} payers, "
                    f"entropy {row['amount_entropy']:.3f}",
                    "",
                    "",
                ]
            )

    print(f"sampled {len(sample)} of {total} sellers")
    print(f"wrote {OUT_MD.relative_to(REPO_ROOT)}")
    print(f"wrote {OUT_CSV.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
