"""Sample x402 settlements on Solana from public data, at zero cost.

This is a measurement tool, not production ingestion. It produces evidence counts
from a time-boxed sample and says so in the output. Nothing here writes to the
index database, and nothing here is published automatically.

WHY A FEE-PAYER ALLOWLIST
    Solana has no on-chain marker that says "x402". The x402 exact scheme on SVM is
    a client-signed SPL `TransferChecked` submitted by a facilitator that pays the
    fee. So a transfer is counted as an x402 settlement only when its fee payer is a
    known x402 facilitator address. The list below comes from the public facilitator
    table on x402scan (fetched 2026-11-25); it can be stale, and every number this
    tool prints is therefore a lower bound for the listed facilitators only.

WHY BALANCE DELTAS, NOT INSTRUCTIONS
    `pre/postTokenBalances` accounts for Token-2022 transfer fees, address lookup
    tables and inner instructions. Diffing them is simpler and more robust than
    walking the instruction tree.

WHAT THIS DOES NOT PROVE
    That the payer is independent of the payee; that the service was delivered; or
    that the facilitator list is complete. It is a sample, labelled as one.

Usage:
    uv run python scripts/solana_sample.py doctor
    uv run python scripts/solana_sample.py sigs --days 7
    uv run python scripts/solana_sample.py txs --sample 400
    uv run python scripts/solana_sample.py report

Set HELIUS_API_KEY (or any HELIUS* variable, URL or key) in the environment or in
`.env` to raise throughput. The value is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# A Helius endpoint is used when one is configured, because the public endpoints
# throttle a 44k-transaction backfill to roughly one call per second. The key is
# read from the environment or `.env` at runtime and must never be printed, logged,
# or included in an error message: every failure below names the endpoint label only.
PUBLIC_ENDPOINTS = (
    {
        "url": "https://api.mainnet-beta.solana.com",
        "label": "solana-official",
        "batch": True,
        "gap": 1.1,
    },
    {
        "url": "https://solana-rpc.publicnode.com",
        "label": "publicnode",
        "batch": False,
        "gap": 1.1,
    },
)
_rpc_index = 0
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6

# x402scan's public facilitator table, 2026-11-25. Fee payers only.
FACILITATORS: dict[str, tuple[str, ...]] = {
    "coinbase": (
        "L54zkaPQFeTn1UsEqieEXBqWrPShiaZEPD7mS5WXfQg",
        "BENrLoUbndxoNMUS5JXApGMtNykLjFXXixMtpDwDR9SP",
        "BFK9TLC3edb13K6v4YyH3DwPb5DSUpkWvb7XnqCL9b4F",
        "D6ZhtNQ5nT9ZnTHUbqXZsTx5MH2rPFiBBggX4hY1WePM",
        "GVJJ7rdGiXr5xaYbRwRbjfaJL7fmwRygFi1H6aGqDveb",
        "Hc3sdEAsCGQcpgfivywog9uwtk8gUBUZgsxdME1EJy88",
        "92QcYJZpkwYyacR3G69QNR2JfjadQxd16cp5d7x5GEzU",
    ),
    "payAI": (
        "2wKupLR9q6wXYppw8Gr2NvWxKBUqm4PPJKkQfoxHDBg4",
        "CjNFTjvBhbJJd2B5ePPMHRLx1ELZpa8dwQgGL727eKww",
        "8B5UKhwfAyFW67h58cBkQj1Ur6QXRgwWJJcQp8ZBsDPa",
    ),
    "ultravioletadao": ("F742C4VfFLQ9zRQyithoj5229ZgtX2WqKCSFKgH2EThq",),
    "openfacilitator": ("Hbe1vdFs4EQVVAzcV12muHhr6DEKwrT9roMXGPLxLBLP",),
}
ALLOWED = {addr for addrs in FACILITATORS.values() for addr in addrs}

OUT = Path("tmp/solana")
SIGS = OUT / "signatures.jsonl"
TXS = OUT / "transactions.jsonl"
ALLOWLIST = OUT / "allowlist.json"
REPORT_JSON = Path("reports/solana_sample_2026-11-25.json")
REPORT_MD = Path("reports/solana_sample_2026-11-25.md")


def write_allowlist() -> None:
    """Record the classifier next to the data it classified, with its date."""
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched": "2026-11-25",
        "source": "x402scan public facilitator table",
        "facilitators": {name: list(addrs) for name, addrs in FACILITATORS.items()},
    }
    ALLOWLIST.write_text(json.dumps(payload, indent=1), encoding="utf-8")


class _HttpError(Exception):
    """Carries only a status code, so a URL containing a key never reaches a log."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _env_values() -> dict[str, str]:
    """Merge `.env` and the process environment. Values are never printed."""
    values: dict[str, str] = {}
    path = Path(".env")
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, raw = line.partition("=")
            values.setdefault(key.strip(), raw.strip().strip("\"'"))
    values.update(os.environ)
    return values


def _helius_url() -> str | None:
    """Find a Helius endpoint without revealing it. Any HELIUS* key accepts a URL or a key."""
    for key, value in _env_values().items():
        if "HELIUS" not in key.upper() or not value:
            continue
        if value.startswith("https://"):
            return value
        if value.startswith("http://"):
            continue
        return f"https://mainnet.helius-rpc.com/?api-key={value}"
    return None


def _endpoints() -> list[dict[str, Any]]:
    endpoints: list[dict[str, Any]] = []
    helius = _helius_url()
    if helius:
        endpoints.append({"url": helius, "label": "helius", "batch": True, "gap": 0.12})
    return endpoints + [dict(endpoint) for endpoint in PUBLIC_ENDPOINTS]


ENDPOINTS = _endpoints()


def _post(endpoint: dict[str, Any], body: str, timeout: int) -> Any:
    try:
        request = urllib.request.Request(
            endpoint["url"], data=body.encode(), headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _HttpError(exc.code) from None
    except Exception as exc:  # noqa: BLE001 - the URL must not escape in a traceback
        raise RuntimeError(f"request failed at {endpoint['label']}: {type(exc).__name__}") from None


def rpc(method: str, params: list[Any]) -> Any:
    global _rpc_index
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    for attempt in range(8):
        endpoint = ENDPOINTS[_rpc_index % len(ENDPOINTS)]
        try:
            payload = _post(endpoint, body, timeout=60)
        except _HttpError as exc:
            if exc.code != 429 or attempt == 7:
                raise RuntimeError(f"{method}: HTTP {exc.code} from {endpoint['label']}") from None
            _rpc_index += 1
            time.sleep(1.5 * (attempt + 1))
            continue
        finally:
            time.sleep(endpoint["gap"])
        if "error" in payload:
            raise RuntimeError(f"{method}: {payload['error']}")
        return payload["result"]
    raise RuntimeError("unreachable")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def collect_signatures(days: int, max_pages: int) -> None:
    """Page each facilitator back `days` days. Resumable per address via the JSONL."""
    write_allowlist()
    cutoff = int((datetime.now(UTC) - timedelta(days=days)).timestamp())
    seen = {(row["facilitator"], row["signature"]) for row in _read_jsonl(SIGS)}
    for facilitator, addresses in FACILITATORS.items():
        for address in addresses:
            before: str | None = None
            for _ in range(max_pages):
                params: list[Any] = [address, {"limit": 1000}]
                if before:
                    params[1]["before"] = before
                page = rpc("getSignaturesForAddress", params)
                if not page:
                    break
                fresh = [
                    {
                        "facilitator": facilitator,
                        "fee_payer": address,
                        "signature": item["signature"],
                        "slot": item["slot"],
                        "block_time": item.get("blockTime"),
                        "err": item.get("err"),
                    }
                    for item in page
                    if (facilitator, item["signature"]) not in seen
                ]
                _append_jsonl(SIGS, fresh)
                seen.update((row["facilitator"], row["signature"]) for row in fresh)
                before = page[-1]["signature"]
                oldest = page[-1].get("blockTime") or 0
                if oldest < cutoff:
                    break
            print(f"{facilitator} {address[:10]}… total signatures {len(seen)}", flush=True)


def _token_deltas(meta: dict[str, Any]) -> list[dict[str, Any]]:
    before = {entry["accountIndex"]: entry for entry in meta.get("preTokenBalances") or []}
    after = {entry["accountIndex"]: entry for entry in meta.get("postTokenBalances") or []}
    out: list[dict[str, Any]] = []
    for index in set(before) | set(after):
        entry = after.get(index) or before.get(index) or {}
        if entry.get("mint") != USDC_MINT:
            continue
        start = int(before[index]["uiTokenAmount"]["amount"]) if index in before else 0
        end = int(after[index]["uiTokenAmount"]["amount"]) if index in after else 0
        if end == start:
            continue
        out.append(
            {
                "account_index": index,
                "owner": entry.get("owner"),
                "delta": end - start,
            }
        )
    return out


# Measured 2026-11-25: Helius accepts batches of 15 at ~13 tx/s, throttles at 20,
# and rejects a batch of 50 outright. 10 is the safe size for an unattended run.
BATCH_SIZE = 10


def _batch_endpoint() -> dict[str, Any]:
    for endpoint in ENDPOINTS:
        if endpoint["batch"]:
            return endpoint
    raise RuntimeError("no batch-capable endpoint")


def rpc_batch(method: str, params_list: list[list[Any]]) -> list[Any]:
    """Batch calls, retrying the items the endpoint throttles individually.

    Measured 2026-11-25: the official endpoint applies a per-method limit and answers
    a batched item with `{"code": 429, "message": "Too many requests for a specific
    RPC call"}` rather than failing the HTTP request. Those items are retried; a
    `result: null` with no error means the transaction is genuinely absent and is
    dropped. publicnode rejects batch payloads, so only batch-capable endpoints are used.
    """
    endpoint = _batch_endpoint()
    results: list[Any] = [None] * len(params_list)
    pending = list(range(len(params_list)))
    for attempt in range(7):
        if not pending:
            break
        body = json.dumps(
            [
                {"jsonrpc": "2.0", "id": index, "method": method, "params": params_list[index]}
                for index in pending
            ]
        )
        try:
            items = _post(endpoint, body, timeout=90)
        except _HttpError as exc:
            if exc.code != 429 or attempt == 6:
                raise RuntimeError(f"{method}: HTTP {exc.code} from {endpoint['label']}") from None
            time.sleep(2.0 * (attempt + 1))
            continue
        throttled = []
        for item in items:
            index = item["id"]
            if item.get("error"):
                throttled.append(index)
            else:
                results[index] = item.get("result")
        pending = throttled
        if pending:
            time.sleep(1.5 * (attempt + 1))
    return results


def collect_transactions(sample_size: int, days: int) -> None:
    """Fetch a time-even sample of successful signatures, resumably.

    A low-activity address returns its whole history in one page, so the window is
    enforced here rather than trusting the paging loop's stop condition.
    """
    if not ALLOWLIST.is_file():
        write_allowlist()
    cutoff = int((datetime.now(UTC) - timedelta(days=days)).timestamp())
    signatures = [
        row
        for row in _read_jsonl(SIGS)
        if row.get("err") is None and (row.get("block_time") or 0) >= cutoff
    ]
    signatures.sort(key=lambda row: row["block_time"])
    done = {row["signature"] for row in _read_jsonl(TXS)}
    if len(signatures) <= sample_size:
        chosen = signatures
    else:
        step = (len(signatures) - 1) / (sample_size - 1)
        chosen = [signatures[round(i * step)] for i in range(sample_size)]
    chosen = [row for row in chosen if row["signature"] not in done]
    print(f"sampling {len(chosen)} of {len(signatures)} signatures", flush=True)
    for start in range(0, len(chosen), BATCH_SIZE):
        batch = chosen[start : start + BATCH_SIZE]
        results = rpc_batch(
            "getTransaction",
            [
                [row["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
                for row in batch
            ],
        )
        rows = []
        for row, tx in zip(batch, results, strict=True):
            if tx is None:
                continue
            meta = tx.get("meta") or {}
            keys = tx["transaction"]["message"]["accountKeys"]
            fee_payer = keys[0]["pubkey"] if isinstance(keys[0], dict) else keys[0]
            rows.append(
                {
                    "signature": row["signature"],
                    "slot": tx.get("slot"),
                    "block_time": tx.get("blockTime"),
                    "fee_payer": fee_payer,
                    "fee_payer_allowed": fee_payer in ALLOWED,
                    "err": meta.get("err"),
                    "deltas": _token_deltas(meta),
                }
            )
        _append_jsonl(TXS, rows)
        print(f"  {min(start + BATCH_SIZE, len(chosen))}/{len(chosen)}", flush=True)


def build_report() -> None:
    txs = _read_jsonl(TXS)
    sellers: dict[str, dict[str, Any]] = {}
    payers: dict[str, set[str]] = defaultdict(set)
    days: dict[str, set[str]] = defaultdict(set)
    amounts: dict[str, list[int]] = defaultdict(list)
    skipped = {"failed": 0, "fee_payer_not_allowed": 0, "no_usdc_transfer": 0}

    for tx in txs:
        if tx.get("err") is not None:
            skipped["failed"] += 1
            continue
        if not tx.get("fee_payer_allowed"):
            skipped["fee_payer_not_allowed"] += 1
            continue
        inflows = [d for d in tx["deltas"] if d["delta"] > 0 and d["owner"] not in ALLOWED]
        outflows = [d for d in tx["deltas"] if d["delta"] < 0]
        if not inflows or not outflows:
            skipped["no_usdc_transfer"] += 1
            continue
        payer = max(outflows, key=lambda d: abs(d["delta"]))["owner"]
        day = datetime.fromtimestamp(tx["block_time"], UTC).strftime("%Y-%m-%d")
        for inflow in inflows:
            seller = inflow["owner"]
            row = sellers.setdefault(
                seller,
                {
                    "settlements": 0,
                    "first_block_time": tx["block_time"],
                    "last_block_time": tx["block_time"],
                },
            )
            row["settlements"] += 1
            row["first_block_time"] = min(row["first_block_time"], tx["block_time"])
            row["last_block_time"] = max(row["last_block_time"], tx["block_time"])
            payers[seller].add(payer)
            days[seller].add(day)
            amounts[seller].append(inflow["delta"])

    rows = []
    for seller, row in sellers.items():
        values = sorted(amounts[seller])
        rows.append(
            {
                "seller": seller,
                "settlements": row["settlements"],
                "unique_payers": len(payers[seller]),
                "active_days": len(days[seller]),
                "amount_usdc_median": round(statistics.median(values) / 10**USDC_DECIMALS, 6),
                "amount_usdc_total": round(sum(values) / 10**USDC_DECIMALS, 6),
                "first_seen": datetime.fromtimestamp(row["first_block_time"], UTC).isoformat(),
                "last_seen": datetime.fromtimestamp(row["last_block_time"], UTC).isoformat(),
            }
        )
    rows.sort(key=lambda r: (-r["settlements"], r["seller"]))
    times = [tx["block_time"] for tx in txs if tx.get("block_time")]
    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "method": "fee-payer allowlist + USDC balance deltas, time-even sample",
        "rpc_label": ENDPOINTS[0]["label"],
        "sample": {
            "transactions_fetched": len(txs),
            "transactions_used": len(txs) - sum(skipped.values()),
            "skipped": skipped,
            "sellers": len(rows),
            "different_payers": len({p for s in payers.values() for p in s}),
            "window_start": (
                datetime.fromtimestamp(min(times), UTC).isoformat() if times else None
            ),
            "window_end": (datetime.fromtimestamp(max(times), UTC).isoformat() if times else None),
        },
        "caveats": [
            "A sample, not a census. Facilitator list from x402scan's public table, 2026-11-25.",
            "No on-chain x402 marker exists on Solana; the allowlist is the classifier.",
            "Payer independence and delivery are not proven.",
            "This is not a published score and is not used by the API.",
        ],
        "sellers": rows,
    }
    REPORT_JSON.write_text(json.dumps(summary, indent=1), encoding="utf-8")

    lines = [
        "# Solana x402 settlement sample — 2026-11-25",
        "",
        "A **sample**, not a census. See the caveats in the JSON.",
        "",
        f"- transactions fetched: {summary['sample']['transactions_fetched']}",
        f"- transactions used: {summary['sample']['transactions_used']}",
        f"- distinct sellers: {summary['sample']['sellers']}",
        f"- distinct payers: {summary['sample']['different_payers']}",
        f"- window: {summary['sample']['window_start']} → {summary['sample']['window_end']}",
        f"- skipped: {skipped}",
        "",
        "| seller | settlements | payers | active days | median USDC |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in rows[:30]:
        lines.append(
            f"| `{row['seller']}` | {row['settlements']} | {row['unique_payers']} | "
            f"{row['active_days']} | {row['amount_usdc_median']} |"
        )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary["sample"], indent=1), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sigs = sub.add_parser("sigs")
    sigs.add_argument("--days", type=int, default=7)
    sigs.add_argument("--max-pages", type=int, default=40)
    txs = sub.add_parser("txs")
    txs.add_argument("--sample", type=int, default=400)
    txs.add_argument("--days", type=int, default=7)
    sub.add_parser("report")
    sub.add_parser("doctor")
    args = parser.parse_args()
    if args.command == "doctor":
        for endpoint in ENDPOINTS:
            print(endpoint["label"], "batch" if endpoint["batch"] else "single", flush=True)
    elif args.command == "sigs":
        collect_signatures(args.days, args.max_pages)
    elif args.command == "txs":
        collect_transactions(args.sample, args.days)
    else:
        build_report()


if __name__ == "__main__":
    main()
