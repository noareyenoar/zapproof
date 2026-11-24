"""Walk the x402 Bazaar and ask Zapproof about every listed seller.

This is the marketing department's prospecting agent. It does not post, message or
submit anything. It produces the raw material a human sends: for each live seller,
what a paying agent sees today when it asks Zapproof before paying them.

The Bazaar is a shelf, not a chat room. The legitimate move is not to shout on the
shelf; it is to read the shelf, find sellers whose evidence is thin, and send each
one their own card with their own numbers.

Run:
    uv run python scripts/bazaar_prospects.py --limit 100
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

BAZAAR = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"
BASE_NETWORK = "eip155:8453"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ZAPPROOF_DEFAULT = "https://zapproof-hc75zyauja-uc.a.run.app"


def fetch_bazaar(limit: int) -> list[dict[str, Any]]:
    url = f"{BAZAAR}?limit={limit}"
    with urllib.request.urlopen(url, timeout=30) as response:
        payload = json.load(response)
    items = payload.get("items", [])
    return items if isinstance(items, list) else []


def base_accept(item: dict[str, Any]) -> dict[str, Any] | None:
    accepts = item.get("accepts") or []
    for accept in accepts:
        if (
            accept.get("network") == BASE_NETWORK
            and str(accept.get("asset", "")).lower() == BASE_USDC.lower()
        ):
            return cast(dict[str, Any], accept)
    return None


def score(pay_to: str, zapproof_url: str) -> dict[str, Any]:
    url = f"{zapproof_url.rstrip('/')}/v1/score/{pay_to}"
    try:
        with urllib.request.urlopen(url, timeout=25) as response:
            answer = json.load(response)
        return answer if isinstance(answer, dict) else {"error": "non_object"}
    except Exception as exc:  # noqa: BLE001 - the report records the failure
        return {"error": type(exc).__name__}


def collect(items: list[dict[str, Any]], zapproof_url: str) -> list[dict[str, Any]]:
    by_payee: dict[str, dict[str, Any]] = {}
    for item in items:
        accept = base_accept(item)
        if accept is None:
            continue
        pay_to = str(accept.get("payTo", ""))
        if not pay_to:
            continue
        amount = int(accept.get("amount", "0") or 0)
        current = by_payee.get(pay_to)
        if current is None or amount < current["amount_atomic"]:
            by_payee[pay_to] = {
                "pay_to": pay_to,
                "amount_atomic": amount,
                "amount_usdc": amount / 1_000_000,
                "resource": item.get("resource"),
                "description": item.get("description"),
                "service_name": (item.get("resource") or {}).get("serviceName")
                if isinstance(item.get("resource"), dict)
                else None,
            }

    def enrich(entry: dict[str, Any]) -> dict[str, Any]:
        answer = score(entry["pay_to"], zapproof_url)
        entry["status"] = answer.get("status") or ("error" if "error" in answer else "unknown")
        entry["found"] = answer.get("found")
        entry["score"] = answer.get("score")
        entry["confidence_band"] = answer.get("confidence_band")
        evidence = answer.get("evidence") or {}
        entry["settlements"] = evidence.get("settlements")
        entry["counterparties"] = evidence.get("counterparties")
        entry["active_days"] = evidence.get("active_days")
        return entry

    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(enrich, by_payee.values()))


def rank(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(entry: dict[str, Any]) -> tuple[int, int, int]:
        # Thin evidence first: those sellers have a concrete reason to care.
        thin = 1 if entry["status"] in {"insufficient_evidence", "unknown", "error"} else 0
        missing = 1 if entry["found"] is False else 0
        volume = int(entry.get("settlements") or 0)
        return (thin + missing, volume, -entry["amount_atomic"])

    return sorted(entries, key=key, reverse=True)


def render(entries: list[dict[str, Any]], items: int) -> str:
    scored = [e for e in entries if e["status"] == "scored"]
    thin = [e for e in entries if e["status"] == "insufficient_evidence"]
    missing = [e for e in entries if e["found"] is False]
    failed = [e for e in entries if e["status"] in {"error", "unknown"}]
    lines = [
        "# x402 Bazaar — what a paying agent sees",
        "",
        f"Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} from "
        f"{items} Bazaar entries, {len(entries)} unique Base payees.",
        "",
        "| Bucket | Sellers | Meaning |",
        "|---|---:|---|",
        f"| Scored | {len(scored)} | Zapproof has enough evidence to interpret. |",
        f"| Insufficient evidence | {len(thin)} | Some record, too thin to score. "
        f"Not bad — unknown. |",
        f"| Not found | {len(missing)} | No seller-side record indexed. Not bad — unknown. |",
        f"| Lookup failed | {len(failed)} | The index did not answer; rerun before using. |",
        "",
        "## Sellers with the thinnest evidence",
        "",
        "These are the honest outreach targets: they already take x402 payments, and a",
        "cautious payer currently sees no score for them. The card is their own data.",
        "",
        "| Payee | Service | Price | Status | Settlements | Payers |",
        "|---|---|---:|---|---:|---:|",
    ]
    for entry in entries:
        if entry["status"] not in {"insufficient_evidence", "unknown", "error"} and entry["found"]:
            continue
        service = (entry.get("description") or entry.get("service_name") or "")[:70]
        service = service.replace("|", "/")
        lines.append(
            f"| `{entry['pay_to']}` | {service} | {entry['amount_usdc']:.6f} | "
            f"{entry['status']} | {entry.get('settlements') or 0} | "
            f"{entry.get('counterparties') or 0} |"
        )
    lines += [
        "",
        "## Scored sellers, by settled volume",
        "",
        "| Payee | Service | Score | Settlements | Payers | Active days |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for entry in sorted(scored, key=lambda e: int(e.get("settlements") or 0), reverse=True):
        service = (entry.get("description") or entry.get("service_name") or "")[:60]
        service = service.replace("|", "/")
        lines.append(
            f"| `{entry['pay_to']}` | {service} | {entry['score']} | "
            f"{entry.get('settlements') or 0} | {entry.get('counterparties') or 0} | "
            f"{entry.get('active_days') or 0} |"
        )
    lines += [
        "",
        "## How to use this",
        "",
        "1. Pick one seller at a time; never bulk-send.",
        "2. Send the seller its own row, a link to `https://zapproof.web.app`, and one",
        "   sentence: a paying agent asks this before it pays you, and today it gets no",
        "   score. Offer the read-only lookup; do not sell a badge or a verdict.",
        "3. Log the reply. Three replies beat three hundred sends.",
        "",
        "The agent that produced this file does not send anything. A human sends.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--zapproof-url", default=ZAPPROOF_DEFAULT)
    parser.add_argument("--out", default="reports/bazaar_prospects.md")
    parser.add_argument("--json-out", default="reports/bazaar_prospects.json")
    args = parser.parse_args()

    items = fetch_bazaar(args.limit)
    entries = rank(collect(items, args.zapproof_url))
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(
        json.dumps({"generated_at": time.time(), "entries": entries}, indent=2),
        encoding="utf-8",
    )
    Path(args.out).write_text(render(entries, len(items)), encoding="utf-8")
    print(
        json.dumps(
            {
                "entries": len(entries),
                "scored": sum(1 for e in entries if e["status"] == "scored"),
                "insufficient": sum(1 for e in entries if e["status"] == "insufficient_evidence"),
                "missing": sum(1 for e in entries if e["found"] is False),
                "wrote": [args.out, args.json_out],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
