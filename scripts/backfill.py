"""Backfill settlements over a multi-day window, in resumable chunks.

The ingest path holds one window's logs in memory, so a seven-day range cannot be
fetched in one call: measured 2026-10-28, about 101 logs per block makes seven days
roughly 30 million logs. This walks the range in windows and commits each one, so a
crash costs at most one window and the checkpoint makes a restart resume rather than
repeat.

Measured costs on 2026-10-28 drive the defaults:
  - one `eth_getLogs` call covers 150 blocks before nearing the 20,000-result cap
  - a block header costs ~524 ms, but is not fetched at all: block hashes come from
    the logs and timestamps are derived from the block number
  - a seven-day log-only pass is about 96 minutes

The facilitator relayer is not resolved (see `fetch_settlements`); that is a
deliberate trade recorded in the schema.

Run:
    uv run python scripts/backfill.py --days 7
    uv run python scripts/backfill.py --days 7 --reset
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from repindex.common.rpc import block_number
from repindex.ingest import pipeline, schema

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "repindex.db"

BLOCKS_PER_DAY = 43_200
WINDOW_BLOCKS = 2_000  # ~200,000 logs in memory at a time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=float, default=7.0)
    parser.add_argument("--window", type=int, default=WINDOW_BLOCKS)
    parser.add_argument("--reset", action="store_true", help="delete the database first")
    args = parser.parse_args()

    if args.reset and DB_PATH.exists():
        DB_PATH.unlink()
        print(f"removed {DB_PATH.relative_to(REPO_ROOT)}")

    schema.initialize(DB_PATH)
    conn = schema.connect(DB_PATH)

    try:
        head = block_number()
        end = head - 60  # honour global.finality_lag_blocks
        start = end - int(args.days * BLOCKS_PER_DAY) + 1

        row = conn.execute("SELECT last_block FROM ingest_checkpoints WHERE id = 1").fetchone()
        checkpoint = int(row["last_block"]) if row else -1
        if checkpoint >= start:
            start = checkpoint + 1
            print(f"resuming from checkpoint at block {checkpoint:,}")

        total_windows = max(1, (end - start + 1 + args.window - 1) // args.window)
        print(
            f"backfilling {args.days:g} days: blocks {start:,}..{end:,} "
            f"({end - start + 1:,} blocks, {total_windows} windows of {args.window:,})"
        )

        done = 0
        totals = 0
        began = time.time()

        cursor = start
        while cursor <= end:
            window_end = min(cursor + args.window - 1, end)
            try:
                result = pipeline.ingest_window(conn, cursor, window_end)
                totals += result.settlements
                done += 1
            except Exception as exc:  # noqa: BLE001 - report and stop, do not skip silently
                print(f"\nFAILED at blocks {cursor:,}-{window_end:,}: {type(exc).__name__}: {exc}")
                print("progress is checkpointed; re-run to resume from this window")
                return 1

            if done % 10 == 0 or window_end == end:
                elapsed = time.time() - began
                rate = (cursor - start + 1) / elapsed if elapsed else 0
                remaining = (end - cursor) / rate if rate else 0
                print(
                    f"  {done}/{total_windows} windows  "
                    f"{totals:,} settlements  "
                    f"{rate:,.0f} blocks/s  "
                    f"eta {remaining / 60:,.0f} min",
                    flush=True,
                )
            cursor = window_end + 1

        print(f"\ndone: {totals:,} settlements in {(time.time() - began) / 60:.0f} min")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
