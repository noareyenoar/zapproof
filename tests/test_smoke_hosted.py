"""The post-deploy smoke test must fail on the 2026-11-17 failure shape."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import httpx

REPO = Path(__file__).resolve().parent.parent
SOLANA = "1" * 32
SOLANA_COVERAGE = (
    "Solana rail: observation window 2026-11-18T04:27Z to 2026-11-25T04:24Z. "
    "The classifier is the x402scan fee-payer allowlist (2026-11-25, 12 addresses)."
)


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "smoke_hosted", REPO / "scripts" / "smoke_hosted.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # The script defines a dataclass; `dataclasses` resolves annotations through
    # sys.modules, so a module that is never registered breaks at definition time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/health":
        return httpx.Response(
            200, json={"status": "ok", "as_of_block": 1, "sellers_indexed": 19397}
        )
    if request.url.path == f"/v1/score/{SOLANA}":
        return httpx.Response(
            200,
            json={"found": False, "rail": "solana", "coverage": SOLANA_COVERAGE},
        )
    if request.url.path.startswith("/v1/score/"):
        return httpx.Response(
            200,
            json={
                "found": False,
                "note": "This is not a low score. An address with no activity is unknown.",
            },
        )
    if request.url.path == "/v1/methodology":
        return httpx.Response(200, json={"methodology_version": "v1"})
    if request.url.path == "/skill.md":
        return httpx.Response(200, text="---\nname: zapproof-counterparty-check\n---\n")
    if request.url.path == "/skill.th.md":
        return httpx.Response(
            200, text="---\nname: zapproof-counterparty-check-th\n---\n"
        )
    if request.url.path == "/mcp":
        body = json.loads(request.content)
        method = body["method"]
        if method == "initialize":
            result = {"serverInfo": {"name": "zapproof"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "check_counterparty"}]}
        else:
            result = {
                "isError": False,
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"found": False, "score": None}),
                    }
                ],
            }
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
    return httpx.Response(404, text="not found")


def test_every_surface_passes_on_a_healthy_deployment() -> None:
    smoke = _script()
    with httpx.Client(transport=httpx.MockTransport(_handler)) as client:
        checks = smoke.check("https://zp.test", client)

    assert len(checks) == 9
    assert [check.name for check in checks if not check.ok] == []


def test_a_deployment_that_shipped_without_the_solana_index_fails() -> None:
    """`rail_not_indexed` is the honest answer for an undeployed index — and a failed
    deploy for one that was supposed to carry data/solana.db."""
    smoke = _script()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/score/"):
            return httpx.Response(
                200,
                json={
                    "found": False,
                    "rail": "solana",
                    "status": "rail_not_indexed",
                    "note": "No solana evidence is indexed yet.",
                },
            )
        return _handler(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        checks = smoke.check("https://zp.test", client)

    assert [check.name for check in checks if not check.ok] == ["solana-index"]


def test_a_500_health_response_fails_without_hiding_the_other_checks() -> None:
    smoke = _script()

    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/health":
            return httpx.Response(500, json={"error": "internal_error"})
        return _handler(request)

    with httpx.Client(transport=httpx.MockTransport(broken)) as client:
        checks = smoke.check("https://zp.test", client)

    health = next(check for check in checks if check.name == "health")
    assert health.ok is False
    assert "500" in health.detail
    assert len(checks) == 9
    assert [check.name for check in checks if not check.ok] == ["health"]


def test_a_non_json_health_response_is_a_failure_not_a_crash() -> None:
    smoke = _script()

    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/health":
            return httpx.Response(502, text="<html>bad gateway</html>")
        return _handler(request)

    with httpx.Client(transport=httpx.MockTransport(broken)) as client:
        checks = smoke.check("https://zp.test", client)

    health = next(check for check in checks if check.name == "health")
    assert health.ok is False
    assert "not JSON" in health.detail


def test_a_421_names_the_allowed_hosts_variable() -> None:
    smoke = _script()

    def wrong_host(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/mcp":
            return httpx.Response(421, text="Misdirected Request")
        return _handler(request)

    with httpx.Client(transport=httpx.MockTransport(wrong_host)) as client:
        checks = smoke.check("https://zp.test", client)

    initialize = next(check for check in checks if check.name == "mcp-initialize")
    assert initialize.ok is False
    assert "ALLOWED_HOSTS" in initialize.detail
