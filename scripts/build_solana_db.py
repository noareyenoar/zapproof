"""Build `data/solana.db` from the measured Solana transaction sample.

The output is a separate, read-only evidence index for the Solana rail. It is not
merged with the Base index: a score computed from seven days of Solana evidence and a
score computed from months of Base evidence answer different questions, and blending
them would hide which rail the evidence came from.

WHAT IS IN THE DATABASE
    `solana_settlements` holds one row per USDC inflow, keyed by `(signature,
    account_index)`. A compatibility VIEW named `settlements` exposes the columns the
    existing feature/classification/score code reads, so the scoring method is the same
    code, not a copy.

WHAT IS DELIBERATELY NOT THERE
    No `facilitator_relayer`: on Solana every transfer is carried by a facilitator, so
    concentration on one is how x402 works, not a signal about the seller. The
    `single_relayer` rule therefore abstains exactly as it does for a Base seller whose
    envelope was not fetched. Treating "Coinbase carried 100% of this seller's
    payments" as a warning would penalise every honest x402 seller.

WHAT THIS IS NOT
    A census. `solana_meta` records the window, the allowlist date and the caveats. Any
    answer built from this file must show them.

Usage:
    uv run python scripts/build_solana_db.py
    uv run python scripts/build_solana_db.py --input tmp/solana/transactions.jsonl
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from repindex.classify import features as feat
from repindex.classify import rules
from repindex.score import rating

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = REPO_ROOT / "tmp" / "solana" / "transactions.jsonl"
DEFAULT_OUTPUT = REPO_ROOT / "data" / "solana.db"
THRESHOLDS = REPO_ROOT / "config" / "thresholds.yaml"

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6

DDL = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS solana_settlements (
    signature      TEXT    NOT NULL,
    account_index  INTEGER NOT NULL,
    slot           INTEGER NOT NULL,
    block_time     INTEGER NOT NULL,
    mint           TEXT    NOT NULL,
    decimals       INTEGER NOT NULL,
    raw_amount     INTEGER NOT NULL,
    payer          TEXT    NOT NULL,
    payee          TEXT    NOT NULL,
    fee_payer      TEXT    NOT NULL,
    facilitator    TEXT    NOT NULL,
    PRIMARY KEY (signature, account_index)
);

CREATE INDEX IF NOT EXISTS idx_solana_payee ON solana_settlements (payee);
CREATE INDEX IF NOT EXISTS idx_solana_payer ON solana_settlements (payer);

-- The existing scoring code reads `settlements`; this view is the adapter. `block_hash`
-- is unused by every read path and is empty on purpose; `nonce` is the signature, and
-- the natural key is already (signature, account_index).
CREATE VIEW IF NOT EXISTS settlements AS
SELECT signature     AS tx_hash,
       account_index AS log_index,
       slot          AS block_number,
       ''            AS block_hash,
       block_time,
       mint          AS token,
       decimals,
       raw_amount,
       payer,
       payee         AS seller,
       NULL          AS facilitator_relayer,
       signature     AS nonce,
       'spl_transfer_checked' AS mechanism
FROM solana_settlements;

CREATE TABLE IF NOT EXISTS addresses (
    address        TEXT PRIMARY KEY,
    first_seen_block INTEGER,
    last_seen_block  INTEGER,
    is_payer       INTEGER NOT NULL DEFAULT 0,
    is_seller      INTEGER NOT NULL DEFAULT 0,
    is_relayer     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS features (
    seller                TEXT PRIMARY KEY,
    unique_payers         INTEGER NOT NULL,
    repeat_buyer_rate     REAL    NOT NULL,
    reciprocity_rate      REAL    NOT NULL,
    amount_entropy        REAL    NOT NULL,
    burstiness            REAL    NOT NULL,
    peak_hour_share       REAL    NOT NULL,
    fresh_payer_share     REAL    NOT NULL,
    self_funding_flag     INTEGER NOT NULL,
    relayer_concentration REAL,
    settlement_count      INTEGER NOT NULL,
    active_days           INTEGER NOT NULL,
    observation_window_hours REAL NOT NULL DEFAULT 0.0,
    methodology_version   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS classifications (
    seller               TEXT PRIMARY KEY,
    signaling_likelihood REAL NOT NULL
        CHECK (signaling_likelihood >= 0.0 AND signaling_likelihood <= 1.0),
    contributions        TEXT NOT NULL,
    methodology_version  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settlement_flags (
    tx_hash     TEXT    NOT NULL,
    log_index   INTEGER NOT NULL,
    flag        TEXT    NOT NULL,
    detail      TEXT,
    PRIMARY KEY (tx_hash, log_index, flag)
);

CREATE TABLE IF NOT EXISTS scores (
    identifier        TEXT PRIMARY KEY,
    status            TEXT NOT NULL,
    score             REAL,
    confidence_band   TEXT NOT NULL,
    evidence_settlements INTEGER NOT NULL,
    evidence_counterparties INTEGER NOT NULL,
    evidence_active_days INTEGER NOT NULL,
    data_window_start INTEGER NOT NULL,
    data_window_end   INTEGER NOT NULL,
    as_of_block       INTEGER NOT NULL,
    methodology_version TEXT NOT NULL,
    CHECK (
        (status = 'scored' AND score IS NOT NULL)
        OR (status = 'insufficient_evidence' AND score IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS labels (
    address       TEXT    NOT NULL,
    label_type    TEXT    NOT NULL,
    label_value   TEXT    NOT NULL,
    method        TEXT    NOT NULL CHECK (length(method) > 0),
    confidence    REAL    NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    evidence      TEXT    NOT NULL CHECK (length(evidence) > 0),
    methodology_version TEXT NOT NULL CHECK (length(methodology_version) > 0),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (address, label_type, label_value)
);

CREATE TABLE IF NOT EXISTS solana_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise SystemExit(f"input not found: {path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def read_allowlist(path: Path) -> tuple[set[str], str]:
    """The classifier is an input, not something to re-invent at build time."""
    if not path.is_file():
        raise SystemExit(f"allowlist not found: {path} (run `solana_sample.py sigs` first)")
    payload = json.loads(path.read_text(encoding="utf-8"))
    addresses = {address for addrs in payload["facilitators"].values() for address in addrs}
    return addresses, str(payload.get("fetched", "unknown"))


def build(
    input_path: Path, output_path: Path, allowed: set[str], allowlist_date: str
) -> dict[str, Any]:
    rows = _rows(input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    conn = sqlite3.connect(output_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(DDL)

        settlements: list[tuple[Any, ...]] = []
        payers: set[str] = set()
        sellers: set[str] = set()
        facilitators: set[str] = set()
        skipped = {"failed": 0, "fee_payer_not_allowed": 0, "no_transfer": 0, "no_time": 0}
        for row in rows:
            if row.get("err") is not None:
                skipped["failed"] += 1
                continue
            fee_payer = row.get("fee_payer")
            if not row.get("fee_payer_allowed") or fee_payer not in allowed:
                skipped["fee_payer_not_allowed"] += 1
                continue
            deltas = [d for d in row.get("deltas") or [] if d.get("owner")]
            inflows = [d for d in deltas if d["delta"] > 0 and d["owner"] not in allowed]
            outflows = [d for d in deltas if d["delta"] < 0]
            if not inflows or not outflows:
                skipped["no_transfer"] += 1
                continue
            if row.get("block_time") is None or row.get("slot") is None:
                skipped["no_time"] += 1
                continue
            payer = max(outflows, key=lambda d: abs(d["delta"]))["owner"]
            payers.add(payer)
            facilitators.add(fee_payer)
            for inflow in inflows:
                seller = inflow["owner"]
                sellers.add(seller)
                settlements.append(
                    (
                        row["signature"],
                        int(inflow["account_index"]),
                        int(row["slot"]),
                        int(row["block_time"]),
                        USDC_MINT,
                        USDC_DECIMALS,
                        int(inflow["delta"]),
                        payer,
                        seller,
                        fee_payer,
                        row.get("facilitator") or "",
                    )
                )

        conn.executemany(
            "INSERT OR IGNORE INTO solana_settlements "
            "(signature, account_index, slot, block_time, mint, decimals, raw_amount, "
            " payer, payee, fee_payer, facilitator) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            settlements,
        )

        first_last: dict[str, tuple[int, int]] = {}
        for row in conn.execute(
            "SELECT payer AS address, MIN(slot) AS lo, MAX(slot) AS hi FROM solana_settlements "
            "GROUP BY payer UNION ALL "
            "SELECT payee AS address, MIN(slot) AS lo, MAX(slot) AS hi FROM solana_settlements "
            "GROUP BY payee"
        ):
            address = row["address"]
            lo, hi = int(row["lo"]), int(row["hi"])
            current = first_last.get(address)
            first_last[address] = (
                min(lo, current[0]) if current else lo,
                max(hi, current[1]) if current else hi,
            )
        conn.executemany(
            "INSERT INTO addresses (address, first_seen_block, last_seen_block, "
            " is_payer, is_seller, is_relayer) VALUES (?,?,?,?,?,?)",
            [
                (
                    address,
                    lo,
                    hi,
                    1 if address in payers else 0,
                    1 if address in sellers else 0,
                    0,
                )
                for address, (lo, hi) in first_last.items()
            ],
        )
        conn.executemany(
            "UPDATE addresses SET is_relayer = 1 WHERE address = ?",
            [(address,) for address in facilitators],
        )

        features = feat.compute_features(conn)
        feat.write_features(conn, features)
        classifications = rules.classify_all(features)
        rules.write_classifications(conn, classifications)

        with THRESHOLDS.open(encoding="utf-8") as fh:
            thresholds = yaml.safe_load(fh) or {}
        minimum = rating.EvidenceMinimum.from_thresholds(thresholds)
        scores = rating.compute_scores(conn, minimum)
        rating.write_scores(conn, scores)
        rules.flag_settlements(conn)

        window = conn.execute(
            "SELECT COUNT(*) AS n, MIN(block_time) AS lo, MAX(block_time) AS hi, "
            "MAX(slot) AS head, COUNT(DISTINCT payee) AS sellers, "
            "COUNT(DISTINCT payer) AS payers FROM solana_settlements"
        ).fetchone()
        meta = {
            "generated_at": datetime.now(UTC).isoformat(),
            "method": "fee-payer allowlist + USDC balance deltas",
            "evidence_class": "x402_fee_payer_allowlist",
            "allowlist_date": allowlist_date,
            "allowlist_size": len(allowed),
            "settlements": int(window["n"]),
            "sellers": int(window["sellers"]),
            "payers": int(window["payers"]),
            "window_start": int(window["lo"] or 0),
            "window_end": int(window["hi"] or 0),
            "as_of_slot": int(window["head"] or 0),
            "scored_sellers": sum(1 for s in scores if s.status == "scored"),
            "insufficient_evidence_sellers": sum(
                1 for s in scores if s.status == "insufficient_evidence"
            ),
            "skipped": skipped,
            "caveats": (
                "A seven-day observation window, not a census. No on-chain x402 marker "
                "exists on Solana; the fee-payer allowlist is the classifier and may miss "
                "or over-include. Payer independence and delivery are not proven. "
                "`as_of_block` is the highest Solana slot seen, not a Base block."
            ),
        }
        conn.executemany(
            "INSERT INTO solana_meta (key, value) VALUES (?, ?)",
            [(key, json.dumps(value)) for key, value in meta.items()],
        )
        conn.commit()
        return meta
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_INPUT.parent / "allowlist.json")
    args = parser.parse_args()
    allowed, allowlist_date = read_allowlist(args.allowlist)
    meta = build(args.input, args.output, allowed, allowlist_date)
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
