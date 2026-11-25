"""A real paying agent that asks Zapproof before it pays an x402 resource.

This is the proof-of-work loop the product exists for:

1. Request a paid resource with no payment attached.
2. Read the real 402 challenge (x402 v2 header or v1 body).
3. Choose the Base USDC entry, if the seller offers one.
4. Ask Zapproof about the seller's `payTo` address.
5. Apply a small, explicit policy and write a decision record.
6. Only with `--pay`, an owner-funded key and `--confirm-pay`, sign one
   EIP-3009 authorization and retry the same request once.

The default is a dry run. It never signs, never sends a payment header and never
moves funds. `--self-test-sign` proves the signing path offline with a throwaway
key; it does not touch the network.

Run:
    uv run python scripts/preflight_payer.py \
      --url https://energy.halowerk.com/demo/paid-sample --max-usdc 0.01
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

import httpx

BASE_NETWORK = "eip155:8453"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ZAPPROOF_DEFAULT = "https://zapproof-hc75zyauja-uc.a.run.app"
SCORE_PAY = 70.0
UNKNOWN_CAP_USDC = 1.0
DEFAULT_KEY_FILE = Path.home() / ".zapproof" / "preflight-wallet.key"

EIP3009_TYPES = {
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ]
}


@dataclass
class Decision:
    action: str  # "pay" | "pay_capped" | "stop"
    reason: str
    amount_usdc: float
    cap_usdc: float | None


@dataclass
class RunRecord:
    ts: str
    url: str
    method: str
    http_status: int
    pay_to: str | None
    network: str | None
    amount_atomic: str | None
    amount_usdc: float | None
    resource: str | None
    zapproof_status: str
    zapproof_found: bool | None
    zapproof_score: float | None
    zapproof_evidence: dict[str, Any] | None
    action: str
    reason: str
    cap_usdc: float | None
    paid: bool
    payment_tx: str | None


def _b64_json(value: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(base64.b64decode(value, validate=True))
    except Exception:
        return None
    return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else None


def parse_challenge(response: httpx.Response) -> dict[str, Any] | None:
    """Return the x402 challenge from a 402, v2 header first, then v1 body."""
    header = response.headers.get("payment-required") or response.headers.get("PAYMENT-REQUIRED")
    if header:
        decoded = _b64_json(header)
        if decoded and "accepts" in decoded:
            decoded["_version"] = decoded.get("x402Version", 2)
            return decoded
    try:
        body = response.json()
    except Exception:
        return None
    if isinstance(body, dict) and "accepts" in body:
        body["_version"] = body.get("x402Version", 1)
        return body
    return None


def pick_base_accept(challenge: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [
        a
        for a in challenge.get("accepts", [])
        if a.get("network") == BASE_NETWORK
        and str(a.get("asset", "")).lower() == BASE_USDC.lower()
        and a.get("scheme") in {"exact", "upto"}
    ]
    if not candidates:
        return None
    return cast(dict[str, Any], min(candidates, key=lambda a: int(a.get("amount", "0"))))


def ask_zapproof(client: httpx.Client, base_url: str, address: str) -> dict[str, Any]:
    try:
        response = client.get(f"{base_url.rstrip('/')}/v1/score/{address}", timeout=20.0)
    except httpx.HTTPError as exc:
        return {"error": "index_unavailable", "detail": type(exc).__name__}
    if response.status_code != 200:
        return {"error": "index_unavailable", "http_status": response.status_code}
    try:
        answer = response.json()
    except Exception:
        return {"error": "index_unavailable", "detail": "non_json"}
    return answer if isinstance(answer, dict) else {"error": "index_unavailable"}


def decide(answer: dict[str, Any], amount_usdc: float, max_usdc: float) -> Decision:
    """The policy. Unknown is not bad; unknown is also not a licence to pay any amount."""
    if "error" in answer:
        return Decision("stop", "Zapproof could not answer; do not pay blind.", amount_usdc, None)

    status = answer.get("status")
    if answer.get("rail") == "lightning" or status == "rail_not_indexed":
        return Decision("stop", "No evidence is indexed for this rail.", amount_usdc, None)

    if amount_usdc > max_usdc:
        return Decision(
            "stop",
            f"Price {amount_usdc:.6f} USDC is above the per-call cap {max_usdc:.6f} USDC.",
            amount_usdc,
            max_usdc,
        )

    if answer.get("found") and status == "scored":
        score = answer.get("score")
        if isinstance(score, (int, float)) and score >= SCORE_PAY:
            return Decision(
                "pay",
                f"Scored {score:.2f} on observed settlements; one input, not an approval.",
                amount_usdc,
                amount_usdc,
            )
        return Decision(
            "stop",
            f"Score {score!r} is below the pay threshold {SCORE_PAY:.0f}.",
            amount_usdc,
            None,
        )

    cap = min(amount_usdc, UNKNOWN_CAP_USDC, max_usdc)
    return Decision(
        "pay_capped",
        "Too little evidence to score: cap the exposure and require a human for more.",
        amount_usdc,
        cap,
    )


def _sign_authorization(
    private_key: str,
    accept: dict[str, Any],
    *,
    now: int | None = None,
) -> dict[str, Any]:
    from eth_account import Account

    account = Account.from_key(private_key)
    issued = int(time.time()) if now is None else now
    valid_after = issued - 60
    valid_before = issued + int(accept.get("maxTimeoutSeconds", 300))
    authorization = {
        "from": account.address,
        "to": accept["payTo"],
        "value": str(accept["amount"]),
        "validAfter": str(valid_after),
        "validBefore": str(valid_before),
        "nonce": "0x" + secrets.token_hex(32),
    }
    offered = accept.get("extra", {}).get("eip712Domain") or {}
    domain = {
        "name": offered.get("name") or "USD Coin",
        "version": str(offered.get("version") or "2"),
        "chainId": int(offered.get("chainId") or 8453),
        "verifyingContract": offered.get("verifyingContract") or BASE_USDC,
    }
    message = {
        "from": authorization["from"],
        "to": authorization["to"],
        "value": int(authorization["value"]),
        "validAfter": int(authorization["validAfter"]),
        "validBefore": int(authorization["validBefore"]),
        "nonce": bytes.fromhex(authorization["nonce"][2:]),
    }
    signed = Account.sign_typed_data(
        private_key,
        domain_data=domain,
        message_types=EIP3009_TYPES,
        message_data=message,
    )
    signature_hex = signed.signature.hex()
    if not signature_hex.startswith("0x"):
        signature_hex = "0x" + signature_hex
    return {"authorization": authorization, "signature": signature_hex}


def build_payment_payload(
    challenge: dict[str, Any], accept: dict[str, Any], signed: dict[str, Any]
) -> str:
    payload = {
        "x402Version": challenge.get("_version", 2),
        "resource": challenge.get("resource", {}),
        "accepted": {k: v for k, v in accept.items()},
        "payload": signed,
        "extensions": challenge.get("extensions", {}),
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def run_self_test() -> int:
    """Prove signing works with a throwaway key; no network, no funds."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    key = "0x" + secrets.token_hex(32)
    accept = {
        "scheme": "exact",
        "network": BASE_NETWORK,
        "amount": "1000",
        "asset": BASE_USDC,
        "payTo": "0x2880EdfFF13100677Bf97A3CBdF3Bc34771C4E5E",
        "maxTimeoutSeconds": 300,
        "extra": {"name": "USD Coin", "version": "2"},
    }
    signed = _sign_authorization(key, accept)
    authorization = signed["authorization"]
    domain = {
        "name": "USD Coin",
        "version": "2",
        "chainId": 8453,
        "verifyingContract": BASE_USDC,
    }
    message = {
        "from": authorization["from"],
        "to": authorization["to"],
        "value": int(authorization["value"]),
        "validAfter": int(authorization["validAfter"]),
        "validBefore": int(authorization["validBefore"]),
        "nonce": bytes.fromhex(authorization["nonce"][2:]),
    }
    recovered = Account.recover_message(
        encode_typed_data(domain_data=domain, message_types=EIP3009_TYPES, message_data=message),
        signature=signed["signature"],
    )
    ok = recovered.lower() == authorization["from"].lower()
    print(json.dumps({"self_test": "pass" if ok else "fail", "recovered": recovered}))
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=False, help="Paid resource to request")
    parser.add_argument("--method", default="GET")
    parser.add_argument("--body", default=None, help="JSON body for POST resources")
    parser.add_argument("--max-usdc", type=float, default=0.01)
    parser.add_argument("--zapproof-url", default=ZAPPROOF_DEFAULT)
    parser.add_argument("--out", default="reports/preflight_runs.jsonl")
    parser.add_argument("--label", default="")
    parser.add_argument("--pay", action="store_true", help="Sign and pay if the policy says pay")
    parser.add_argument(
        "--confirm-pay",
        action="store_true",
        help="Required with --pay; confirms the owner approved spending this key's funds",
    )
    parser.add_argument(
        "--key-file",
        default=str(DEFAULT_KEY_FILE),
        help="Payer key file used by --pay (never printed); PREFLIGHT_PRIVATE_KEY wins",
    )
    parser.add_argument("--self-test-sign", action="store_true")
    args = parser.parse_args()

    if args.self_test_sign:
        return run_self_test()
    if not args.url:
        parser.error("--url is required unless --self-test-sign is used")

    private_key = os.environ.get("PREFLIGHT_PRIVATE_KEY", "").strip()
    if not private_key:
        key_path = Path(args.key_file).expanduser()
        if key_path.exists():
            private_key = key_path.read_text(encoding="utf-8").strip()
    if args.pay and not (private_key and args.confirm_pay):
        parser.error(
            "--pay needs a payer key (PREFLIGHT_PRIVATE_KEY or --key-file) "
            "and --confirm-pay (owner approval)"
        )

    headers = {"Accept": "application/json", "User-Agent": "zapproof-preflight-agent/0.1"}
    body = json.loads(args.body) if args.body else None
    if body is not None:
        headers["Content-Type"] = "application/json"

    with httpx.Client(follow_redirects=False, timeout=30.0) as http:
        response = http.request(args.method, args.url, headers=headers, json=body)
        challenge = parse_challenge(response)
        if challenge is None:
            record = RunRecord(
                ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                url=args.url,
                method=args.method,
                http_status=response.status_code,
                pay_to=None,
                network=None,
                amount_atomic=None,
                amount_usdc=None,
                resource=None,
                zapproof_status="not_asked",
                zapproof_found=None,
                zapproof_score=None,
                zapproof_evidence=None,
                action="stop",
                reason="The resource did not return an x402 challenge.",
                cap_usdc=None,
                paid=False,
                payment_tx=None,
            )
            _emit(record, args.out)
            return 0

        accept = pick_base_accept(challenge)
        if accept is None:
            record = RunRecord(
                ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                url=args.url,
                method=args.method,
                http_status=response.status_code,
                pay_to=None,
                network=None,
                amount_atomic=None,
                amount_usdc=None,
                resource=(challenge.get("resource") or {}).get("url"),
                zapproof_status="not_asked",
                zapproof_found=None,
                zapproof_score=None,
                zapproof_evidence=None,
                action="stop",
                reason="No Base USDC entry in the challenge.",
                cap_usdc=None,
                paid=False,
                payment_tx=None,
            )
            _emit(record, args.out)
            return 0

        amount_usdc = int(accept["amount"]) / 1_000_000
        answer = ask_zapproof(http, args.zapproof_url, accept["payTo"])
        decision = decide(answer, amount_usdc, args.max_usdc)

        paid = False
        payment_tx: str | None = None
        if args.pay and decision.action in {"pay", "pay_capped"}:
            cap = decision.cap_usdc or amount_usdc
            if amount_usdc <= cap:
                signed = _sign_authorization(private_key, accept)
                payment_header = build_payment_payload(challenge, accept, signed)
                paid_headers = dict(headers)
                paid_headers["PAYMENT-SIGNATURE"] = payment_header
                paid_response = http.request(
                    args.method, args.url, headers=paid_headers, json=body
                )
                paid = paid_response.status_code == 200
                payment_tx = paid_response.headers.get("payment-response")
                if not paid:
                    detail = ""
                    try:
                        error_body = paid_response.json()
                    except Exception:
                        error_body = None
                    if isinstance(error_body, dict) and error_body.get("error"):
                        detail = f" ({error_body['error']})"
                    decision = Decision(
                        "stop",
                        f"Payment retry returned {paid_response.status_code}{detail}; "
                        "not retried again.",
                        amount_usdc,
                        decision.cap_usdc,
                    )

        record = RunRecord(
            ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            url=args.url,
            method=args.method,
            http_status=response.status_code,
            pay_to=accept["payTo"],
            network=accept["network"],
            amount_atomic=str(accept["amount"]),
            amount_usdc=amount_usdc,
            resource=(challenge.get("resource") or {}).get("url"),
            zapproof_status=str(
                answer.get("status") or ("error" if "error" in answer else "unknown")
            ),
            zapproof_found=answer.get("found"),
            zapproof_score=answer.get("score"),
            zapproof_evidence=_evidence(answer),
            action=decision.action,
            reason=decision.reason,
            cap_usdc=decision.cap_usdc,
            paid=paid,
            payment_tx=payment_tx,
        )
        _emit(record, args.out)
        return 0


def _evidence(answer: dict[str, Any]) -> dict[str, Any] | None:
    evidence = answer.get("evidence") or {}
    interpretation = answer.get("interpretation") or {}
    out: dict[str, Any] = {}
    for key in ("settlements", "counterparties", "active_days", "as_of_block", "window"):
        if key in evidence:
            out[key] = evidence[key]
    for key in ("signaling_likelihood", "methodology_version"):
        if key in interpretation:
            out[key] = interpretation[key]
    if "confidence_band" in answer:
        out["confidence_band"] = answer["confidence_band"]
    return out or None


def _emit(record: RunRecord, out_path: str) -> None:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                "url": record.url,
                "payTo": record.pay_to,
                "amount_usdc": record.amount_usdc,
                "zapproof": {
                    "status": record.zapproof_status,
                    "found": record.zapproof_found,
                    "score": record.zapproof_score,
                },
                "action": record.action,
                "reason": record.reason,
                "paid": record.paid,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
