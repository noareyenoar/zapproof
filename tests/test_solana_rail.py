"""The Solana rail: identifier shape, separate evidence index, honest coverage.

These tests exist because the rail was added on 2026-11-25 and the failure modes are
specific to it: a Solana answer must never carry Base evidence, a deployment without the
Solana index must say `rail_not_indexed` rather than "unknown", and a base58-shaped
token that is not 32 bytes must not be treated as an address.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from scripts.build_solana_db import build

from repindex.api import app as app_module
from repindex.api.app import app
from repindex.api.identifiers import KIND_EVM, KIND_SOLANA, classify_identifier
from repindex.mcp import server

DISCLAIMER_MARKER = "not independently validated"

SELLER = "Bd2xS189uhhnxUeBeVF3JFXrg8zQgrvqZtpqh8hgkCEQ"
UNKNOWN = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ALLOWED_PAYER = "L54zkaPQFeTn1UsEqieEXBqWrPShiaZEPD7mS5WXfQg"
PAYERS = [
    "9MkM5Ra48KFcSv9HDQ8sb3C3r5HjouEwGQ6sp1cx1vde",
    "AQqnMFBwGZEoti85aTVRy8XYpKrho7GaMDx9ZB3CEeKA",
    "GmgbqY2AKRa2oEdtSxvDn48uz1ozmbM6EjhYs1MGz8PH",
    "HJHeNjURYf3Lwp5iTnfSt8ktn3oGGTV3A8atJHLn281K",
    "12Ec2cJmfR1C9uwejzxcuMhUgEC7wDrLgm1wBvvR5w9E",
    "4an2sqamWWhny9mjLsMtGXCDXakeNtg6vSLq4QvhdmQu",
]
BASE_TIME = 1_795_000_000


@pytest.fixture(scope="module")
def solana_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("solana")
    rows = []
    for index in range(24):
        payer = PAYERS[index % len(PAYERS)]
        rows.append(
            {
                "signature": f"sig{index:03d}",
                "slot": 1000 + index,
                "block_time": BASE_TIME + (index // 8) * 86_400 + (index % 8) * 3_600,
                "fee_payer": ALLOWED_PAYER,
                "fee_payer_allowed": True,
                "err": None,
                "facilitator": "coinbase",
                "deltas": [
                    {"account_index": 0, "owner": payer, "delta": -(1000 + index * 137)},
                    {"account_index": 2, "owner": SELLER, "delta": 1000 + index * 137},
                ],
            }
        )
    (root / "transactions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    (root / "allowlist.json").write_text(
        json.dumps(
            {
                "fetched": "2026-11-25",
                "source": "test fixture",
                "facilitators": {"coinbase": [ALLOWED_PAYER]},
            }
        ),
        encoding="utf-8",
    )
    output = root / "solana.db"
    build(root / "transactions.jsonl", output, {ALLOWED_PAYER}, "2026-11-25")
    return output


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, solana_db: Path) -> TestClient:
    monkeypatch.setenv("REPINDEX_SOLANA_DB", str(solana_db))
    app_module._rate_buckets.clear()
    return TestClient(app)


def test_a_base58_address_keeps_its_case() -> None:
    kind, normalised = classify_identifier(SELLER)

    assert kind == KIND_SOLANA
    assert normalised == SELLER


@pytest.mark.parametrize(
    "value", ["not-an-address", "0x" + "a" * 40, "1" * 31, "this is not base58 at all"]
)
def test_a_non_address_is_not_classified_as_solana(value: str) -> None:
    try:
        kind, _ = classify_identifier(value)
    except Exception:  # noqa: BLE001 - InvalidIdentifier is the expected path
        return
    assert kind != KIND_SOLANA


def test_the_api_scored_seller_carries_rail_and_coverage(client: TestClient) -> None:
    response = client.get(f"/v1/score/{SELLER}")

    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True
    assert body["rail"] == "solana"
    assert body["status"] == "scored"
    assert body["score"] is not None
    assert "Solana rail" in body["coverage"]
    assert "sample, not a census" in body["coverage"]
    assert body["evidence"]["settlements"] == 24
    assert body["evidence"]["counterparties"] == 6
    assert DISCLAIMER_MARKER in body["disclaimer"]


def test_an_unknown_solana_address_is_not_a_zero(client: TestClient) -> None:
    response = client.get(f"/v1/score/{UNKNOWN}")

    assert response.status_code == 200
    body = response.json()
    assert body["found"] is False
    assert body["rail"] == "solana"
    assert "score" not in body or body["score"] is None
    assert "Solana rail" in body["coverage"]
    assert DISCLAIMER_MARKER in body["disclaimer"]


def test_a_deployment_without_the_solana_index_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPINDEX_SOLANA_DB", "data/does-not-exist.db")
    response = TestClient(app).get(f"/v1/score/{SELLER}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rail_not_indexed"
    assert body["rail"] == "solana"
    assert body["score"] is None


def test_the_mcp_tool_answers_solana_from_the_local_index(
    monkeypatch: pytest.MonkeyPatch, solana_db: Path
) -> None:
    monkeypatch.delenv("ZAPPROOF_API_URL", raising=False)
    monkeypatch.setenv("REPINDEX_SOLANA_DB", str(solana_db))

    body = server.check_counterparty(SELLER)

    assert body["found"] is True
    assert body["evidence_rail"] == "solana"
    assert body["score"] is not None
    assert "Solana rail" in body["coverage"]


def test_the_mcp_tool_refuses_a_rail_mismatch(
    monkeypatch: pytest.MonkeyPatch, solana_db: Path
) -> None:
    monkeypatch.delenv("ZAPPROOF_API_URL", raising=False)
    monkeypatch.setenv("REPINDEX_SOLANA_DB", str(solana_db))

    body = server.check_counterparty(SELLER, rail="base")

    assert body["error"] == "rail_mismatch"
    assert body["identifier_rail"] == "solana"


def test_a_base_address_is_still_base() -> None:
    kind, normalised = classify_identifier("0x" + "A" * 40)

    assert kind == KIND_EVM
    assert normalised == "0x" + "a" * 40
