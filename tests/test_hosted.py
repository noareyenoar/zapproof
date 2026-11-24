"""One origin for the API, the remote MCP endpoint and the skill (Cloud Run).

Three failures this guards against, all invisible until a client connects:

- `/mcp` reaching the API's 404 because a plain `Mount("/mcp", ...)` does not match
  the bare path that MCP clients use;
- the MCP session manager's lifespan never starting, so every call answers 500 with
  "Task group is not initialized" while `/v1/health` still looks fine;
- `/skill.md` shipping with its URL placeholder or announcing an endpoint that is not
  the one serving it.
"""

from __future__ import annotations

import sqlite3

from starlette.testclient import TestClient

from repindex import hosted

ACCEPT = {"Accept": "application/json, text/event-stream"}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    },
}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


def _app(**overrides: object) -> hosted.HostedApp:
    options: dict[str, object] = {
        "allowed_hosts": "testserver",
        "max_sessions": 5,
        "rate_limit": 1000,
        "stateless": True,
        "json_response": True,
    }
    options.update(overrides)
    return hosted.create_app(**options)  # type: ignore[arg-type]


def _top_seller() -> str:
    conn = sqlite3.connect("file:data/repindex.db?mode=ro", uri=True)
    try:
        return str(
            conn.execute(
                "SELECT seller FROM features ORDER BY settlement_count DESC LIMIT 1"
            ).fetchone()[0]
        )
    finally:
        conn.close()


def test_one_origin_serves_the_api_the_mcp_endpoint_and_the_skill() -> None:
    with TestClient(_app()) as client:
        health = client.get("/v1/health")
        initialize = client.post("/mcp", json=INITIALIZE, headers=ACCEPT)
        skill = client.get("/skill.md")
        skill_th = client.get("/skill.th.md")

    assert health.status_code == 200

    assert initialize.status_code == 200
    assert initialize.headers["content-type"].startswith("application/json")
    assert initialize.json()["result"]["serverInfo"]["name"]

    assert skill.status_code == 200
    assert skill.headers["content-type"].startswith("text/markdown")
    assert "zapproof-counterparty-check" in skill.text
    assert "__API_URL__" not in skill.text
    assert "http://testserver" in skill.text

    assert skill_th.status_code == 200
    assert skill_th.headers["content-type"].startswith("text/markdown")
    assert "zapproof-counterparty-check-th" in skill_th.text
    assert "ตรวจคู่ค้าก่อนจ่าย" in skill_th.text
    assert "__API_URL__" not in skill_th.text
    assert "http://testserver" in skill_th.text


def test_the_mcp_endpoint_lists_and_calls_the_tool_through_the_combined_app() -> None:
    seller = _top_seller()
    call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "check_counterparty", "arguments": {"identifier": seller}},
    }

    with TestClient(_app()) as client:
        listed = client.post("/mcp", json=TOOLS_LIST, headers=ACCEPT)
        called = client.post("/mcp", json=call, headers=ACCEPT)

    assert listed.status_code == 200
    tools = listed.json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["check_counterparty"]

    assert called.status_code == 200
    content = called.json()["result"]["content"]
    assert content and content[0]["type"] == "text"
    assert seller.lower() in content[0]["text"].lower()


def test_a_non_mcp_path_still_reaches_the_api() -> None:
    with TestClient(_app()) as client:
        score = client.get("/v1/score/0x0000000000000000000000000000000000000000")

    assert score.status_code == 200
    assert score.json()["found"] is False
