"""The remote MCP app must survive a proxy and be bounded against abuse.

Two failures this guards against, both silent in production: a proxy that preserves
`Host` getting 421 on every request (the SDK's loopback-only allow-list), and an
unauthenticated public endpoint with no rate limit.
"""

from __future__ import annotations

import json
from pathlib import Path

from starlette.testclient import TestClient

from repindex.api.usage import UsageStore
from repindex.mcp import http_app

INITIALIZE = {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}}


def test_foreign_host_is_rejected_with_421() -> None:
    app = http_app.create_app(allowed_hosts="mcp.example.com", max_sessions=5, rate_limit=100)

    with TestClient(app) as client:
        response = client.post("/mcp", json=INITIALIZE)

    assert response.status_code == 421


def test_configured_host_is_accepted_through_the_proxy_name() -> None:
    app = http_app.create_app(allowed_hosts="testserver", max_sessions=5, rate_limit=100)

    with TestClient(app) as client:
        response = client.post("/mcp", json=INITIALIZE)

    assert response.status_code != 421


def test_rate_limit_returns_429() -> None:
    app = http_app.create_app(allowed_hosts="testserver", max_sessions=5, rate_limit=1)

    with TestClient(app) as client:
        first = client.get("/mcp")
        second = client.get("/mcp")

    assert first.status_code != 429
    assert second.status_code == 429


def test_request_label_names_the_tool_but_never_the_arguments() -> None:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 1,
            "params": {
                "name": "check_counterparty",
                "arguments": {"identifier": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"},
            },
        }
    ).encode()

    label = http_app.request_label("/mcp", "POST", body)

    assert label == "/mcp/tools/call/check_counterparty"
    assert "0x8335" not in label


def test_request_label_leaves_non_tool_requests_at_the_route() -> None:
    assert http_app.request_label("/mcp", "POST", json.dumps(INITIALIZE).encode()) == "/mcp"
    assert http_app.request_label("/mcp", "GET", b"") == "/mcp"
    assert http_app.request_label("/mcp", "POST", b"<xml>not json</xml>") == "/mcp"


def test_mcp_requests_are_recorded_in_the_shared_usage_store(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "usage.db")
    app = http_app.create_app(allowed_hosts="testserver", usage_store=store)
    try:
        with TestClient(app) as client:
            client.post("/mcp", json=INITIALIZE)
        row = store.conn.execute("SELECT path, status, ip_hash FROM requests").fetchone()
    finally:
        store.close()

    assert row is not None
    assert row["path"] == "/mcp"
    assert len(row["ip_hash"]) == 64


def test_host_patterns_cover_exact_and_port_forms() -> None:
    assert http_app.host_patterns("mcp.example.com") == [
        "mcp.example.com",
        "mcp.example.com:*",
    ]
    assert http_app.host_patterns("127.0.0.1,::1") == [
        "127.0.0.1",
        "127.0.0.1:*",
        "[::1]",
        "[::1]:*",
    ]
