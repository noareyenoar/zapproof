"""Edge cases the independent Gate B review found untested (F1, F3, F6, F7, F13, F14, F20).

The review's central criticism of the coverage artifact was not that the number was
wrong but that the tests behind it did not exist in the repository. These tests do
exist. They cover the response classes the gate's four sampled 200s never touched:
429, 500, 304, request-validation 422s, the entity route, DID identifiers, the
payer-only wording, null likelihood, and operator-group provenance.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from repindex.api import app as app_module
from repindex.api import queries
from repindex.api.app import app
from repindex.api.identifiers import (
    KIND_EVM,
    KIND_LIGHTNING_ADDRESS,
    KIND_LIGHTNING_NODE_PUBKEY,
    KIND_MPP_OPERATOR,
    InvalidIdentifier,
    classify_identifier,
    normalise_identifier,
)

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
def _clear_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Buckets are module-global; one test's 429 must not leak into the next."""
    app_module._rate_buckets.clear()
    yield
    app_module._rate_buckets.clear()


# --------------------------------------------------------------------------- #
# F3 — every response class carries the disclaimer
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/v1/nonexistent", 404),
        ("POST", "/v1/health", 405),
        ("GET", "/v1/score/not-an-address", 422),
        ("GET", "/v1/entity/abc", 422),
        ("GET", "/v1/entity/99999999999999999999999999999999999999", 422),
    ],
)
def test_error_responses_carry_the_disclaimer(
    client: TestClient, method: str, path: str, expected: int
) -> None:
    response = client.request(method, path)

    assert response.status_code == expected
    assert DISCLAIMER_MARKER in response.text, response.text


@pytest.mark.parametrize(
    "path",
    ["/v1/health", "/v1/methodology", "/v1/entity/61", "/v1/score/0x" + "f" * 40],
)
def test_every_v1_route_is_rate_limited_and_carries_the_disclaimer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """F6: only the score route was limited; task B1 requires every route.

    A positive limit forces the 429 now. The earlier version set the limit to 0,
    which only produced a 429 because of the G3 bug — the test was proving the wrong
    thing. One request consumes the single slot; the next one is refused.
    """
    monkeypatch.setattr(app_module, "RATE_LIMIT_PER_MINUTE", 1)

    first = client.get("/v1/health")
    response = client.get(path)

    assert first.status_code == 200
    assert response.status_code == 429
    assert DISCLAIMER_MARKER in response.json()["disclaimer"]


def test_a_zero_rate_limit_means_unlimited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G3: `0` is documented as the off switch; it must not deny every request."""
    monkeypatch.setattr(app_module, "RATE_LIMIT_PER_MINUTE", 0)

    for _ in range(5):
        assert client.get("/v1/health").status_code == 200


def test_the_unhandled_error_response_carries_the_disclaimer_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seller, _ = _db_one("SELECT seller, settlement_count FROM features LIMIT 1")

    def boom(_conn: sqlite3.Connection, _identifier: str) -> None:
        raise RuntimeError("synthetic failure for the error handler")

    monkeypatch.setattr(queries, "fetch_score", boom)
    tolerant = TestClient(app, raise_server_exceptions=False)

    response = tolerant.get(f"/v1/score/{seller}")

    assert response.status_code == 500
    assert response.json()["error"] == "internal_error"
    assert DISCLAIMER_MARKER in response.json()["disclaimer"]
    assert "Traceback" not in response.text


def test_a_conditional_request_returns_304_with_no_body(client: TestClient) -> None:
    """304 is the one response class that cannot carry a JSON disclaimer."""
    seller, _ = _db_one("SELECT seller, settlement_count FROM features LIMIT 1")

    first = client.get(f"/v1/score/{seller}")
    second = client.get(f"/v1/score/{seller}", headers={"If-None-Match": first.headers["etag"]})

    assert second.status_code == 304
    assert second.content == b""


# --------------------------------------------------------------------------- #
# F6 — strict input validation on the entity route
# --------------------------------------------------------------------------- #


def test_the_entity_route_answers_for_a_real_group(client: TestClient) -> None:
    (group_id,) = _db_one("SELECT entity_id FROM entities LIMIT 1")

    response = client.get(f"/v1/entity/{group_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["group_id"] == group_id
    assert body["members"]
    assert DISCLAIMER_MARKER in body["disclaimer"]


def test_a_non_integer_group_id_is_a_422_not_a_500(client: TestClient) -> None:
    response = client.get("/v1/entity/not-a-number")

    assert response.status_code == 422
    assert "Traceback" not in response.text


def test_a_group_id_beyond_sqlite_int64_is_a_422_not_a_500(client: TestClient) -> None:
    """The review reached a 500 here with a 38-digit id. A bad id is a bad request."""
    response = client.get("/v1/entity/99999999999999999999999999999999999999")

    assert response.status_code == 422
    assert "out of range" in response.text


# --------------------------------------------------------------------------- #
# F14 — DIDs are part of the published identifier contract
# --------------------------------------------------------------------------- #


def test_a_did_ethr_identifier_resolves_to_its_address(client: TestClient) -> None:
    seller, _ = _db_one("SELECT seller, settlement_count FROM features LIMIT 1")

    response = client.get(f"/v1/score/did:ethr:{seller}")

    assert response.status_code == 200
    assert response.json()["identifier"] == seller


def test_a_did_pkh_identifier_resolves_to_its_address(client: TestClient) -> None:
    seller, _ = _db_one("SELECT seller, settlement_count FROM features LIMIT 1")

    response = client.get(f"/v1/score/did:pkh:eip155:8453:{seller}")

    assert response.status_code == 200
    assert response.json()["identifier"] == seller


def test_an_unsupported_did_method_is_rejected_with_a_reason(client: TestClient) -> None:
    response = client.get("/v1/score/did:web:example.com")

    assert response.status_code == 422
    assert "unsupported DID method" in response.text


def test_the_did_normaliser_is_its_own_contract() -> None:
    address = "0xaf36084a4dc0ce760a6dcfa46cf129e140c92aa9"

    assert normalise_identifier(address) == address
    assert normalise_identifier(f"did:ethr:{address}") == address
    assert normalise_identifier(f"did:ethr:8453:{address}") == address
    assert normalise_identifier(f"did:ethr:0x2105:{address}") == address
    assert normalise_identifier(f"did:pkh:eip155:8453:{address}") == address

    with pytest.raises(InvalidIdentifier):
        normalise_identifier("did:web:example.com")
    with pytest.raises(InvalidIdentifier):
        normalise_identifier("did:pkh:eip155:not-a-chain:0x1234")


def test_a_did_naming_another_chain_is_rejected_not_answered_with_base_data(
    client: TestClient,
) -> None:
    """G1: the same address on another chain is a different subject.

    The second review found mainnet and invented networks silently receiving the
    Base-chain score, with no field saying the qualifier was discarded.
    """
    seller, _ = _db_one("SELECT seller, settlement_count FROM features LIMIT 1")

    for identifier in (
        f"did:pkh:eip155:1:{seller}",
        f"did:ethr:mainnet:{seller}",
        f"did:ethr:evil:{seller}",
    ):
        response = client.get(f"/v1/score/{identifier}")
        assert response.status_code == 422, (identifier, response.text)
        assert "only Base" in response.text
        assert "score" not in response.json()


# --------------------------------------------------------------------------- #
# Non-indexed rails — a well-formed Lightning/MPP identifier is coverage, not input
# --------------------------------------------------------------------------- #


def test_a_lightning_node_pubkey_is_rail_not_indexed_not_a_422(
    client: TestClient,
) -> None:
    """Before 2026-11-25 the live API answered 422 for a valid 33-byte node key."""
    identifier = "02" + "ab" * 32

    response = client.get(f"/v1/score/{identifier}")

    assert response.status_code == 200
    body = response.json()
    assert body["found"] is False
    assert body["status"] == "rail_not_indexed"
    assert body["rail"] == "lightning"
    assert body["identifier_type"] == KIND_LIGHTNING_NODE_PUBKEY
    assert body["score"] is None
    assert body["identifier"] == identifier
    assert DISCLAIMER_MARKER in body["disclaimer"]
    assert "not a low score" in body["note"]


def test_a_lightning_address_is_rail_not_indexed_and_normalised(
    client: TestClient,
) -> None:
    response = client.get("/v1/score/User@Example.COM")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rail_not_indexed"
    assert body["rail"] == "lightning"
    assert body["identifier_type"] == KIND_LIGHTNING_ADDRESS
    assert body["identifier"] == "user@example.com"
    assert body["score"] is None


def test_an_mpp_operator_is_rail_not_indexed(client: TestClient) -> None:
    response = client.get("/v1/score/mpp:operator-1")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rail_not_indexed"
    assert body["rail"] == "mpp"
    assert body["identifier_type"] == KIND_MPP_OPERATOR
    assert body["identifier"] == "operator-1"
    assert body["score"] is None


def test_a_non_indexed_identifier_never_carries_base_evidence(
    client: TestClient,
) -> None:
    for identifier in ("02" + "ab" * 32, "user@example.com", "mpp:operator-1"):
        body = client.get(f"/v1/score/{identifier}").json()
        assert body.get("evidence") is None
        assert body.get("evidence_rail") is None
        assert body.get("score") is None
        assert body.get("as_of_block") is None


def test_the_identifier_classifier_separates_rails() -> None:
    raw_pubkey = "03" + "CD" * 32
    assert classify_identifier(raw_pubkey) == (
        KIND_LIGHTNING_NODE_PUBKEY,
        raw_pubkey.lower(),
    )
    assert classify_identifier("User@Example.COM") == (
        KIND_LIGHTNING_ADDRESS,
        "user@example.com",
    )
    assert classify_identifier("mpp:Operator-1") == (KIND_MPP_OPERATOR, "Operator-1")
    assert classify_identifier("0x" + "ab" * 20) == (KIND_EVM, "0x" + "ab" * 20)

    with pytest.raises(InvalidIdentifier):
        classify_identifier("not-an-address")


# --------------------------------------------------------------------------- #
# F13 — found:false must not claim the index knows nothing about a payer
# --------------------------------------------------------------------------- #

# 5,426 settlements as payer, none as seller, on 2026-10-30. The review used it.
PAYER_ONLY = "0x000000000040f45bb6722481915df68fd8c4e5cb"


def test_a_known_payer_is_not_described_as_unknown(client: TestClient) -> None:
    body = client.get(f"/v1/score/{PAYER_ONLY}").json()

    assert body["found"] is False
    assert body["role_observed"] == "payer"
    assert body["payer_settlements"] > 0
    assert body["seller_settlements"] == 0
    assert "known to the index" in body["note"]


def test_an_address_the_index_never_saw_is_described_as_unknown(client: TestClient) -> None:
    body = client.get("/v1/score/0xffffffffffffffffffffffffffffffffffffffff").json()

    assert body["found"] is False
    assert body.get("role_observed") is None
    assert "unknown, not bad" in body["note"]


# --------------------------------------------------------------------------- #
# F7 — unknown is not zero in the interpretation, not just in the score
# --------------------------------------------------------------------------- #


def test_an_insufficient_evidence_seller_has_a_null_likelihood_not_zero(
    client: TestClient,
) -> None:
    seller, _ = _db_one(
        "SELECT seller, settlement_count FROM features ORDER BY settlement_count ASC LIMIT 1"
    )

    body = client.get(f"/v1/score/{seller}").json()

    assert body["status"] == "insufficient_evidence"
    assert body["score"] is None
    assert body["interpretation"]["signaling_likelihood"] is None
    assert body["interpretation"]["judged"] is False


def test_a_scored_seller_has_a_likelihood_and_is_marked_judged(client: TestClient) -> None:
    seller, _ = _db_one(
        "SELECT identifier, score FROM scores WHERE status='scored' ORDER BY score DESC LIMIT 1"
    )

    body = client.get(f"/v1/score/{seller}").json()

    assert body["status"] == "scored"
    assert body["interpretation"]["judged"] is True
    assert 0.0 <= body["interpretation"]["signaling_likelihood"] <= 1.0


# --------------------------------------------------------------------------- #
# F20 — operator-group attribution is discoverable
# --------------------------------------------------------------------------- #


def test_an_operator_group_label_exposes_its_entity_id(client: TestClient) -> None:
    row = _db_one(
        "SELECT l.address, l.label_value FROM labels l JOIN features f ON f.seller = l.address "
        "WHERE l.label_type = 'operator_group' LIMIT 1"
    )
    assert row is not None, "the fixture database should contain an operator-group label"
    address, label_value = row
    expected_id = int(label_value.split(":", 1)[1])

    body = client.get(f"/v1/score/{address}").json()

    groups = [
        label
        for label in body["label_provenance"]
        if label["label_type"] == "operator_group"
    ]
    assert groups, body["label_provenance"]
    assert groups[0]["label_value"] == label_value
    assert groups[0]["entity_id"] == expected_id

    entity = client.get(f"/v1/entity/{expected_id}")
    assert entity.status_code == 200
    assert address in entity.json()["members"]
