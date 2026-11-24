"""Cross-check the local index against logs fetched from a second RPC provider.

Gate A, check GA.3 asks for three random windows on different days where the
settlement count and total amount agree within 2% with a second source. Until now
there was no zero-cost source, so the check honestly reported "not measured".

This script uses an independent public Base JSON-RPC provider (`base-rpc.publicnode.com`
by default) rather than the endpoints ingestion used. It fetches the same
`AuthorizationUsed` and `Transfer` logs, decodes them with the repository's own
decoder, and compares the resulting settlement keys, payers, sellers and integer
amounts with the local database.

**What this proves, and what it does not.** A different RPC provider is an independent
operator serving the same chain, so this checks that our ingestion and decoding agree
with logs fetched independently. It is not an independent *indexer*: both sides read
the same Base chain, and neither can prove the chain itself is complete. The output
says so, and the gate result should be read with that sentence attached.

Run:
    uv run python scripts/crosscheck_second_source.py
    uv run python scripts/crosscheck_second_source.py --seed 20261123 --windows 3
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sqlite3
from pathlib import Path
from typing import Any

from repindex.common.rpc import rpc_call
from repindex.ingest.decode import (
    AUTHORIZATION_USED_TOPIC,
    TRANSFER_TOPIC,
    USDC_BASE,
    Settlement,
    decode_settlements,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "repindex.db"
DEFAULT_OUT = REPO_ROOT / "reports" / "crosscheck_A.json"
DEFAULT_RPC = "https://base-rpc.publicnode.com"
WINDOW_BLOCKS = 20


def _db_rows(
    conn: sqlite3.Connection, from_block: int, to_block: int
) -> list[tuple[str, int, int, str, str]]:
    return [
        (
            str(row["tx_hash"]),
            int(row["log_index"]),
            int(row["raw_amount"]),
            str(row["payer"]),
            str(row["seller"]),
        )
        for row in conn.execute(
            "SELECT tx_hash, log_index, raw_amount, payer, seller FROM settlements "
            "WHERE block_number BETWEEN ? AND ?",
            (from_block, to_block),
        )
    ]


def _source_settlements(rpc_url: str, from_block: int, to_block: int) -> list[Settlement]:
    logs = rpc_call(
        "eth_getLogs",
        [
            {
                "address": USDC_BASE,
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "topics": [[AUTHORIZATION_USED_TOPIC, TRANSFER_TOPIC]],
            }
        ],
        urls=(rpc_url,),
        timeout=60.0,
    )
    if not isinstance(logs, list):
        raise RuntimeError(f"unexpected eth_getLogs result: {logs!r}")
    return decode_settlements(logs, None)


def _relative_diff(a: int, b: int) -> float:
    if a == b:
        return 0.0
    denominator = max(abs(a), abs(b))
    return abs(a - b) / denominator if denominator else 0.0


def _pick_windows(
    conn: sqlite3.Connection, windows: int, rng: random.Random
) -> list[tuple[int, int, str]]:
    days = [
        int(row["day"])
        for row in conn.execute(
            "SELECT DISTINCT block_time / 86400 AS day FROM settlements ORDER BY day"
        )
    ]
    if len(days) < windows:
        raise SystemExit(f"need {windows} distinct days, database has {len(days)}")
    chosen: list[tuple[int, int, str]] = []
    for day in rng.sample(days, windows):
        bounds = conn.execute(
            "SELECT MIN(block_number) AS lo, MAX(block_number) AS hi FROM settlements "
            "WHERE block_time / 86400 = ?",
            (day,),
        ).fetchone()
        lo, hi = int(bounds["lo"]), int(bounds["hi"])
        start = rng.randint(lo, max(lo, hi - WINDOW_BLOCKS + 1))
        end = min(start + WINDOW_BLOCKS - 1, hi)
        label = dt.datetime.fromtimestamp(day * 86_400, tz=dt.UTC).date().isoformat()
        chosen.append((start, end, label))
    return sorted(chosen)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--rpc-url", default=DEFAULT_RPC)
    parser.add_argument("--windows", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20261123)
    args = parser.parse_args(argv)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        ranges = _pick_windows(conn, args.windows, random.Random(args.seed))
        results: list[dict[str, Any]] = []
        for from_block, to_block, day in ranges:
            db_rows = _db_rows(conn, from_block, to_block)
            source = _source_settlements(args.rpc_url, from_block, to_block)

            db_by_key = {
                (tx, log): (amount, payer, seller)
                for tx, log, amount, payer, seller in db_rows
            }
            source_by_key = {
                (s.tx_hash, s.log_index): (s.raw_amount, s.payer, s.seller) for s in source
            }
            missing = sorted(set(db_by_key) - set(source_by_key))
            extra = sorted(set(source_by_key) - set(db_by_key))
            mismatches = sum(
                1
                for key in set(db_by_key) & set(source_by_key)
                if db_by_key[key] != source_by_key[key]
            )
            db_count, source_count = len(db_rows), len(source)
            db_amount = sum(row[2] for row in db_rows)
            source_amount = sum(s.raw_amount for s in source)

            results.append(
                {
                    "window": f"{from_block}-{to_block}",
                    "day": day,
                    "blocks": to_block - from_block + 1,
                    "db_count": db_count,
                    "source_count": source_count,
                    "count_relative_diff": round(
                        _relative_diff(db_count, source_count), 8
                    ),
                    "db_amount_raw": db_amount,
                    "source_amount_raw": source_amount,
                    "amount_relative_diff": round(
                        _relative_diff(db_amount, source_amount), 8
                    ),
                    "missing_in_source": len(missing),
                    "extra_in_source": len(extra),
                    "field_mismatches": mismatches,
                }
            )
            print(
                f"{day} {from_block}-{to_block}: db {db_count}/{db_amount} "
                f"source {source_count}/{source_amount} "
                f"missing {len(missing)} extra {len(extra)} mismatches {mismatches}"
            )
    finally:
        conn.close()

    payload = {
        "generated": dt.datetime.now(tz=dt.UTC).isoformat(),
        "database": (
            str(args.db.relative_to(REPO_ROOT))
            if args.db.is_relative_to(REPO_ROOT)
            else str(args.db)
        ),
        "source": {
            "name": args.rpc_url,
            "class": "independent public JSON-RPC provider",
            "endpoint_used_by_ingestion": False,
            "note": (
                "A different RPC provider is an independent operator serving the same Base "
                "chain. This verifies our ingestion and decoding against independently fetched "
                "logs; it is not an independent indexer and cannot prove chain completeness."
            ),
        },
        "selection": {
            "method": "seeded random block window of 20 blocks on each of three distinct days",
            "seed": args.seed,
            "windows": args.windows,
            "window_blocks": WINDOW_BLOCKS,
        },
        "windows": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
