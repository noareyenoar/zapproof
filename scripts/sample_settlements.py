"""Sample real x402 settlements from Base, decoded two ways, and write the evidence.

Phase 0 task 0.4 requires at least `protocol_evidence_txs_min` (20) real
transactions decoded from chain — never typed from memory — with every transaction
hash recorded. This produces that evidence.

Two independent decode paths are run over the same settlements and compared:

  A. **Events** — pair `AuthorizationUsed` with the `Transfer` that follows it.
  B. **Calldata** — read `transferWithAuthorization`'s (from, to, value, nonce).

They must agree on payer, seller and amount for every settlement. Where they do
not, the record is reported rather than dropped.

Cost notes measured 2026-10-16, which shaped the design:

  - `AuthorizationUsed` alone is cheap: ~0.69 MB and ~1.1 s for 400 blocks.
  - Asking for `AuthorizationUsed` OR `Transfer` in one query exceeds the
    endpoint's 20,000-result cap at 400 blocks, because USDC emits roughly 59
    transfers per block against ~3 settlements.
  - `eth_getBlockByNumber(..., True)` costs ~874 ms per block, so blocks are
    fetched only for blocks that actually contain a settlement.
  - JSON-RPC batching is rejected with HTTP 429 by the public endpoint, so it is
    not a way around any of this.

Usage:
    uv run python scripts/sample_settlements.py --blocks 120
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repindex.common.rpc import block_number, get_logs, rpc_call
from repindex.ingest.decode import (
    AUTHORIZATION_USED_TOPIC,
    TRANSFER_TOPIC,
    USDC_BASE,
    Settlement,
    decode_from_calldata,
    decode_settlements,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "sample_settlements.json"

# Keep a combined AuthorizationUsed|Transfer query under the 20,000-result cap.
MAX_BLOCKS_PER_OR_QUERY = 120


def fetch_blocks_with_transactions(block_numbers: list[int]) -> dict[str, dict[str, Any]]:
    """tx hash -> transaction object, from whole blocks fetched with full transactions."""
    by_hash: dict[str, dict[str, Any]] = {}
    for number in block_numbers:
        block = rpc_call("eth_getBlockByNumber", [hex(number), True])
        if not isinstance(block, dict):
            continue
        for tx in block.get("transactions") or []:
            by_hash[tx["hash"]] = tx
    return by_hash


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks", type=int, default=120, help="window size in blocks")
    args = parser.parse_args()

    head = block_number()
    to_block = head - 60  # honour global.finality_lag_blocks
    from_block = to_block - args.blocks + 1
    print(f"head {head:,} | window {from_block:,}..{to_block:,} ({args.blocks} blocks)")

    # --- path A inputs: AuthorizationUsed events, cheap ---
    auth_logs = get_logs(
        address=USDC_BASE,
        topics=[AUTHORIZATION_USED_TOPIC],
        from_block=from_block,
        to_block=to_block,
    )
    print(f"{len(auth_logs):,} AuthorizationUsed events")
    if not auth_logs:
        print("no settlements in this window")
        return 1

    block_numbers = sorted({int(log["blockNumber"], 16) for log in auth_logs})
    print(f"fetching {len(block_numbers):,} blocks with full transactions (~874 ms each)")

    txs = fetch_blocks_with_transactions(block_numbers)
    senders = {h: t["from"] for h, t in txs.items()}

    # --- primary path: calldata, over the whole window ---
    by_calldata: dict[str, Settlement] = {}
    for tx_hash in sorted({log["transactionHash"] for log in auth_logs}):
        tx = txs.get(tx_hash)
        if tx is None:
            continue
        decoded = decode_from_calldata(tx)
        if decoded is not None:
            by_calldata[tx_hash] = decoded

    settlements = sorted(by_calldata.values(), key=lambda s: (s.block_number, s.tx_hash))
    print(f"calldata-decoded {len(settlements):,} of {len(auth_logs):,} events")

    # --- cross-check path: events, over a slice small enough to fetch both log
    # kinds under the endpoint's 20,000-result cap ---
    slice_to = min(from_block + MAX_BLOCKS_PER_OR_QUERY - 1, to_block)
    both_logs = get_logs(
        address=USDC_BASE,
        topics=[[AUTHORIZATION_USED_TOPIC, TRANSFER_TOPIC]],
        from_block=from_block,
        to_block=slice_to,
    )
    slice_auth = sum(1 for log in both_logs if log["topics"][0] == AUTHORIZATION_USED_TOPIC)
    slice_transfer = sum(1 for log in both_logs if log["topics"][0] == TRANSFER_TOPIC)
    print(f"slice {from_block:,}..{slice_to:,}: {slice_auth} auth, {slice_transfer} transfer logs")

    by_events = decode_settlements(both_logs, senders)
    print(f"event-paired {len(by_events):,} settlements in the slice")

    disagreements: list[dict[str, Any]] = []
    agreed = 0
    for settlement in by_events:
        other = by_calldata.get(settlement.tx_hash)
        if other is None:
            disagreements.append({"tx_hash": settlement.tx_hash, "reason": "no calldata decode"})
            continue
        diffs = {
            field: (getattr(settlement, field), getattr(other, field))
            for field in ("payer", "seller", "raw_amount", "nonce", "facilitator_relayer")
            if getattr(settlement, field) != getattr(other, field)
        }
        if diffs:
            disagreements.append({"tx_hash": settlement.tx_hash, "fields": diffs})
        else:
            agreed += 1

    print(f"cross-check: {agreed}/{len(by_events)} agree between calldata and events")
    if disagreements:
        print(f"  {len(disagreements)} disagreement(s) — recorded, not dropped")

    one_hour_blocks = 1_800  # 3,600 s / 2.000 s per block

    payload = {
        "scanned_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "head_block": head,
        "from_block": from_block,
        "to_block": to_block,
        "usdc": USDC_BASE,
        "event_count": len(auth_logs),
        "settlement_count": len(settlements),
        "cross_check": {
            "slice_from_block": from_block,
            "slice_to_block": slice_to,
            "event_paired_settlements": len(by_events),
            "agreed": agreed,
            "disagreements": disagreements[:20],
        },
        "total_raw_amount": sum(s.raw_amount for s in settlements),
        "unique_relayers": len({s.facilitator_relayer for s in settlements}),
        "unique_payers": len({s.payer for s in settlements}),
        "unique_sellers": len({s.seller for s in settlements}),
        "log_mix_in_slice": {
            "blocks": slice_to - from_block + 1,
            "authorization_used": slice_auth,
            "transfer": slice_transfer,
        },
        "derived_rates": {
            "blocks_per_hour": one_hour_blocks,
            "settlements_per_hour_estimate": round(
                len(settlements) * one_hour_blocks / args.blocks, 1
            ),
            "note": "estimated from this window; not a claim about a specific hour",
        },
        "evidence_transactions": [
            {
                "tx_hash": s.tx_hash,
                "block_number": s.block_number,
                "payer": s.payer,
                "seller": s.seller,
                "facilitator_relayer": s.facilitator_relayer,
                "raw_amount": s.raw_amount,
                "amount_usdc": s.amount_usdc,
                "nonce": s.nonce,
            }
            for s in settlements[:40]
        ],
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print()
    print(f"settlements decoded           : {len(settlements):,}")
    print(
        f"unique relayers/payers/sellers: {payload['unique_relayers']} / "
        f"{payload['unique_payers']} / {payload['unique_sellers']}"
    )
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
