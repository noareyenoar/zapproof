"""Tests for the MCP tool's logic (prompt task B2).

These call the tool function directly. The transport itself is covered separately by
`scripts/mcp_stdio_test.py`, which spawns a real subprocess; this file covers the
branches that script cannot reach cheaply — the rail guard, the failure paths, and the
response shape an agent will parse.

Why this file exists at all: the coverage artifact once reported 95% from tests that
were no longer in the repository, and `mcp/server.py` was at 0%. The independent review
found it. These tests are real and they are committed.
"""

from __future__ import annotations

import sqlite3

import pytest

from repindex.mcp import server

DISCLAIMER_MARKER = "not independently validated"
PAYER_ONLY = "0x000000000040f45bb6722481915df68fd8c4e5cb"


def _db_one(sql: str) -> tuple:
    conn = sqlite3.connect("file:data/repindex.db?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchone()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def a_scored_seller() -> str:
    conn = sqlite3.connect("file:data/repindex.db?mode=ro", uri=True)
    try:
        return str(
            conn.execute(
                "SELECT identifier FROM scores WHERE status='scored' "
                "ORDER BY score DESC LIMIT 1"
            ).fetchone()[0]
        )
    finally:
        conn.close()


def test_a_scored_seller_returns_the_compact_shape(a_scored_seller: str) -> None:
    body = server.check_counterparty(a_scored_seller)

    assert body["found"] is True
    assert body["evidence_rail"] == "base"
    assert body["status"] in ("scored", "insufficient_evidence")
    assert DISCLAIMER_MARKER in body["disclaimer"]
    assert body["methodology_version"]


def test_an_unknown_identifier_is_not_a_zero() -> None:
    body = server.check_counterparty("0xffffffffffffffffffffffffffffffffffffffff")

    assert body["found"] is False
    assert body["score"] is None
    assert body["flags"] == []
    assert "not a zero" in body["note"]


def test_the_lightning_rail_is_not_silently_answered_with_base_data() -> None:
    """The bug the review found: rail='lightning' returned a Base score tagged lightning."""
    body = server.check_counterparty(
        "0xffffffffffffffffffffffffffffffffffffffff", rail="lightning"
    )

    assert body["found"] is False
    assert body["score"] is None
    assert body["evidence_rail"] is None
    assert body["status"] == "rail_not_indexed"
    assert "not covered" in body["note"]


def test_the_lightning_rail_refuses_even_a_real_seller() -> None:
    body = server.check_counterparty(
        "0xffffffffffffffffffffffffffffffffffffffff", rail="lightning"
    )
    assert body["found"] is False
    assert body["evidence_rail"] is None


def test_an_invalid_rail_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    body = server.check_counterparty("0x" + "ab" * 20, rail="solana")  # type: ignore[arg-type]
    assert "error" in body
    assert body["disclaimer"]


def test_an_unavailable_index_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, a_scored_seller: str
) -> None:
    def boom(_conn: sqlite3.Connection, _identifier: str) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(server.queries, "fetch_score", boom)

    body = server.check_counterparty(a_scored_seller)

    assert body["error"] == "index_unavailable"
    assert DISCLAIMER_MARKER in body["disclaimer"]


def test_a_missing_database_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, a_scored_seller: str
) -> None:
    """F16: `connect()` used to sit outside the try, so this raised out of the tool."""

    def boom(_path: object = None) -> None:
        raise FileNotFoundError("index database not found")

    monkeypatch.setattr(server.queries, "connect", boom)

    body = server.check_counterparty(a_scored_seller)

    assert body["error"] == "index_unavailable"
    assert "not found" in body["detail"]
    assert DISCLAIMER_MARKER in body["disclaimer"]


def test_a_did_identifier_resolves_like_its_address(a_scored_seller: str) -> None:
    body = server.check_counterparty(f"did:ethr:{a_scored_seller}")

    assert body["found"] is True
    assert body["identifier"] == a_scored_seller


def test_an_invalid_identifier_is_refused_with_a_reason() -> None:
    body = server.check_counterparty("did:key:not-supported")

    assert body["error"] == "invalid_identifier"
    assert DISCLAIMER_MARKER in body["disclaimer"]


def test_a_known_payer_is_not_described_as_unknown() -> None:
    body = server.check_counterparty(PAYER_ONLY)

    assert body["found"] is False
    assert body["status"] == "no_seller_record"
    assert body["role_observed"] == "payer"
    assert body["payer_settlements"] > 0


def test_an_insufficient_evidence_seller_has_a_null_likelihood() -> None:
    (seller,) = _db_one(
        "SELECT seller FROM features ORDER BY settlement_count ASC LIMIT 1"
    )

    body = server.check_counterparty(seller)

    assert body["status"] == "insufficient_evidence"
    assert body["signaling_likelihood"] is None
    assert body["judged"] is False


def test_a_classification_dispute_is_reported_as_a_correction_not_a_rule(
    monkeypatch: pytest.MonkeyPatch, a_scored_seller: str
) -> None:
    """I2: `dispute_override` was listed in rules_fired and flags as a ninth rule."""
    data = {
        "identifier": a_scored_seller,
        "status": "scored",
        "score": 3.11,
        "confidence_band": "low",
        "settlements": 25,
        "counterparties": 7,
        "active_days": 4,
        "window": {"start_block": 1, "end_block": 2, "start_time": 1, "end_time": 2},
        "as_of_block": 2,
        "signaling_likelihood": 0.9,
        "rules": [
            {"rule": "fixed_amount", "fired": True, "contribution": 0.2, "detail": "entropy"},
            {
                "rule": "dispute_override",
                "fired": True,
                "contribution": 0.5,
                "computed_before": 0.4,
                "detail": (
                    "signaling_likelihood set to 0.900000 by an upheld dispute; "
                    "the rules computed 0.400000"
                ),
            },
        ],
        "labels": [],
    }
    monkeypatch.setattr(server.queries, "fetch_score", lambda _conn, _id: data)

    body = server.check_counterparty(a_scored_seller)

    assert body["rules_fired"] == ["fixed_amount"]
    assert body["flags"] == ["fixed_amount"], "a correction must not read as a pattern"
    assert body["correction"]["rule"] == "dispute_override"
    assert body["correction"]["computed_before"] == 0.4
    assert "upheld dispute" in body["correction"]["detail"]


def test_an_operator_group_is_discoverable_through_the_tool() -> None:
    row = _db_one(
        "SELECT l.address, l.label_value FROM labels l JOIN features f ON f.seller = l.address "
        "WHERE l.label_type = 'operator_group' LIMIT 1"
    )
    assert row is not None
    address, label_value = row
    expected_id = int(label_value.split(":", 1)[1])

    body = server.check_counterparty(address)

    assert expected_id in body["operator_groups"]
    group = [
        label for label in body["label_provenance"] if label["entity_id"] == expected_id
    ]
    assert group and group[0]["label_type"] == "operator_group"


def test_every_return_path_carries_the_disclaimer(
    monkeypatch: pytest.MonkeyPatch, a_scored_seller: str
) -> None:
    """GB.12 says every response. This checks all four branches of the tool."""
    bodies = [
        server.check_counterparty(a_scored_seller),
        server.check_counterparty("0xffffffffffffffffffffffffffffffffffffffff"),
        server.check_counterparty(a_scored_seller, rail="lightning"),
        server.check_counterparty("bad", rail="nope"),  # type: ignore[arg-type]
    ]
    for body in bodies:
        assert DISCLAIMER_MARKER in body["disclaimer"], body
