"""Prompt task B1 / gate check GB.8: unknown is not zero.

The rule the whole project rests on: an identifier with no evidence returns
`found: false` and no number. Not zero, not a low score. A counterparty nobody has
paid is unknown, not bad.

This file is what GB.8 runs. It tests the API directly and the query layer beneath it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from repindex.api import queries
from repindex.api.app import app

# A well-formed address verified absent from `features` on 2026-10-30. The earlier
# pick, 0x...dead, turned out to have one settlement and the test failed correctly:
# "present but thin" is a different answer from "absent".
ABSENT = "0xffffffffffffffffffffffffffffffffffffffff"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def test_absent_address_returns_found_false(client: TestClient) -> None:
    response = client.get(f"/v1/score/{ABSENT}")

    assert response.status_code == 200
    body = response.json()
    assert body["found"] is False
    assert "score" not in body, "an unknown identifier must carry no score field at all"
    assert body["note"], "the response must say that unknown is not bad"


def test_absent_address_never_returns_zero(client: TestClient) -> None:
    """A zero would read as a verdict. There is nothing to judge."""
    body = client.get(f"/v1/score/{ABSENT}").json()

    assert body.get("score") != 0
    assert body.get("score") is None
    serialised = str(body)
    assert '"score": 0' not in serialised


def test_absent_address_response_carries_the_disclaimer(client: TestClient) -> None:
    body = client.get(f"/v1/score/{ABSENT}").json()

    assert "not independently validated" in body["disclaimer"]


def test_the_query_layer_returns_none_not_a_zero_row(tmp_path: Path) -> None:
    """The refusal happens below the API, so no future endpoint can bypass it."""
    conn = sqlite3.connect("file:data/repindex.db?mode=ro", uri=True)
    try:
        assert queries.fetch_score(conn, ABSENT) is None
    finally:
        conn.close()


def test_a_thin_but_present_seller_is_insufficient_evidence_not_zero(
    client: TestClient,
) -> None:
    """Present with almost no evidence is a different answer from absent."""
    conn = sqlite3.connect("file:data/repindex.db?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT seller FROM features ORDER BY settlement_count ASC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    body = client.get(f"/v1/score/{row[0]}").json()

    assert body["found"] is True
    assert body["status"] == "insufficient_evidence"
    assert body["score"] is None
    assert body["score"] != 0
