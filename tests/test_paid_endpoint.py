"""The paid x402 route: opt-in, checksum-validated, and honest about its price.

These tests need the `paid` extra (`uv run --extra paid pytest tests/test_paid_endpoint.py`);
the default suite skips them because the x402 SDK is not installed there.

Nothing here moves money. The facilitator is a local stub, so the middleware's
startup handshake (GET /supported) never leaves the machine, and every request is
unpaid — which is exactly the 402 a catalog probe will see.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("x402")

from fastapi import FastAPI, Request, Response  # noqa: E402 - after the importorskip
from fastapi.responses import JSONResponse  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from repindex.api import paid  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PAY_TO = "0x000000000000000000000000000000000000dEaD"
LOWERCASE_PAY_TO = "0x000000000000000000000000000000000000dead"
TESTNET_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
SUPPORTED = {
    "kinds": [{"x402Version": 2, "scheme": "exact", "network": paid.TESTNET}],
}


class _FacilitatorStub(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.rstrip("/").endswith("/supported"):
            body = json.dumps(SUPPORTED).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *args: object) -> None:
        """Silence the stub; test output should only carry failures."""


@pytest.fixture()
def facilitator(monkeypatch: pytest.MonkeyPatch) -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FacilitatorStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("ZAPPROOF_X402_FACILITATOR", url)
    monkeypatch.setenv("ZAPPROOF_X402_NETWORK", paid.TESTNET)
    monkeypatch.setenv("ZAPPROOF_X402_PAY_TO", PAY_TO)
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ZAPPROOF_X402_ENABLED",
        "ZAPPROOF_X402_PAY_TO",
        "ZAPPROOF_X402_NETWORK",
        "ZAPPROOF_X402_PRICE",
        "ZAPPROOF_X402_FACILITATOR",
        "ZAPPROOF_X402_ICON_URL",
        "CDP_API_KEY_ID",
        "CDP_API_KEY_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


def test_config_refuses_a_pay_to_that_cannot_catch_a_typo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)

    with pytest.raises(paid.PaidConfigError, match="ZAPPROOF_X402_PAY_TO is required"):
        paid.paid_config()

    monkeypatch.setenv("ZAPPROOF_X402_PAY_TO", LOWERCASE_PAY_TO)
    with pytest.raises(paid.PaidConfigError, match="checksummed"):
        paid.paid_config()


def test_config_rejects_an_unknown_network_and_a_malformed_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("ZAPPROOF_X402_PAY_TO", PAY_TO)

    monkeypatch.setenv("ZAPPROOF_X402_NETWORK", "eip155:1")
    with pytest.raises(paid.PaidConfigError, match="ZAPPROOF_X402_NETWORK"):
        paid.paid_config()

    monkeypatch.setenv("ZAPPROOF_X402_NETWORK", paid.TESTNET)
    monkeypatch.setenv("ZAPPROOF_X402_PRICE", "0.005")
    with pytest.raises(paid.PaidConfigError, match="ZAPPROOF_X402_PRICE"):
        paid.paid_config()

    monkeypatch.setenv("ZAPPROOF_X402_PRICE", "$0.005")
    assert paid.paid_config()["price"] == "$0.005"
    assert paid.paid_config()["pay_to"] == PAY_TO


def test_mainnet_without_cdp_keys_fails_before_it_can_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("ZAPPROOF_X402_PAY_TO", PAY_TO)

    with pytest.raises(paid.PaidConfigError, match="CDP_API_KEY_ID"):
        paid.facilitator_config(paid.MAINNET)

    # A named facilitator removes the CDP requirement.
    assert paid.facilitator_config(paid.MAINNET, "https://facilitator.example") == {
        "url": "https://facilitator.example"
    }


def test_the_unpaid_probe_sees_a_402_with_its_discovery_declaration(
    facilitator: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This is the response x402scan, x402-list and the CDP preflight validate."""
    _clear(monkeypatch)
    monkeypatch.setenv("ZAPPROOF_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("ZAPPROOF_X402_NETWORK", paid.TESTNET)
    monkeypatch.setenv("ZAPPROOF_X402_FACILITATOR", facilitator)

    app = FastAPI()

    @app.get("/v1/paid/score/{identifier}")
    def answer(identifier: str, request: Request) -> Response:
        return JSONResponse({"identifier": identifier, "found": False})

    paid.install_paid_endpoint(app, answer)

    with TestClient(app, base_url="https://zapproof.test") as client:
        response = client.get("/v1/paid/score/0xabc")

    assert response.status_code == 402
    body = json.loads(base64.b64decode(response.headers["payment-required"]))

    assert body["x402Version"] == 2
    assert body["resource"]["url"].startswith("https://zapproof.test/v1/paid/score/")
    accept = body["accepts"][0]
    assert accept["scheme"] == "exact"
    assert accept["network"] == paid.TESTNET
    assert accept["asset"] == TESTNET_USDC
    assert accept["amount"] == "5000", "the default price is $0.005, not $0.001"
    assert accept["payTo"] == PAY_TO
    assert accept["maxTimeoutSeconds"] == 60

    bazaar = body["extensions"]["bazaar"]
    assert bazaar["info"]["input"]["type"] == "http"
    assert bazaar["info"]["input"]["method"] == "GET"
    assert bazaar["info"]["input"]["pathParams"]["identifier"] == "0xabc"
    assert bazaar["schema"]["properties"]["input"]["required"] == ["type", "method"]


def test_the_real_app_keeps_the_free_route_free(
    facilitator: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route is installed into the shipped app, and /v1/score is untouched.

    A subprocess is the only honest way to test the import-time gate: the test
    process has already imported the app, so re-reading the environment in place
    would test a reload rather than a boot.
    """
    env = {
        **os.environ,
        "ZAPPROOF_X402_ENABLED": "1",
        "ZAPPROOF_X402_PAY_TO": PAY_TO,
        "ZAPPROOF_X402_NETWORK": paid.TESTNET,
        "ZAPPROOF_X402_FACILITATOR": facilitator,
        "RATE_LIMIT_PER_MINUTE": "0",
    }
    script = """
import json
from starlette.testclient import TestClient
from repindex.api import app as module

with TestClient(module.app, base_url="https://zapproof.test") as client:
    paid = client.get("/v1/paid/score/0x0000000000000000000000000000000000000000")
    free = client.get("/v1/score/0x0000000000000000000000000000000000000000")
    print(json.dumps({
        "paid": paid.status_code,
        "free": free.status_code,
        "paid_header": paid.headers.get("payment-required", ""),
    }))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]

    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["paid"] == 402
    assert result["free"] == 200
    body = json.loads(base64.b64decode(result["paid_header"]))
    assert body["accepts"][0]["payTo"] == PAY_TO


def test_the_gate_is_off_without_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env flag, no paid route — and no import of the x402 SDK in the app."""
    _clear(monkeypatch)
    script = (
        "from starlette.testclient import TestClient\n"
        "from repindex.api import app as module\n"
        "with TestClient(module.app) as client:\n"
        "    print(client.get('/v1/paid/score/0xabc').status_code)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO,
        env={**os.environ, "RATE_LIMIT_PER_MINUTE": "0"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.strip().splitlines()[-1] == "404"
