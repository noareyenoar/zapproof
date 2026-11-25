"""The preflight agent's policy and challenge parsing, without the network."""

from __future__ import annotations

import base64
import importlib.util
import json
import secrets
import sys
from pathlib import Path
from types import ModuleType

import httpx

REPO = Path(__file__).resolve().parent.parent
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "preflight_payer", REPO / "scripts" / "preflight_payer.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _accept(amount: str = "1000", network: str = "eip155:8453") -> dict[str, object]:
    return {
        "scheme": "exact",
        "network": network,
        "amount": amount,
        "asset": BASE_USDC,
        "payTo": "0x2880EdfFF13100677Bf97A3CBdF3Bc34771C4E5E",
        "maxTimeoutSeconds": 300,
        "extra": {"name": "USD Coin", "version": "2"},
    }


def test_scored_high_pays() -> None:
    module = _script()
    answer = {"found": True, "status": "scored", "score": 88.89}
    decision = module.decide(answer, 0.001, 0.01)
    assert decision.action == "pay"
    assert decision.cap_usdc == 0.001


def test_scored_low_stops() -> None:
    module = _script()
    answer = {"found": True, "status": "scored", "score": 40.0}
    assert module.decide(answer, 0.001, 0.01).action == "stop"


def test_insufficient_evidence_caps_not_zero() -> None:
    module = _script()
    answer = {"found": True, "status": "insufficient_evidence", "score": None}
    decision = module.decide(answer, 0.5, 1.0)
    assert decision.action == "pay_capped"
    assert decision.cap_usdc == 0.5


def test_unknown_cap_is_bounded() -> None:
    module = _script()
    answer = {"found": False, "status": "not_found", "score": None}
    decision = module.decide(answer, 1.5, 2.0)
    assert decision.action == "pay_capped"
    assert decision.cap_usdc == 1.0


def test_index_error_stops() -> None:
    module = _script()
    assert module.decide({"error": "index_unavailable"}, 0.001, 0.01).action == "stop"


def test_lightning_stops() -> None:
    module = _script()
    answer = {"status": "rail_not_indexed", "rail": "lightning"}
    assert module.decide(answer, 0.001, 0.01).action == "stop"


def test_price_above_cap_stops_even_when_scored() -> None:
    module = _script()
    answer = {"found": True, "status": "scored", "score": 99.0}
    decision = module.decide(answer, 0.02, 0.01)
    assert decision.action == "stop"
    assert "above" in decision.reason


def test_parse_v2_header() -> None:
    module = _script()
    challenge = {"x402Version": 2, "accepts": [_accept()], "resource": {"url": "https://x.test"}}
    encoded = base64.b64encode(json.dumps(challenge).encode()).decode()
    response = httpx.Response(402, headers={"PAYMENT-REQUIRED": encoded})
    parsed = module.parse_challenge(response)
    assert parsed is not None
    assert parsed["_version"] == 2
    assert parsed["accepts"][0]["payTo"] == "0x2880EdfFF13100677Bf97A3CBdF3Bc34771C4E5E"


def test_parse_v1_body() -> None:
    module = _script()
    response = httpx.Response(402, json={"x402Version": 1, "accepts": [_accept()]})
    parsed = module.parse_challenge(response)
    assert parsed is not None
    assert parsed["_version"] == 1


def test_parse_returns_none_without_accepts() -> None:
    module = _script()
    assert module.parse_challenge(httpx.Response(200, json={"ok": True})) is None


def test_pick_base_accept_chooses_cheapest_base_usdc() -> None:
    module = _script()
    challenge = {
        "accepts": [
            _accept("5000"),
            _accept("1000"),
            _accept("1000", network="eip155:137"),
        ]
    }
    chosen = module.pick_base_accept(challenge)
    assert chosen is not None
    assert chosen["amount"] == "1000"
    assert chosen["network"] == "eip155:8453"


def test_pick_base_accept_none_when_only_other_networks() -> None:
    module = _script()
    assert module.pick_base_accept({"accepts": [_accept(network="eip155:137")]}) is None


def test_signing_recovers_the_payer() -> None:
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    module = _script()
    key = "0x" + secrets.token_hex(32)
    signed = module._sign_authorization(key, _accept())
    authorization = signed["authorization"]
    assert signed["signature"].startswith("0x")
    assert len(signed["signature"]) == 132
    domain = {"name": "USD Coin", "version": "2", "chainId": 8453, "verifyingContract": BASE_USDC}
    message = {
        "from": authorization["from"],
        "to": authorization["to"],
        "value": int(authorization["value"]),
        "validAfter": int(authorization["validAfter"]),
        "validBefore": int(authorization["validBefore"]),
        "nonce": bytes.fromhex(authorization["nonce"][2:]),
    }
    recovered = Account.recover_message(
        encode_typed_data(
            domain_data=domain,
            message_types=module.EIP3009_TYPES,
            message_data=message,
        ),
        signature=signed["signature"],
    )
    assert recovered.lower() == authorization["from"].lower()


def test_signing_uses_the_domain_offered_by_the_seller() -> None:
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    module = _script()
    key = "0x" + secrets.token_hex(32)
    accept = _accept()
    accept["extra"] = {
        "eip712Domain": {
            "name": "USD Coin",
            "version": "2",
            "chainId": 8453,
            "verifyingContract": BASE_USDC,
        }
    }
    signed = module._sign_authorization(key, accept)
    authorization = signed["authorization"]
    recovered = Account.recover_message(
        encode_typed_data(
            domain_data=accept["extra"]["eip712Domain"],
            message_types=module.EIP3009_TYPES,
            message_data={
                "from": authorization["from"],
                "to": authorization["to"],
                "value": int(authorization["value"]),
                "validAfter": int(authorization["validAfter"]),
                "validBefore": int(authorization["validBefore"]),
                "nonce": bytes.fromhex(authorization["nonce"][2:]),
            },
        ),
        signature=signed["signature"],
    )
    assert recovered.lower() == authorization["from"].lower()


def test_payment_payload_round_trips() -> None:
    module = _script()
    challenge = {"x402Version": 2, "accepts": [_accept()], "resource": {"url": "https://x.test"}}
    signed = {"signature": "0xdead", "authorization": {"from": "0x1"}}
    encoded = module.build_payment_payload(challenge, _accept(), signed)
    payload = json.loads(base64.b64decode(encoded))
    assert payload["x402Version"] == 2
    assert payload["accepted"]["payTo"] == "0x2880EdfFF13100677Bf97A3CBdF3Bc34771C4E5E"
    assert payload["payload"]["signature"] == "0xdead"
