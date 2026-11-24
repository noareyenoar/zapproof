"""Smoke-check the hosted API, MCP endpoint and both agent skills.

Run after every deploy, before a registry or directory submission:

    uv run python scripts/smoke_hosted.py --base-url https://<service-url>

Run it against the exact URL you intend to publish. Cloud Run prints a new service
URL after some revisions, and `/mcp` answers `421` on a host that is not in
`ZAPPROOF_MCP_ALLOWED_HOSTS`; the script names that variable in the failure.

The 2026-11-17 deploy shipped a Cloud Run environment override that rewrote
`REPINDEX_DB` into a Windows path, so `/v1/methodology` and `/skill.md` answered
`200` while `/v1/health` and `/v1/score` answered `500`. A deploy is not done until
this script exits zero; checking one green route by eye is how that shipped.

Exit codes: 0 all checks passed, 1 any check failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

ZERO_ADDRESS = "0x" + "0" * 40
ACCEPT = "application/json, text/event-stream"


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise ValueError(f"not JSON: {response.text[:120]!r}") from exc
    if not isinstance(body, dict):
        raise ValueError(f"JSON body is {type(body).__name__}, not an object")
    return body


def _get_json(client: httpx.Client, url: str) -> tuple[httpx.Response, dict[str, Any]]:
    response = client.get(url)
    return response, _json(response)


def _health(client: httpx.Client, base: str) -> Check:
    response, body = _get_json(client, f"{base}/v1/health")
    ok = response.status_code == 200 and body.get("status") == "ok"
    return Check(
        "health",
        ok,
        f"{response.status_code} status={body.get('status')!r} "
        f"sellers={body.get('sellers_indexed')}",
    )


def _zero_score(client: httpx.Client, base: str) -> Check:
    response, body = _get_json(client, f"{base}/v1/score/{ZERO_ADDRESS}")
    found = body.get("found")
    score = body.get("score", "<absent>")
    ok = response.status_code == 200 and found is False and score in (None, "<absent>")
    return Check(
        "score-zero",
        ok,
        f"{response.status_code} found={found!r} score={score!r}",
    )


def _methodology(client: httpx.Client, base: str) -> Check:
    response, body = _get_json(client, f"{base}/v1/methodology")
    ok = response.status_code == 200 and bool(body)
    return Check("methodology", ok, f"{response.status_code} keys={len(body)}")


def _skill(client: httpx.Client, base: str, name: str, marker: str) -> Check:
    response = client.get(f"{base}/{name}")
    text = response.text
    ok = (
        response.status_code == 200
        and marker in text
        and "__API_URL__" not in text
    )
    return Check(name, ok, f"{response.status_code} bytes={len(text)}")


def _mcp(client: httpx.Client, base: str) -> list[Check]:
    headers = {"Accept": ACCEPT, "Content-Type": "application/json"}
    init = client.post(
        f"{base}/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "smoke-hosted", "version": "0"},
            },
        },
    )
    checks: list[Check] = []
    if init.status_code == 421:
        checks.append(
            Check(
                "mcp-initialize",
                False,
                "421 (host missing from ZAPPROOF_MCP_ALLOWED_HOSTS)",
            )
        )
        return checks
    try:
        init_body = _json(init)
        server_name = init_body.get("result", {}).get("serverInfo", {}).get("name")
        checks.append(
            Check(
                "mcp-initialize",
                init.status_code == 200 and server_name == "zapproof",
                f"{init.status_code} serverInfo.name={server_name!r}",
            )
        )
    except ValueError as exc:
        checks.append(Check("mcp-initialize", False, str(exc)))
        return checks

    listed = client.post(
        f"{base}/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}},
    )
    try:
        tools = _json(listed).get("result", {}).get("tools", [])
        names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
        checks.append(
            Check(
                "mcp-tools-list",
                listed.status_code == 200 and "check_counterparty" in names,
                f"{listed.status_code} tools={names}",
            )
        )
    except ValueError as exc:
        checks.append(Check("mcp-tools-list", False, str(exc)))
        return checks

    called = client.post(
        f"{base}/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 3,
            "params": {
                "name": "check_counterparty",
                "arguments": {"identifier": ZERO_ADDRESS},
            },
        },
    )
    try:
        body = _json(called)
        result = body.get("result", {})
        is_error = bool(result.get("isError"))
        content = result.get("content") or []
        text = content[0].get("text", "") if content and isinstance(content[0], dict) else ""
        parsed = json.loads(text) if text else {}
        ok = (
            called.status_code == 200
            and not is_error
            and parsed.get("found") is False
            and parsed.get("score") is None
        )
        checks.append(
            Check(
                "mcp-tools-call",
                ok,
                f"{called.status_code} isError={is_error} found={parsed.get('found')!r}",
            )
        )
    except (ValueError, json.JSONDecodeError) as exc:
        checks.append(Check("mcp-tools-call", False, str(exc)))
    return checks


def _safe(name: str, run: Callable[[], Check]) -> Check:
    try:
        return run()
    except (httpx.HTTPError, ValueError) as exc:
        return Check(name, False, str(exc))


def check(base_url: str, client: httpx.Client) -> list[Check]:
    """Run every check and return the results, including failures."""
    base = base_url.rstrip("/")
    checks = [
        _safe("health", lambda: _health(client, base)),
        _safe("score-zero", lambda: _zero_score(client, base)),
        _safe("methodology", lambda: _methodology(client, base)),
        _safe(
            "skill.md",
            lambda: _skill(client, base, "skill.md", "zapproof-counterparty-check"),
        ),
        _safe(
            "skill.th.md",
            lambda: _skill(client, base, "skill.th.md", "zapproof-counterparty-check-th"),
        ),
    ]
    checks.extend(_mcp(client, base))
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True, help="hosted origin, e.g. https://…")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    with httpx.Client(timeout=args.timeout, follow_redirects=True) as client:
        try:
            checks = check(args.base_url, client)
        except httpx.HTTPError as exc:
            print(f"FAIL transport: {exc}", file=sys.stderr)
            return 1

    for item in checks:
        print(f"{'PASS' if item.ok else 'FAIL'} {item.name}: {item.detail}")
    failed = [item.name for item in checks if not item.ok]
    if failed:
        print(f"\n{len(failed)} check(s) failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"\nall {len(checks)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
