"""Exercise the MCP tool over a real stdio transport (prompt task B2, gate GB.6).

Not an in-process call: this spawns the server as a subprocess exactly as an agent's
MCP client would, so the transport, the handshake and the tool schema are all covered.

Writes reports/mcp_test.json with the three cases the gate requires:
  invalid_identifier, unknown_address, insufficient_evidence

Run:  uv run python scripts/mcp_stdio_test.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from repindex.common.db import connect_readonly
from repindex.common.provenance import (
    CommitObserver,
    DatabaseChangedError,
    PendingWALError,
    attest_identity,
    data_version,
    provenance,
    require_no_commits,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = REPO_ROOT / "reports" / "mcp_test.json"
DB = REPO_ROOT / "data" / "repindex.db"


def _thin_seller_with_distinct_payers() -> str:
    """A present seller that is definitely below the evidence minimum."""
    version_before = -1
    version_after = -1
    conn = connect_readonly(DB)
    try:
        version_before = data_version(conn)
        row = conn.execute(
            "SELECT seller FROM features WHERE settlement_count < 20 "
            "ORDER BY settlement_count DESC LIMIT 1"
        ).fetchone()
        version_after = data_version(conn)
    finally:
        conn.close()
    require_no_commits(DB, version_before, version_after)
    return str(row[0])


def _payload(result: Any) -> dict[str, Any]:
    """MCP returns a content list; our tool returns one JSON object."""
    if getattr(result, "structuredContent", None):
        structured = result.structuredContent
        if isinstance(structured, dict):
            # FastMCP-style servers wrap the value under "result".
            return structured.get("result", structured)  # type: ignore[no-any-return]
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)  # type: ignore[no-any-return]
            except json.JSONDecodeError:
                continue
    return {}


async def run() -> int:
    # P2: the observer spans the attestation hash and the whole subprocess run, so a
    # commit during either is refused even if the bytes are restored afterwards.
    observer = CommitObserver(DB)
    try:
        attested = attest_identity(DB)
    except PendingWALError as exc:
        observer.close()
        print(f"{exc} The MCP test is not written from a state its provenance cannot name.")
        return 1
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "repindex.mcp.server"],
        cwd=str(REPO_ROOT),
    )

    cases: dict[str, dict[str, Any]] = {}

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        registered = "check_counterparty" in names

        async def call(identifier: str) -> dict[str, Any]:
            result = await session.call_tool("check_counterparty", {"identifier": identifier})
            return _payload(result)

        body = await call("not-an-address")
        cases["invalid_identifier"] = {
            "pass": body.get("error") is not None or body.get("found") is False,
            "detail": f"returned {body.get('error') or body.get('status')}",
        }

        body = await call("0xffffffffffffffffffffffffffffffffffffffff")
        cases["unknown_address"] = {
            "pass": body.get("found") is False and body.get("score") is None,
            "detail": f"found={body.get('found')} score={body.get('score')}",
        }

        try:
            thin_seller = _thin_seller_with_distinct_payers()
        except (PendingWALError, DatabaseChangedError) as exc:
            observer.close()
            print(f"{exc} The MCP test is not written from a state it cannot name.")
            return 1
        body = await call(thin_seller)
        cases["insufficient_evidence"] = {
            "pass": body.get("status") == "insufficient_evidence" and body.get("score") is None,
            "detail": f"status={body.get('status')} score={body.get('score')}",
        }

    try:
        observer.require_quiet()
        prov = provenance(DB, attested)
    except (PendingWALError, DatabaseChangedError) as exc:
        print(f"{exc} The MCP test is not written from a state it cannot name.")
        return 1
    finally:
        observer.close()
    payload = {
        **prov,
        "tool_registered": registered,
        "tools": names,
        "cases": cases,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"tools registered: {names}")
    for name, case in cases.items():
        print(f"  {name:24} {'PASS' if case['pass'] else 'FAIL'}  {case['detail']}")
    print(f"wrote {OUT.relative_to(REPO_ROOT)}")
    return 0 if registered and all(c["pass"] for c in cases.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
