"""Build the Phase 0 scratch table of decoded settlements (gate check G0.2).

G0.2 requires at least `phase0.decoded_sample_min` (1,000) settlements decoded into
a scratch table, and then requires that a random sample of
`phase0.onchain_recheck_sample` (50) of them re-verify against
`eth_getTransactionReceipt` with a 100% match. The gate does the re-verification
itself; this script produces the table it reads.

Decoding uses the **event** path, not calldata, because the event path covers every
settlement regardless of how the transaction was wrapped (see ADR 0001: 214/214
versus 153/214 in a sample window).

That means both log kinds are needed, which sets the query shape: a combined
`AuthorizationUsed|Transfer` query exceeds the endpoint's 20,000-result cap at 400
blocks, because USDC emits roughly 105 transfers per block against ~2.4 settlements.
So the window is walked in chunks small enough to stay under the cap.

Amounts are stored as INTEGER base units. Nothing here stores a float.

Usage:
    uv run python scripts/build_scratch.py --blocks 500
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from repindex.common.rpc import block_number, get_logs, rpc_call
from repindex.ingest.decode import (
    AUTHORIZATION_USED_TOPIC,
    TRANSFER_TOPIC,
    USDC_BASE,
    Settlement,
    decode_settlements,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "scratch.db"

# Largest window measured to stay under the 20,000-result cap for a combined query.
OR_QUERY_CHUNK = 150

SCHEMA = """
CREATE TABLE IF NOT EXISTS settlements (
    tx_hash              TEXT    NOT NULL,
    log_index            INTEGER NOT NULL,
    block_number         INTEGER NOT NULL,
    token                TEXT    NOT NULL,
    payer                TEXT    NOT NULL,
    seller               TEXT    NOT NULL,
    facilitator_relayer  TEXT    NOT NULL,
    raw_amount           INTEGER NOT NULL,   -- base units, never a float
    decimals             INTEGER NOT NULL,
    nonce                TEXT    NOT NULL,
    mechanism            TEXT    NOT NULL,
    PRIMARY KEY (tx_hash, log_index)
);
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks", type=int, default=500)
    args = parser.parse_args()

    head = block_number()
    to_block = head - 60
    from_block = to_block - args.blocks + 1

    print(f"window {from_block:,}..{to_block:,} ({args.blocks} blocks)")

    logs: list[dict] = []
    cursor = from_block
    while cursor <= to_block:
        chunk_end = min(cursor + OR_QUERY_CHUNK - 1, to_block)
        chunk = get_logs(
            address=USDC_BASE,
            topics=[[AUTHORIZATION_USED_TOPIC, TRANSFER_TOPIC]],
            from_block=cursor,
            to_block=chunk_end,
        )
        logs.extend(chunk)
        print(f"  {cursor:,}-{chunk_end:,}: {len(chunk):,} logs")
        cursor = chunk_end + 1

    auth = sum(1 for log in logs if log["topics"][0] == AUTHORIZATION_USED_TOPIC)
    transfer = sum(1 for log in logs if log["topics"][0] == TRANSFER_TOPIC)
    print(f"total {len(logs):,} logs ({auth:,} AuthorizationUsed, {transfer:,} Transfer)")

    block_numbers = sorted({int(log["blockNumber"], 16) for log in logs})
    print(f"fetching {len(block_numbers):,} blocks for transaction senders")

    senders: dict[str, str] = {}
    for index, number in enumerate(block_numbers, 1):
        block = rpc_call("eth_getBlockByNumber", [hex(number), True])
        if not isinstance(block, dict):
            continue
        for tx in block.get("transactions") or []:
            senders[tx["hash"]] = tx["from"].lower()
        if index % 50 == 0:
            print(f"  {index}/{len(block_numbers)} blocks")

    settlements: list[Settlement] = decode_settlements(logs, senders)
    print(f"decoded {len(settlements):,} settlements")

    # The window that actually contains settlements, which can be narrower than the
    # window requested if the earliest blocks hold no events. Recorded because the
    # difference is not obvious from the artefact otherwise.
    actual_from = min((s.block_number for s in settlements), default=from_block)
    covered_blocks = to_block - actual_from + 1

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    try:
        connection.execute(SCHEMA)
        # A scratch table represents ONE ingest, so start clean. Without this, a
        # rebuild silently unions the new window with whatever was there before —
        # measured 2026-10-23: a 500-block run left 9,017 rows where it had decoded
        # 7,732, because 1,285 rows from an earlier window survived. The union is
        # harmless for G0.2 but makes ingest_meta describe a window the table does
        # not actually match.
        connection.execute("DELETE FROM settlements")
        connection.executemany(
            "INSERT OR REPLACE INTO settlements VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    s.tx_hash,
                    s.log_index,
                    s.block_number,
                    s.token,
                    s.payer,
                    s.seller,
                    s.facilitator_relayer,
                    s.raw_amount,
                    s.decimals,
                    s.nonce,
                    s.mechanism,
                )
                for s in settlements
            ],
        )
        connection.commit()
        rows = connection.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
        distinct_tx = connection.execute(
            "SELECT COUNT(DISTINCT tx_hash) FROM settlements"
        ).fetchone()[0]
    finally:
        connection.close()

    # Record the window actually ingested, inside the database, so the artefact says
    # what it covers instead of relying on a docstring. Independent review found a
    # 452-block table sitting behind a 500-block docstring with nothing in the
    # artefact to explain the gap; this makes the covered range self-describing and
    # stops a future reader having to infer it.
    connection = sqlite3.connect(DB_PATH)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS ingest_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        for key, value in (
            ("from_block", str(actual_from)),
            ("to_block", str(to_block)),
            ("blocks_requested", str(args.blocks)),
            ("blocks_covered", str(covered_blocks)),
            ("ingested_at", datetime.now(UTC).isoformat(timespec="seconds")),
        ):
            connection.execute("INSERT OR REPLACE INTO ingest_meta VALUES (?, ?)", (key, value))
        connection.commit()
    finally:
        connection.close()

    print()
    print(f"rows in scratch table : {rows:,}")
    print(f"distinct transactions : {distinct_tx:,}")
    print(f"window requested      : {args.blocks} blocks ({from_block:,}..{to_block:,})")
    print(f"window with events    : {covered_blocks} blocks ({actual_from:,}..{to_block:,})")
    print(f"database              : {DB_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
