"""Ingestion pipeline (prompt Section 7, task A1).

Three properties the prompt asks for, and how each is obtained:

- **Idempotent.** `settlements` is keyed on `(tx_hash, log_index)`, so re-ingesting a
  window cannot duplicate a row. Verified by gate check GA.2.
- **Resumable.** The checkpoint is written in the same transaction as the rows it
  describes, so a crash leaves the two consistent — never rows without a checkpoint
  that would be re-fetched forever, never a checkpoint past rows that were not
  written.
- **Reorg-aware.** Every settlement stores its block hash. `find_reorg` compares the
  stored hashes of recent blocks against chain and reports the first block that
  disagrees, which `rollback_to` then removes.

Only blocks at or below `head - finality_lag_blocks` are ingested (prompt Section 5),
so a settlement is not recorded from a block that may still be reorganised.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from repindex.common.rpc import block_number, get_logs, rpc_call
from repindex.common.version import METHODOLOGY_VERSION
from repindex.ingest.decode import (
    AUTHORIZATION_USED_TOPIC,
    TRANSFER_TOPIC,
    USDC_BASE,
    Settlement,
    decode_settlements,
)

# A combined AuthorizationUsed|Transfer query hits the endpoint's 20,000-result cap
# quickly. Measured 2026-10-28: about 101 logs per block, so 150 blocks is ~15,000
# results — under the cap, and one request instead of the four a 1,000-block chunk
# would need after repeated halving. get_logs still halves a chunk that overflows,
# so this is a preference rather than a hard requirement.
INGEST_CHUNK_BLOCKS = 150


@dataclass(frozen=True, slots=True)
class WindowResult:
    from_block: int
    to_block: int
    settlements: int
    blocks_touched: int


# Base produces a block every 2.000000 seconds. Measured 2026-10-28: 302,400 blocks
# spanned exactly 604,800 seconds, and interpolating the timestamp from the block
# number had a maximum error of 0 seconds across 40 blocks sampled over seven days.
#
# This matters because a block header costs about 524 ms to fetch — 44 hours for a
# seven-day window — while a log already carries its block hash. Deriving the
# timestamp removes the single largest cost in the backfill.
#
# It is an interpolation, so it is checked against a real header at ingest time and
# the check fails loudly if the chain ever changes its cadence.
BLOCK_TIME_SECONDS = 2.0
TIMESTAMP_TOLERANCE_SECONDS = 60


def timestamp_anchor() -> tuple[int, int]:
    """(block number, timestamp) at the chain head, used as the interpolation origin."""
    head = block_number()
    block = rpc_call("eth_getBlockByNumber", [hex(head), False])
    if not isinstance(block, dict):
        raise RuntimeError("could not read the head block for the timestamp anchor")
    return head, int(block["timestamp"], 16)


def derive_timestamp(number: int, anchor: tuple[int, int]) -> int:
    return round(anchor[1] + (number - anchor[0]) * BLOCK_TIME_SECONDS)


def verify_timestamp_derivation(anchor: tuple[int, int], sample: list[int]) -> str | None:
    """Return a problem description if any sampled block disagrees with the anchor."""
    for number in sample:
        block = rpc_call("eth_getBlockByNumber", [hex(number), False])
        if not isinstance(block, dict):
            return f"could not read block {number} to verify the timestamp derivation"
        actual = int(block["timestamp"], 16)
        derived = derive_timestamp(number, anchor)
        if abs(actual - derived) > TIMESTAMP_TOLERANCE_SECONDS:
            return (
                f"block {number}: derived {derived}, chain says {actual} "
                f"(tolerance {TIMESTAMP_TOLERANCE_SECONDS}s). The 2.000 s/block "
                "assumption no longer holds; stop and re-derive."
            )
    return None


def fetch_settlements(
    from_block: int,
    to_block: int,
    *,
    resolve_relayers: bool = False,
) -> tuple[list[Settlement], dict[int, dict[str, Any]]]:
    """Decode every settlement in a block range.

    Block hash and timestamp are taken without an RPC call: the hash comes from the
    log itself and the timestamp is derived from the block number (see above).

    `resolve_relayers` is off by default. Turning it on costs one transaction fetch
    per settlement — measured 2026-10-28, about 50 settlements per block makes a
    seven-day window cost roughly 125 hours, so the relayer is left NULL and the
    `single_relayer` rule abstains rather than the backfill being impossible.
    """
    logs: list[dict[str, Any]] = []
    cursor = from_block
    while cursor <= to_block:
        chunk_end = min(cursor + INGEST_CHUNK_BLOCKS - 1, to_block)
        logs.extend(
            get_logs(
                address=USDC_BASE,
                topics=[[AUTHORIZATION_USED_TOPIC, TRANSFER_TOPIC]],
                from_block=cursor,
                to_block=chunk_end,
            )
        )
        cursor = chunk_end + 1

    if not logs:
        return [], {}

    anchor = timestamp_anchor()

    # Verify the interpolation at up to five blocks spread across the window, so a
    # change in chain cadence is caught by this run rather than silently baked in.
    numbers = sorted({int(log["blockNumber"], 16) for log in logs})
    step = max(1, len(numbers) // 5)
    problem = verify_timestamp_derivation(anchor, numbers[::step][:5])
    if problem:
        raise RuntimeError(problem)

    meta = {
        number: {
            "hash": next(
                (log.get("blockHash", "") for log in logs if int(log["blockNumber"], 16) == number),
                "",
            ),
            "timestamp": derive_timestamp(number, anchor),
        }
        for number in numbers
    }

    senders: dict[str, str] | None = None
    if resolve_relayers:
        senders = {}
        for number in numbers:
            block = rpc_call("eth_getBlockByNumber", [hex(number), True])
            if not isinstance(block, dict):
                continue
            for tx in block.get("transactions") or []:
                senders[tx["hash"]] = str(tx["from"]).lower()

    return decode_settlements(logs, senders), meta


def ingest_window(
    conn: sqlite3.Connection,
    from_block: int,
    to_block: int,
    *,
    methodology_version: str = METHODOLOGY_VERSION,
    resolve_relayers: bool = False,
) -> WindowResult:
    """Ingest one range, writing rows and the checkpoint in a single transaction."""
    settlements, meta = fetch_settlements(from_block, to_block, resolve_relayers=resolve_relayers)

    if not settlements:
        conn.execute(
            "UPDATE ingest_checkpoints SET last_block = ?, last_block_hash = ?, "
            "updated_at = datetime('now') WHERE id = 1",
            (to_block, ""),
        )
        conn.commit()
        return WindowResult(from_block, to_block, 0, 0)

    rows = [
        (
            s.tx_hash,
            s.log_index,
            s.block_number,
            meta.get(s.block_number, {}).get("hash", ""),
            meta.get(s.block_number, {}).get("timestamp", 0),
            s.token,
            s.decimals,
            s.raw_amount,
            s.amount_usdc,
            s.payer,
            s.seller,
            s.facilitator_relayer,
            s.nonce,
            s.mechanism,
        )
        for s in settlements
        if s.block_number in meta
    ]

    last_block = max(s.block_number for s in settlements)
    last_hash = meta.get(last_block, {}).get("hash", "")

    try:
        conn.executemany(
            "INSERT OR REPLACE INTO settlements ("
            "tx_hash, log_index, block_number, block_hash, block_time, token, decimals, "
            "raw_amount, normalized_amount, payer, seller, facilitator_relayer, nonce, mechanism"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.execute(
            "UPDATE ingest_checkpoints SET last_block = ?, last_block_hash = ?, "
            "updated_at = datetime('now') WHERE id = 1",
            (last_block, last_hash),
        )
        conn.execute(
            "INSERT INTO runs (kind, finished_at, status, detail) "
            "VALUES ('ingest', datetime('now'), 'ok', ?)",
            (f"blocks {from_block}-{to_block}, {len(rows)} settlements, {methodology_version}",),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise

    return WindowResult(from_block, to_block, len(rows), len(meta))


def ingest_to_finality(
    conn: sqlite3.Connection,
    lag_blocks: int,
    *,
    start_block: int | None = None,
    max_blocks: int | None = None,
) -> WindowResult:
    """Ingest from the stored checkpoint up to `head - lag_blocks`.

    `start_block` is required on a fresh database and ignored once a checkpoint
    exists. An earlier version defaulted a fresh start to the safe head, which would
    have ingested a single block and reported success — a quiet way to look like work
    had happened when none had.
    """
    head = block_number()
    safe_head = head - lag_blocks

    row = conn.execute("SELECT last_block FROM ingest_checkpoints WHERE id = 1").fetchone()
    checkpoint = int(row["last_block"]) if row else -1

    if checkpoint >= 0:
        start = checkpoint + 1
    elif start_block is not None:
        start = start_block
    else:
        raise ValueError(
            "fresh database with no checkpoint: pass start_block rather than defaulting, "
            "because there is no sensible default for how far back to begin"
        )

    if max_blocks is not None:
        start = max(start, safe_head - max_blocks + 1)

    if start > safe_head:
        return WindowResult(start, safe_head, 0, 0)

    # Check for a reorg before ingesting forward. Independent review found that
    # `find_reorg` and `rollback_to` existed but had no production caller — only the
    # tests called them, so "ingestion rolls back and recovers" was demonstrated in
    # isolation rather than in the path that actually runs. This is that path.
    diverged = find_reorg(conn, depth=lag_blocks)
    if diverged is not None:
        removed = rollback_to(conn, max(0, diverged - 1))
        checkpoint = conn.execute(
            "SELECT last_block FROM ingest_checkpoints WHERE id = 1"
        ).fetchone()[0]
        start = int(checkpoint) + 1
        print(
            f"reorg detected at block {diverged:,}: rolled back {removed} settlements, "
            f"resuming from {start:,}"
        )

    if start > safe_head:
        return WindowResult(start, safe_head, 0, 0)

    return ingest_window(conn, start, safe_head)


def find_reorg(conn: sqlite3.Connection, depth: int = 60) -> int | None:
    """Return the LOWEST recent block whose stored hash disagrees with chain.

    The lowest, not the highest: after a reorg every block from the fork onward has a
    different hash, and the fork point is what must be rolled back to. Returning the
    tip would leave the blocks between the fork and the tip in place, still carrying
    hashes that no longer exist. An earlier version did exactly that, and the test
    for it caught the difference between 1004 and 1005.

    `depth` bounds how far back the comparison looks. A reorg deeper than that is not
    detected here; prompt task A1 asks for the last N blocks, and N is configurable.
    """
    rows = conn.execute(
        "SELECT DISTINCT block_number, block_hash FROM settlements "
        "WHERE block_hash != '' ORDER BY block_number DESC LIMIT ?",
        (depth,),
    ).fetchall()

    disagreeing: list[int] = []
    for row in rows:
        number = int(row["block_number"])
        chain = rpc_call("eth_getBlockByNumber", [hex(number), False])
        if not isinstance(chain, dict):
            continue
        if chain.get("hash") != row["block_hash"]:
            disagreeing.append(number)

    return min(disagreeing) if disagreeing else None


def rollback_to(conn: sqlite3.Connection, block: int) -> int:
    """Delete settlements above `block` and rewind the checkpoint. Returns rows removed."""
    removed = conn.execute("DELETE FROM settlements WHERE block_number > ?", (block,)).rowcount
    conn.execute(
        "UPDATE ingest_checkpoints SET last_block = ?, last_block_hash = '', "
        "updated_at = datetime('now') WHERE id = 1",
        (block,),
    )
    conn.execute(
        "INSERT INTO runs (kind, finished_at, status, detail) "
        "VALUES ('rollback', datetime('now'), 'ok', ?)",
        (f"removed {removed} settlements above block {block}",),
    )
    conn.commit()
    return int(removed)
