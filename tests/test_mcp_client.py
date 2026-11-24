"""The hosted-API path must answer exactly like the local database, and fail closed.

`zapproof-mcp` installed from a wheel has no database beside it. It either calls a
hosted index (`ZAPPROOF_API_URL`) or reads a snapshot. This file covers the hosted
path: parity against the real app, and the mapping that turns a transport failure into
`index_unavailable` rather than into a score.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from repindex.api.app import app
from repindex.mcp import client as hosted
from repindex.mcp.server import check_counterparty

REPO_DB = "file:data/repindex.db?mode=ro"


@pytest.fixture
def api_transport(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Route the client's HTTP calls into the real FastAPI app."""
    test_client = TestClient(app)

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return test_client.get(urlparse(str(url)).path)

    monkeypatch.setattr(hosted.httpx, "get", fake_get)
    return test_client


def _seller() -> str:
    conn = sqlite3.connect(REPO_DB, uri=True)
    try:
        row = conn.execute(
            "SELECT seller FROM features ORDER BY settlement_count DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


def _canned(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> None:
    monkeypatch.setattr(hosted.httpx, "get", lambda *args, **kwargs: response)


def test_parity_for_a_scored_seller(api_transport: TestClient) -> None:
    seller = _seller()

    local = check_counterparty(seller, "any")
    remote = hosted.lookup("http://api.test", seller, "any")

    assert remote == local


def test_parity_for_an_unknown_address(api_transport: TestClient) -> None:
    unknown = "0x" + "cd" * 20

    local = check_counterparty(unknown, "any")
    remote = hosted.lookup("http://api.test", unknown, "any")

    assert remote == local


def test_invalid_identifier_maps_to_the_tools_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _canned(monkeypatch, httpx.Response(422, json={"detail": "not an address"}))

    answer = hosted.lookup("http://api.test", "nope", "any")

    assert answer["error"] == "invalid_identifier"
    assert answer["detail"] == "not an address"


@pytest.mark.parametrize("status", [429, 500, 503, 404])
def test_other_statuses_are_unavailable_not_unknown(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _canned(monkeypatch, httpx.Response(status, json={"detail": "down"}))

    answer = hosted.lookup("http://api.test", "0x" + "11" * 20, "any")

    assert answer["error"] == "index_unavailable"
    assert "score" not in answer


def test_timeout_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.TimeoutException("too slow")

    monkeypatch.setattr(hosted.httpx, "get", boom)

    answer = hosted.lookup("http://api.test", "0x" + "11" * 20, "any")

    assert answer["error"] == "index_unavailable"


def test_non_json_body_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _canned(monkeypatch, httpx.Response(200, text="<html>proxy error</html>"))

    answer = hosted.lookup("http://api.test", "0x" + "11" * 20, "any")

    assert answer["error"] == "index_unavailable"


def test_the_tool_uses_the_hosted_index_when_configured(
    monkeypatch: pytest.MonkeyPatch, api_transport: TestClient
) -> None:
    seller = _seller()
    monkeypatch.setenv("ZAPPROOF_API_URL", "http://api.test")

    answer = check_counterparty(seller, "any")

    assert answer["found"] is True
    assert answer["identifier"] == seller


def test_lightning_never_reaches_the_hosted_index(
    monkeypatch: pytest.MonkeyPatch, api_transport: TestClient
) -> None:
    monkeypatch.setenv("ZAPPROOF_API_URL", "http://api.test")

    answer = check_counterparty(_seller(), "lightning")

    assert answer["status"] == "rail_not_indexed"
    assert answer["evidence_rail"] is None


def test_repo_has_a_database_so_the_parity_tests_are_meaningful() -> None:
    assert Path("data/repindex.db").is_file()
