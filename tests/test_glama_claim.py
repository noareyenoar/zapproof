"""The Glama ownership proof must appear on the connector's own origin — and nowhere.

Glama offers three verification methods. On Cloud Run only the HTTP challenge can
work: the connector host is `*.run.app`, so the `_glama-claim` TXT record would have to
be created in a DNS zone Google owns. The claim token is account-bound and contains no
personal information, but it is still read from the environment and never logged: a
repository that carries it would be serving a claim its owner did not publish.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from repindex.api import app as app_module
from repindex.api.app import GLAMA_CLAIM_SCHEMA, app

TOKEN = "glama_claim_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    app_module._rate_buckets.clear()
    monkeypatch.delenv("GLAMA_CLAIM_TOKEN", raising=False)
    yield
    app_module._rate_buckets.clear()


def test_the_claim_is_served_when_a_token_is_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GLAMA_CLAIM_TOKEN", TOKEN)

    response = client.get("/.well-known/glama.json")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"$schema": GLAMA_CLAIM_SCHEMA, "claim": TOKEN}


def test_no_token_is_a_plain_404(client: TestClient) -> None:
    assert client.get("/.well-known/glama.json").status_code == 404


@pytest.mark.parametrize(
    "value",
    [
        "not-a-token",
        "glama_claim_",
        "glama_claim_short",
        "glama_claim_" + "A" * 31,
        "glama_claim_" + "A" * 33,
        "glama_claim_" + "A" * 31 + "!",  # outside the documented alphabet
        "glama_claim_" + "A" * 31 + "é",  # non-ASCII
    ],
)
def test_a_malformed_token_is_never_echoed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("GLAMA_CLAIM_TOKEN", value)

    response = client.get("/.well-known/glama.json")

    assert response.status_code == 404
    assert value not in response.text


def test_the_claim_route_is_not_part_of_the_public_api_documentation(
    client: TestClient,
) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/.well-known/glama.json" not in paths
