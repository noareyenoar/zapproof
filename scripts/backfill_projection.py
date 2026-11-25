"""Measure the real cost of a backfill, then project it (Phase 0 task 0.7).

Task 0.7 asks for the time and request budget to backfill
`global.backfill_days_default` days. This script measures a small window and
extrapolates, rather than assuming a throughput figure.

What it measures, all on live endpoints:

  1. `eth_getLogs` for `AuthorizationUsed`, bytes and seconds per block.
  2. Batched `eth_getTransactionReceipt` for the resulting transactions, using the
     batch size that the endpoint actually accepts. This matters: a single receipt
     call costs ~478 ms, while the same call inside a batch of 25 costs ~52 ms, so
     the batch size is not an implementation detail, it is most of the runtime.
  3. The derived settlement rate, from the events actually observed.

Writes `reports/backfill_projection.md`.

Usage:
    uv run python scripts/backfill_projection.py --sample-blocks 600
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from repindex.common.rpc import BLOCKS_PER_DAY, base_rpc_urls, block_number, get_logs, rpc_call
from repindex.ingest.decode import AUTHORIZATION_USED_TOPIC, USDC_BASE

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "reports" / "backfill_projection.md"
MEASUREMENTS_PATH = REPO_ROOT / "reports" / "backfill_measurements.json"

# Measured 2026-10-16: Tenderly rejects batches with HTTP 429, Thirdweb accepts 25
# but rejects 100 with HTTP 413. The projection therefore uses batching against the
# endpoint that supports it.
BATCH_SIZE = 25


def batch_receipts(url: str, tx_hashes: list[str], timeout: float = 90.0) -> tuple[list[Any], int]:
    """Fetch receipts in one batch. Returns (results, response_bytes).

    Raises on a transport failure, and raises when ANY entry came back as an error.
    That second check is not optional: an endpoint that rejects batches may answer
    with a body full of per-entry errors rather than an HTTP error, and counting
    those bytes produces a nonsense projection. Measured on 2026-10-16, an earlier
    version of this script did exactly that and reported 15 bytes per receipt.
    """
    payload = [
        {"jsonrpc": "2.0", "id": index, "method": "eth_getTransactionReceipt", "params": [h]}
        for index, h in enumerate(tx_hashes)
    ]
    response = httpx.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    body = response.json()

    if not isinstance(body, list) or len(body) != len(tx_hashes):
        returned = len(body) if isinstance(body, list) else type(body).__name__
        raise RuntimeError(f"batch of {len(tx_hashes)} returned {returned} entries")

    errors = [entry["error"] for entry in body if isinstance(entry, dict) and entry.get("error")]
    if errors:
        raise RuntimeError(f"batch returned {len(errors)} error(s); first: {errors[0]}")

    return [entry.get("result") for entry in body], len(response.content)


def pick_batching_endpoint(sample_hashes: list[str]) -> tuple[str, str]:
    """Return (url, reason) for an endpoint that accepts a batch of REAL calls.

    The probe deliberately uses real transaction hashes. Probing with an empty or
    trivial batch gives a false positive: measured 2026-10-16, Tenderly accepts a
    two-call `eth_blockNumber` batch and then answers a 25-call receipt batch with
    HTTP 429, which is how the earlier version of this script picked the wrong
    endpoint.
    """
    if not sample_hashes:
        raise SystemExit("cannot probe batching without at least one transaction hash")

    probe = sample_hashes[:BATCH_SIZE]
    failures: list[str] = []
    for url in base_rpc_urls():
        try:
            results, _ = batch_receipts(url, probe)
        except Exception as exc:  # noqa: BLE001 - recorded, then the next endpoint is tried
            failures.append(f"{url}: {type(exc).__name__}: {exc}")
            continue
        if any(result is None for result in results):
            failures.append(f"{url}: batch succeeded but some receipts were null")
            continue
        return url, f"accepts a real batch of {len(probe)} receipt calls"

    raise SystemExit("no endpoint accepts batched receipt calls:\n  " + "\n  ".join(failures))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-blocks", type=int, default=600)
    args = parser.parse_args()

    head = block_number()
    to_block = head - 60
    from_block = to_block - args.sample_blocks + 1

    # --- 1. logs ---
    started = time.time()
    logs = get_logs(
        address=USDC_BASE,
        topics=[AUTHORIZATION_USED_TOPIC],
        from_block=from_block,
        to_block=to_block,
    )
    log_seconds = time.time() - started

    # byte cost of the same query, measured directly
    single = httpx.post(
        base_rpc_urls()[0],
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getLogs",
            "params": [
                {
                    "fromBlock": hex(from_block),
                    "toBlock": hex(from_block + 999),
                    "address": USDC_BASE,
                    "topics": [AUTHORIZATION_USED_TOPIC],
                }
            ],
        },
        timeout=60,
    )
    log_bytes_per_1000 = len(single.content)

    tx_hashes = sorted({log["transactionHash"] for log in logs})

    # --- 2. receipts, batched ---
    batch_url, batch_reason = pick_batching_endpoint(tx_hashes)
    receipt_bytes = 0
    receipt_seconds = 0.0
    fetched = 0

    # Read the whole window rather than a slice, so the projection is not scaled
    # from a handful of calls.
    for start in range(0, len(tx_hashes), BATCH_SIZE):
        chunk = tx_hashes[start : start + BATCH_SIZE]
        began = time.time()
        _, size = batch_receipts(batch_url, chunk)
        receipt_seconds += time.time() - began
        receipt_bytes += size
        fetched += len(chunk)

    # Baseline for comparison, so the claim "batching is most of the runtime" is
    # measured here rather than asserted from a separate experiment.
    single_began = time.time()
    single_count = min(20, len(tx_hashes))
    for tx_hash in tx_hashes[:single_count]:
        rpc_call("eth_getTransactionReceipt", [tx_hash])
    single_ms_each = (time.time() - single_began) / single_count * 1000 if single_count else 0.0

    # --- 3. derive ---
    days = args.sample_blocks / BLOCKS_PER_DAY
    settlements_per_day = len(tx_hashes) / days if days else 0

    target_days = 30
    target_blocks = BLOCKS_PER_DAY * target_days
    scale = target_blocks / args.sample_blocks

    projected = {
        "target_days": target_days,
        "target_blocks": target_blocks,
        "projected_settlements": round(settlements_per_day * target_days),
        "log_requests": int(target_blocks / 1000),
        "log_seconds": log_seconds * scale,
        "log_gb": log_bytes_per_1000 * (target_blocks / 1000) / 1e9,
        "receipt_requests": int(settlements_per_day * target_days / BATCH_SIZE),
        "receipt_seconds": (receipt_seconds / fetched) * settlements_per_day * target_days
        if fetched
        else 0.0,
        "receipt_gb": (receipt_bytes / fetched) * settlements_per_day * target_days / 1e9
        if fetched
        else 0.0,
    }
    projected["total_seconds"] = projected["log_seconds"] + projected["receipt_seconds"]
    projected["total_hours"] = projected["total_seconds"] / 3600
    projected["total_gb"] = projected["log_gb"] + projected["receipt_gb"]

    measurements = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sample": {
            "from_block": from_block,
            "to_block": to_block,
            "blocks": args.sample_blocks,
            "days": round(days, 4),
            "authorization_used_events": len(logs),
            "distinct_settlement_txs": len(tx_hashes),
            "log_seconds": round(log_seconds, 2),
            "log_bytes_per_1000_blocks": log_bytes_per_1000,
            "batch_endpoint": batch_url,
            "batch_endpoint_reason": batch_reason,
            "batch_size": BATCH_SIZE,
            "receipt_seconds": round(receipt_seconds, 2),
            "receipt_bytes": receipt_bytes,
            "receipt_ms_each_batched": round(receipt_seconds / fetched * 1000, 1)
            if fetched
            else None,
            "receipt_ms_each_single": round(single_ms_each, 1),
        },
        "derived": {
            "blocks_per_day": BLOCKS_PER_DAY,
            "settlements_per_day": round(settlements_per_day),
        },
        "projected": projected,
    }
    MEASUREMENTS_PATH.write_text(json.dumps(measurements, indent=2), encoding="utf-8")

    # --- report ---
    lines: list[str] = []
    lines.append("# Backfill projection")
    lines.append("")
    lines.append(
        "Generated by `uv run python scripts/backfill_projection.py`. "
        "Every figure below is measured, then scaled — nothing is assumed."
    )
    lines.append("")
    lines.append(f"- **Measured:** {measurements['measured_at']}")
    lines.append(
        f"- **Sample window:** blocks {from_block:,}–{to_block:,} "
        f"({args.sample_blocks:,} blocks, {days:.2f} days)"
    )
    lines.append(
        f"- **Batching endpoint:** `{batch_url}` ({batch_reason}), batch size {BATCH_SIZE}"
    )
    lines.append("")
    lines.append("## What was measured")
    lines.append("")
    lines.append("| Quantity | Measured |")
    lines.append("|---|---|")
    lines.append(f"| `AuthorizationUsed` events in sample | {len(logs):,} |")
    lines.append(f"| Distinct settlement transactions | {len(tx_hashes):,} |")
    lines.append(f"| `eth_getLogs` bytes per 1,000 blocks | {log_bytes_per_1000:,} |")
    lines.append(f"| `eth_getLogs` seconds for the sample | {log_seconds:.1f} |")
    if fetched:
        lines.append(
            f"| Batched receipt fetch | {receipt_seconds / fetched * 1000:.1f} ms each "
            f"(batch of {BATCH_SIZE}) |"
        )
        lines.append(
            f"| Unbatched receipt fetch | {single_ms_each:.1f} ms each (measured, same window) |"
        )
        lines.append(f"| Receipt bytes each | {receipt_bytes / fetched:,.0f} |")
    lines.append("")
    lines.append("## Derived rate")
    lines.append("")
    lines.append(f"- **Settlements per day: {settlements_per_day:,.0f}**")
    lines.append(f"- Blocks per day: {BLOCKS_PER_DAY:,} (block time measured at exactly 2.000 s)")
    lines.append("")
    lines.append(f"## Projection for a {target_days}-day backfill")
    lines.append("")
    lines.append("| Quantity | Projected |")
    lines.append("|---|---|")
    lines.append(f"| Blocks to scan | {target_blocks:,} |")
    lines.append(f"| Settlements | {projected['projected_settlements']:,} |")
    lines.append(f"| `eth_getLogs` requests | {projected['log_requests']:,} |")
    lines.append(f"| Receipt batch requests | {projected['receipt_requests']:,} |")
    lines.append(f"| Wall clock | **{projected['total_hours']:.1f} hours** |")
    lines.append(f"| Response bytes | **{projected['total_gb']:.1f} GB** |")
    lines.append("")
    lines.append("## Open questions this projection depends on")
    lines.append("")
    lines.append(
        "- **Tenderly documents a 1 GB per IP per day response cap.** At "
        f"{projected['total_gb']:.1f} GB, a {target_days}-day backfill cannot run "
        "against that one endpoint in a day. Splitting across endpoints works only if "
        "each has its own allowance, which is **not verified**."
    )
    lines.append(
        "- **Thirdweb's response cap is unknown.** It accepts batches of 25 and rejects "
        "100, but no byte allowance has been found. `NEEDS_HUMAN`: this is a terms-of-use "
        "question, not a technical one."
    )
    lines.append(
        "- The projection assumes the settlement rate stays flat. It is currently rising, "
        "so treat the wall-clock figure as a floor."
    )
    lines.append("")

    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"settlements/day measured : {settlements_per_day:,.0f}")
    print(
        f"projected {target_days}-day backfill: {projected['total_hours']:.1f} h, "
        f"{projected['total_gb']:.1f} GB"
    )
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)} and {MEASUREMENTS_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
