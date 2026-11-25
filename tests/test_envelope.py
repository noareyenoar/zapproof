"""The self-describing envelope: schema_version, evidence_hash, permalink, no endorsement.

An agent that stores an answer and re-fetches it later must be able to tell whether the
evidence changed. That is the whole purpose of `evidence_hash`, and it constrains the
design: the hash must be over the record, not over the host that served it, and must
never contain itself. The signed-receipt alternative was researched on 2026-11-25 and
rejected for lack of a consumer; see `reports/meetings/2026-11-25/06-lookup-receipt-research.md`.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from repindex.api import app as app_module
from repindex.api.app import app
from repindex.api.models import SCHEMA_VERSION
from repindex.common.canonical import canonical_json, evidence_hash
from repindex.mcp.server import check_counterparty

DISCLAIMER_MARKER = "not independently validated"


def _db_one(sql: str) -> tuple:
    conn = sqlite3.connect("file:data/repindex.db?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchone()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_rate_limit() -> None:
    app_module._rate_buckets.clear()
    yield
    app_module._rate_buckets.clear()


def _seller() -> str:
    row = _db_one("SELECT seller FROM features ORDER BY settlement_count DESC LIMIT 1")
    assert row is not None
    return str(row[0])


UNKNOWN = "0x" + "cd" * 20
LIGHTNING = "02" + "ab" * 32


def test_canonical_json_is_independent_of_key_order() -> None:
    assert canonical_json({"b": 2, "a": 1}) == canonical_json({"a": 1, "b": 2})
    assert evidence_hash({"b": 2, "a": 1}) == evidence_hash({"a": 1, "b": 2})


def test_the_hash_excludes_itself_and_the_host_dependent_fields() -> None:
    body = {"identifier": "0xabc", "score": 50.0, "evidence_hash": "sha256:old"}
    without = {k: v for k, v in body.items() if k != "evidence_hash"}
    assert evidence_hash(body) == evidence_hash(without)
    assert evidence_hash(body) == evidence_hash({**body, "permalink": "http://a/"})
    assert evidence_hash(body) == evidence_hash({**body, "disclaimer": "other words"})


@pytest.mark.parametrize(
    "path",
    [
        "/v1/score/{seller}",
        "/v1/score/0x" + "cd" * 20,
        "/v1/score/" + "02" + "ab" * 32,
    ],
)
def test_every_score_response_is_sealed(client: TestClient, path: str) -> None:
    path = path.replace("{seller}", _seller())
    body = client.get(path).json()

    assert body["schema_version"] == SCHEMA_VERSION
    assert body["not_an_endorsement"] is True
    assert body["evidence_hash"].startswith("sha256:")
    assert body["permalink"] == f"http://testserver/v1/score/{path.rsplit('/', 1)[1]}"
    assert body["evidence_hash"] == evidence_hash(body)


def test_the_hash_is_host_independent_but_the_permalink_is_not(
    client: TestClient,
) -> None:
    seller = _seller()
    one = client.get(f"/v1/score/{seller}", headers={"host": "one.test"})
    two = client.get(f"/v1/score/{seller}", headers={"host": "two.example"})

    assert one.json()["permalink"].startswith("http://one.test/")
    assert two.json()["permalink"].startswith("http://two.example/")
    assert one.json()["evidence_hash"] == two.json()["evidence_hash"]
    # The ETag fingerprints the same bytes as the hash; it must not vary by host.
    assert one.headers["etag"] == two.headers["etag"]


def test_a_stored_answer_can_be_compared_after_a_refetch(client: TestClient) -> None:
    body = client.get(f"/v1/score/{_seller()}").json()
    stored = body["evidence_hash"]

    again = client.get(f"/v1/score/{_seller()}").json()

    assert again["evidence_hash"] == stored
    assert evidence_hash(again) == stored


def test_the_mcp_tool_seals_found_and_unknown_answers_alike() -> None:
    found = check_counterparty(_seller(), "any")
    unknown = check_counterparty(UNKNOWN, "any")

    for answer in (found, unknown):
        assert answer["schema_version"] == SCHEMA_VERSION
        assert answer["not_an_endorsement"] is True
        assert answer["evidence_hash"] == evidence_hash(answer)


def test_the_mcp_tool_seals_rail_not_indexed_answers() -> None:
    answer = check_counterparty(LIGHTNING, "any")

    assert answer["status"] == "rail_not_indexed"
    assert answer["schema_version"] == SCHEMA_VERSION
    assert answer["not_an_endorsement"] is True
    assert answer["evidence_hash"] == evidence_hash(answer)


def test_the_envelope_does_not_weaken_the_disclaimer(client: TestClient) -> None:
    body = client.get(f"/v1/score/{_seller()}").json()
    assert DISCLAIMER_MARKER in body["disclaimer"]
