"""Planted-pattern validation (prompt task A6, gate check GA.7).

Generates synthetic settlements in a **separate database** — never the real one — then
runs the classifier and reports recall on the planted patterns and the false-positive
rate on synthetic genuine ones.

**This test is partly circular, and that is recorded, not glossed.** The patterns are
written by the same person who wrote the rules, so a high recall says the rules find
patterns someone planted, not that they find real signaling in the world. The prompt
requires this to be stated in the report; `circularity_disclosed` in the output is
what gate check GA.7 reads.

Run:  uv run python scripts/planted_patterns.py
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

from repindex.classify import features as feat
from repindex.classify import rules
from repindex.ingest import schema

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "planted_test.db"
OUT_PATH = REPO_ROOT / "reports" / "planted_pattern_results.json"

FLAG_CUTOFF = 0.5
BASE_TIME = 1_700_000_000
HOUR = 3_600

PATTERNS = (
    "self_loop",
    "ping_pong",
    "fan_in_fresh",
    "fixed_amount_burst",
    "genuine_varied",
    "genuine_repeat_customers",
)


def _insert(
    conn: sqlite3.Connection,
    *,
    seller: str,
    payer: str,
    amount: int,
    when: int,
    seq: int,
) -> None:
    conn.execute(
        "INSERT INTO settlements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"0x{seq:064x}",
            0,
            1_000 + seq,
            f"0xblock{seq}",
            when,
            "0xtoken",
            6,
            amount,
            f"{amount / 1e6:.6f}",
            payer,
            seller,
            f"0xrelayer{seq % 3}",
            f"0xnonce{seq}",
            "eip3009_authorization_used",
        ),
    )


def build_fixture(conn: sqlite3.Connection) -> Counter[str]:
    """Create one seller per pattern. Returns a count of settlements per pattern."""
    counts: Counter[str] = Counter()
    seq = 0

    # --- planted: a seller paying itself ---
    for i in range(40):
        seq += 1
        _insert(
            conn,
            seller="0xplanted_self",
            payer="0xplanted_self",
            amount=2000,
            when=BASE_TIME + i * HOUR,
            seq=seq,
        )
        counts["self_loop"] += 1

    # --- planted: two addresses paying each other in a tight loop ---
    for i in range(40):
        seq += 1
        payer = "0xping_a" if i % 2 == 0 else "0xping_b"
        _insert(
            conn,
            seller="0xplanted_ping",
            payer=payer,
            amount=2000,
            when=BASE_TIME + i * 600,
            seq=seq,
        )
        seq += 1
        _insert(
            conn,
            seller=payer,
            payer="0xplanted_ping",
            amount=2000,
            when=BASE_TIME + i * 600 + 60,
            seq=seq,
        )
        counts["ping_pong"] += 2

    # --- planted: many payers that appear only once, all at the same moment ---
    for i in range(30):
        seq += 1
        _insert(
            conn,
            seller="0xplanted_fanin",
            payer=f"0xfresh{i:03d}",
            amount=3000,
            when=BASE_TIME + i,
            seq=seq,
        )
        counts["fan_in_fresh"] += 1

    # --- planted: fixed amount, many settlements, one hour ---
    for i in range(60):
        seq += 1
        _insert(
            conn,
            seller="0xplanted_burst",
            payer=f"0xfixed{i % 8}",
            amount=2000,
            when=BASE_TIME + i * 30,
            seq=seq,
        )
        counts["fixed_amount_burst"] += 1

    # --- genuine: many distinct payers, varied amounts, spread out ---
    for i in range(80):
        seq += 1
        _insert(
            conn,
            seller="0xgenuine_varied",
            payer=f"0xbuyer{i:03d}",
            amount=1000 + (i * 7919) % 50_000,
            when=BASE_TIME + i * 4 * HOUR,
            seq=seq,
        )
        counts["genuine_varied"] += 1

    # --- genuine: repeat customers over time, varied amounts ---
    for i in range(80):
        seq += 1
        _insert(
            conn,
            seller="0xgenuine_repeat",
            payer=f"0xregular{i % 25:03d}",
            amount=1500 + (i * 104_729) % 20_000,
            when=BASE_TIME + i * 3 * HOUR,
            seq=seq,
        )
        counts["genuine_repeat_customers"] += 1

    conn.commit()
    return counts


def main() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    schema.initialize(DB_PATH)
    conn = schema.connect(DB_PATH)
    try:
        counts = build_fixture(conn)
        computed = feat.compute_features(conn)
        feat.write_features(conn, computed)
        classifications = rules.classify_all(computed)
        rules.write_classifications(conn, classifications)
    finally:
        conn.close()

    likelihood = {c.seller: c.signaling_likelihood for c in classifications}
    fired = {c.seller: sorted(o.rule for o in c.contributions if o.fired) for c in classifications}

    planted_sellers = [s for s in likelihood if s.startswith("0xplanted_")]
    genuine_sellers = [s for s in likelihood if s.startswith("0xgenuine_")]

    flagged_planted = [s for s in planted_sellers if likelihood[s] >= FLAG_CUTOFF]
    flagged_genuine = [s for s in genuine_sellers if likelihood[s] >= FLAG_CUTOFF]

    recall = len(flagged_planted) / len(planted_sellers) if planted_sellers else 0.0
    fpr = len(flagged_genuine) / len(genuine_sellers) if genuine_sellers else 0.0

    payload = {
        "cutoff": FLAG_CUTOFF,
        "patterns": list(PATTERNS),
        "settlements_per_pattern": dict(counts),
        "per_seller": {
            seller: {
                "likelihood": likelihood[seller],
                "flagged": likelihood[seller] >= FLAG_CUTOFF,
                "rules_fired": fired[seller],
            }
            for seller in sorted(likelihood)
        },
        "planted_count": len(planted_sellers),
        "planted_flagged": len(flagged_planted),
        "genuine_count": len(genuine_sellers),
        "genuine_flagged": len(flagged_genuine),
        "recall": round(recall, 6),
        "synthetic_genuine_fpr": round(fpr, 6),
        # Prompt task A6 requires this stated in the report, and GA.7 reads it.
        "circularity_disclosed": True,
        "circularity_note": (
            "This test is partly circular. The planted patterns were written by the "
            "same author as the rules, so a high recall shows the rules find patterns "
            "someone planted, not that they find real signaling. It tests mechanics, "
            "not real-world truth."
        ),
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"planted sellers : {len(planted_sellers)}, flagged {len(flagged_planted)}")
    print(f"genuine sellers : {len(genuine_sellers)}, flagged {len(flagged_genuine)}")
    print(f"recall          : {recall:.2%}")
    print(f"synthetic FPR   : {fpr:.2%}")
    for seller in sorted(likelihood):
        print(f"  {seller:26} {likelihood[seller]:.3f}  {','.join(fired[seller])}")
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)}")

    DB_PATH.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
