"""Contract tests for the published OpenAPI schema (prompt task B1, gate GB.1).

These check the *contract*, not the data: every documented path answers, every
response carries the fields the schema promises, and no endpoint mutates anything.
schemathesis-style schema fuzzing is covered separately by the identifier fuzz sweep
in scripts/verify_api.py; this file asserts the shape.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from repindex.api.app import app

DISCLAIMER_MARKER = "not independently validated"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def schema() -> dict:
    return app.openapi()


def test_every_documented_v1_path_answers(client: TestClient, schema: dict) -> None:
    conn = sqlite3.connect("file:data/repindex.db?mode=ro", uri=True)
    try:
        seller = conn.execute(
            "SELECT seller FROM features ORDER BY settlement_count DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()

    substitutions = {"identifier": seller, "group_id": "1"}

    for path in schema["paths"]:
        if not path.startswith("/v1/"):
            continue
        concrete = path
        for name, value in substitutions.items():
            concrete = concrete.replace("{" + name + "}", value)
        response = client.get(concrete)
        assert response.status_code < 500, f"{concrete} returned {response.status_code}"


def test_only_get_methods_are_documented(schema: dict) -> None:
    """The API is read-only. A documented POST would be a write endpoint by definition."""
    for path, operations in schema["paths"].items():
        for method in operations:
            if method in ("parameters", "summary", "description"):
                continue
            assert method.lower() == "get", f"{method.upper()} {path} is not read-only"


def test_score_response_documents_every_required_field(schema: dict) -> None:
    required = {
        "found",
        "identifier",
        "status",
        "confidence_band",
        "evidence",
        "interpretation",
        "methodology_version",
        "disclaimer",
    }
    components = schema.get("components", {}).get("schemas", {})
    score_schema = components.get("ScoreResponse", {})
    documented = set(score_schema.get("properties", {}))

    assert required <= documented, f"schema is missing {required - documented}"


def test_a_score_response_matches_the_documented_shape(client: TestClient) -> None:
    conn = sqlite3.connect("file:data/repindex.db?mode=ro", uri=True)
    try:
        seller = conn.execute(
            "SELECT seller FROM features ORDER BY settlement_count DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()

    body = client.get(f"/v1/score/{seller}").json()

    for key in ("found", "identifier", "status", "confidence_band", "disclaimer"):
        assert key in body
    assert set(body["evidence"]) >= {
        "settlements", "counterparties", "active_days", "window", "as_of_block"
    }
    assert set(body["interpretation"]) >= {
        "signaling_likelihood", "rules", "methodology_version"
    }


def test_validation_errors_are_422_and_carry_no_stack_trace(client: TestClient) -> None:
    for bad in ("not-an-address", "0x", "0x" + "z" * 40):
        response = client.get(f"/v1/score/{bad}")
        assert response.status_code == 422
        assert "Traceback" not in response.text


def test_agent_card_is_not_served_until_enabled(client: TestClient) -> None:
    """The static card points at a placeholder and cannot answer message/send."""
    response = client.get("/.well-known/agent-card.json")
    assert response.status_code == 404


def test_agent_card_is_served_when_explicitly_enabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZAPPROOF_A2A_CARD", "1")
    response = client.get("/.well-known/agent-card.json")
    assert response.status_code == 200
    card = response.json()
    assert card["name"] == "Zapproof"
    assert card["skills"]
