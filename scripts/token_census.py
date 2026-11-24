"""Token and mechanism census (Phase 0 task 0.5).

Answers two questions the gate needs:

  1. Which tokens actually appear in x402 activity, and do we decode them?
  2. Which settlement mechanisms are in use, and which can we not decode?

Two independent inputs, deliberately kept separate:

  - **Advertised** — the CDP discovery catalogue, which lists what services say
    they accept. Free, keyless, HTTP GET.
  - **Observed** — `AuthorizationUsed` events emitted on chain, scanned without an
    address filter so that a token nobody advertises still shows up.

The gap between advertised and observed is the interesting part. A token that is
advertised but never observed is a claim nobody exercised; a token observed but
never advertised is a coverage hole that would silently bias every number later.

Writes `reports/token_census.json`, whose shape is what gate check G0.4 reads.

Usage:
    uv run python scripts/token_census.py --blocks 400
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from repindex.common.rpc import block_number, get_logs, rpc_call
from repindex.ingest.decode import AUTHORIZATION_USED_TOPIC

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "reports" / "token_census.json"

DISCOVERY_URL = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"
BASE_NETWORK = "eip155:8453"

# Function selectors, read from the contract's own ABI behaviour rather than
# guessed. `decimals()` and `symbol()` are standard ERC-20 read methods.
SELECTOR_SYMBOL = "0x95d89b41"
SELECTOR_DECIMALS = "0x313ce567"


def decode_string(raw: str) -> str:
    """ABI-encoded string return value -> text."""
    if len(raw) < 130:
        return ""
    body = raw[2:]
    length = int(body[64:128], 16)
    return bytes.fromhex(body[128 : 128 + length * 2]).decode("utf-8", errors="replace")


def decode_uint(raw: str) -> int | None:
    if len(raw) < 66:
        return None
    return int(raw[2:66], 16)


def fetch_discovery() -> dict[str, Any]:
    response = httpx.get(DISCOVERY_URL, timeout=60)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def advertised_assets(items: list[dict[str, Any]], network: str) -> dict[str, dict[str, Any]]:
    """asset address (lowercased) -> advertised facts, for one network."""
    found: dict[str, dict[str, Any]] = {}
    for item in items:
        for accept in item.get("accepts") or []:
            if accept.get("network") != network:
                continue
            asset = (accept.get("asset") or "").lower()
            if not asset:
                continue
            entry = found.setdefault(asset, {"entries": 0, "schemes": Counter(), "resources": []})
            entry["entries"] += 1
            entry["schemes"][accept.get("scheme", "?")] += 1
            if len(entry["resources"]) < 3:
                entry["resources"].append(item.get("resource"))
    return found


def observed_authorization_tokens(
    from_block: int, to_block: int
) -> tuple[Counter[str], dict[str, str], list[str]]:
    """Count AuthorizationUsed by emitting contract, scanning WITHOUT an address filter.

    No address filter is the point: filtering to USDC would make 'only USDC uses
    this mechanism' true by construction rather than by measurement.
    The first transaction hash seen per contract is kept as a sample, and every
    distinct transaction hash is returned so the mechanism census can sample them.
    """
    counts: Counter[str] = Counter()
    samples: dict[str, str] = {}
    tx_hashes: set[str] = set()
    logs = get_logs(
        address=None,
        topics=[AUTHORIZATION_USED_TOPIC],
        from_block=from_block,
        to_block=to_block,
    )
    for log in logs:
        address = (log.get("address") or "").lower()
        counts[address] += 1
        tx = log.get("transactionHash")
        if isinstance(tx, str):
            samples.setdefault(address, tx)
            tx_hashes.add(tx)
    return counts, samples, sorted(tx_hashes)


def resolve_selector(selector: str) -> str | None:
    """Look a 4-byte selector up in 4byte.directory. None means unresolved."""
    try:
        response = httpx.get(
            f"https://www.4byte.directory/api/v1/signatures/?hex_signature={selector}",
            timeout=25,
        )
        results = response.json().get("results") or []
    except (httpx.HTTPError, ValueError):
        return None
    if not results:
        return None
    return str(results[0].get("text_signature"))


def mechanism_census(tx_hashes: list[str], sample_size: int) -> dict[str, Any]:
    """How the settlement transactions were actually constructed.

    An earlier version of this census did not measure this at all: it scanned a
    single event topic and then wrote `mechanism` from the fact that the scan found
    the log. That makes the unresolved-mechanism share structurally zero and unable
    to move, which independent review flagged. Any mechanism that does not emit
    `AuthorizationUsed` was invisible to it, yet gate check G0.4's cap is about
    tokens OR mechanisms.

    Sampling is used because resolving a selector costs one HTTP call per distinct
    selector, and the census window holds hundreds of thousands of transactions.
    The sample size is recorded so the number can be read for what it is.
    """
    import random

    rng = random.Random(20261017)  # fixed seed: the sample is reproducible
    sample = rng.sample(tx_hashes, min(sample_size, len(tx_hashes)))

    counts: Counter[str] = Counter()
    for tx_hash in sample:
        try:
            tx = rpc_call("eth_getTransactionByHash", [tx_hash])
        except Exception:  # noqa: BLE001 - counted as unresolved below, not swallowed
            counts["(fetch failed)"] += 1
            continue
        if not isinstance(tx, dict):
            counts["(fetch failed)"] += 1
            continue
        counts[str(tx.get("input", "0x"))[:10]] += 1

    resolved: dict[str, str | None] = {sel: resolve_selector(sel) for sel in counts}
    unresolved_count = sum(n for sel, n in counts.items() if resolved.get(sel) is None)

    return {
        "sampled_transactions": len(sample),
        "available_transactions": len(tx_hashes),
        "sample_seed": 20261017,
        "selectors": {
            sel: {"count": n, "signature": resolved.get(sel)} for sel, n in counts.most_common()
        },
        "unresolved_selector_count": unresolved_count,
        "unresolved_mechanism_share": round(unresolved_count / len(sample), 6) if sample else None,
    }


PERMIT2_PROXY = "0x402085c248eea27d92e8b30b2c58ed07f9e20001"


def mechanism_path_census(from_block: int, to_block: int, eip3009_count: int) -> dict[str, Any]:
    """Which settlement PATHS exist, as distinct from which wrappers carry them.

    Two different questions were being conflated:

    - **Mechanism** — how the token moved: an EIP-3009 authorization, a Permit2
      proxy settlement, or something else. This is what prompt task 0.5 names.
    - **Wrapper** — how the transaction was constructed: a direct call, a multicall,
      an ERC-4337 user operation, a custom router. Unnamed wrappers are not an
      unresolved mechanism; they are an unnamed envelope around a mechanism we can
      already read, because `AuthorizationUsed` and `Transfer` still fire.

    Measuring only the wrapper made 3% of transactions look like an unresolved
    mechanism when every one of them was a decodable EIP-3009 settlement. This
    function adds the missing half: it looks at the Permit2 proxy, which is the one
    other settlement path known to exist on Base, so the census is capable of
    finding a second mechanism rather than only counting what it already decodes.
    """
    permit2_logs = get_logs(
        address=PERMIT2_PROXY,
        topics=None,
        from_block=from_block,
        to_block=to_block,
    )
    topics = Counter(str(log["topics"][0]) for log in permit2_logs if log.get("topics"))

    # These events are NOT decoded: their topics are not in 4byte.directory and they
    # carry no AuthorizationUsed. An earlier version of this function returned
    # `unresolved_mechanism_share: 0.0` as a literal, which meant the check could
    # never fail — exactly the circularity independent review had already flagged.
    # An event we cannot classify is unresolved, and is counted as such here.
    unattributed = len(permit2_logs)
    denominator = eip3009_count + unattributed

    samples: list[str] = []
    for log in permit2_logs:
        tx = log.get("transactionHash")
        if isinstance(tx, str) and tx not in samples:
            samples.append(tx)
        if len(samples) >= 10:
            break

    return {
        "eip3009_authorization_used": eip3009_count,
        "permit2_proxy_events": unattributed,
        "permit2_event_topics": dict(topics.most_common()),
        "permit2_sample_tx_hashes": samples,
        "unattributed_events": unattributed,
        "unresolved_mechanism_share": round(unattributed / denominator, 6) if denominator else None,
        "note": (
            "The census scans two settlement paths: EIP-3009 via AuthorizationUsed, and the "
            "Permit2 proxy. Events on the proxy cannot be decoded — their topics are not in "
            "4byte.directory and they emit no AuthorizationUsed — so they are counted as "
            "unresolved rather than assumed to be nothing. What remains invisible to any "
            "census of this shape is a mechanism that emits neither of those events; that "
            "blind spot cannot be measured from logs, only acknowledged."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks", type=int, default=400, help="window size for the on-chain scan")
    parser.add_argument(
        "--mechanism-sample",
        type=int,
        default=300,
        help="transactions to sample when measuring how settlements were constructed",
    )
    args = parser.parse_args()

    head = block_number()
    to_block = head - 60
    from_block = to_block - args.blocks + 1

    print(f"fetching discovery catalogue from {DISCOVERY_URL}")
    discovery = fetch_discovery()
    items = discovery.get("items") or []
    print(f"  {len(items)} advertised services")

    advertised = advertised_assets(items, BASE_NETWORK)
    print(f"  {len(advertised)} distinct asset(s) advertised on Base")

    print(f"scanning blocks {from_block:,}..{to_block:,} for AuthorizationUsed, no address filter")
    observed, samples, settlement_txs = observed_authorization_tokens(from_block, to_block)
    print(f"  {sum(observed.values()):,} events from {len(observed)} contract(s)")

    print(f"wrapper census over a sample of {min(args.mechanism_sample, len(settlement_txs))} txs")
    wrappers = mechanism_census(settlement_txs, args.mechanism_sample)
    print(
        f"  {wrappers['unresolved_selector_count']} of {wrappers['sampled_transactions']} "
        f"transactions use a wrapper whose selector is not in 4byte.directory "
        f"(informational: the settlement inside is still decodable)"
    )

    print("mechanism path census (EIP-3009 vs the Permit2 proxy)")
    paths = mechanism_path_census(from_block, to_block, sum(observed.values()))
    print(
        f"  {paths['eip3009_authorization_used']:,} EIP-3009 settlements; "
        f"{paths['permit2_proxy_events']:,} Permit2 proxy events"
    )

    all_addresses = sorted(set(advertised) | set(observed))

    tokens: list[dict[str, Any]] = []
    for address in all_addresses:
        entry: dict[str, Any] = {
            "address": address,
            "settlement_count": int(observed.get(address, 0)),
            "advertised_entries": advertised.get(address, {}).get("entries", 0),
            "sample_tx_hashes": [samples[address]] if address in samples else [],
        }

        # Bytecode presence is asked ONCE, before anything that can raise. An earlier
        # version called eth_getCode inside the same try as symbol()/decimals() and
        # defaulted has_code to False in the except branch — so a contract whose
        # symbol() reverts was recorded as having no code. That is self-refuting: an
        # address with no code cannot emit the logs it was found by.
        code = rpc_call("eth_getCode", [address, "latest"])
        has_code = isinstance(code, str) and len(code) > 2
        entry["has_code"] = has_code
        entry["code_bytes"] = (len(code) // 2 - 1) if has_code and isinstance(code, str) else 0

        # Resolve symbol/decimals on chain. A token we cannot read is unresolved —
        # and the gate counts it against us, which is the correct direction.
        try:
            symbol = decode_string(
                rpc_call("eth_call", [{"to": address, "data": SELECTOR_SYMBOL}, "latest"])
            )
            decimals = decode_uint(
                rpc_call("eth_call", [{"to": address, "data": SELECTOR_DECIMALS}, "latest"])
            )
            entry["symbol"] = symbol or None
            entry["decimals"] = decimals
            entry["resolved"] = bool(symbol and decimals is not None and has_code)
        except Exception as exc:  # noqa: BLE001 - recorded as unresolved, not swallowed
            entry["symbol"] = None
            entry["decimals"] = None
            entry["resolved"] = False
            entry["metadata_error"] = f"{type(exc).__name__}: {exc}"
            # The standard ERC-20 view interface did not answer. That is exactly the
            # "cannot decode" case prompt task 0.5 asks to be listed rather than hidden.

        if address in advertised:
            schemes = advertised[address]["schemes"]
            entry["advertised_schemes"] = dict(schemes)
            entry["advertised_resources"] = advertised[address]["resources"]

        tokens.append(entry)

    total_settlements = sum(t["settlement_count"] for t in tokens) or 1
    unresolved = [t for t in tokens if not t["resolved"]]
    unresolved_events = sum(t["settlement_count"] for t in unresolved)

    token_share = round(unresolved_events / total_settlements, 6)
    mechanism_share = paths["unresolved_mechanism_share"]

    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "window": {"from_block": from_block, "to_block": to_block, "blocks": args.blocks},
        "discovery_source": DISCOVERY_URL,
        "base_network": BASE_NETWORK,
        "advertised_service_count": len(items),
        "observed_contract_count": len(observed),
        "total_settlements_in_window": sum(observed.values()),
        "tokens": tokens,
        "mechanism_paths": paths,
        "wrapper_census": wrappers,
        "summary": {
            "tokens_total": len(tokens),
            "tokens_resolved": len(tokens) - len(unresolved),
            "unresolved_token_count": len(unresolved),
            "unresolved_settlement_share": token_share,
            "unresolved_mechanism_share": mechanism_share,
            # G0.4's cap is about "an unresolved token OR mechanism", so the number
            # the gate must judge is the larger of the two, not the token share alone.
            "worst_unresolved_share": max(
                token_share, mechanism_share if mechanism_share is not None else 0.0
            ),
        },
        "advertised_vs_observed": {
            "advertised_only": sorted(set(advertised) - set(observed)),
            "observed_only": sorted(set(observed) - set(advertised)),
            "both": sorted(set(advertised) & set(observed)),
        },
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print()
    for token in tokens:
        flag = "resolved" if token["resolved"] else "UNRESOLVED"
        print(
            f"  {token['symbol'] or '?':<8} {token['address']}  "
            f"observed={token['settlement_count']:<6} "
            f"advertised={token['advertised_entries']:<4} {flag}"
        )
    print()
    print(f"unresolved settlement share: {payload['summary']['unresolved_settlement_share']:.4%}")
    adv_only = payload["advertised_vs_observed"]["advertised_only"] or "none"
    obs_only = payload["advertised_vs_observed"]["observed_only"] or "none"
    print(f"advertised but never observed: {adv_only}")
    print(f"observed but never advertised: {obs_only}")
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
