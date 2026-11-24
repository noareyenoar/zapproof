"""ASGI app for the hosted MCP endpoint.

`server.run(transport="streamable-http")` is convenient, but it builds a bare uvicorn
config with access logging on: every request would write the client's IP to disk,
which contradicts the rule that telemetry stores only salted hashes. This module
builds the Starlette app explicitly so the deployment can run:

    uvicorn repindex.mcp.http_app:app --no-access-log --proxy-headers \\
        --forwarded-allow-ips=127.0.0.1

Two things that silently break a proxied deployment, both handled here:

1. **Host header.** The SDK auto-enables DNS-rebinding protection whenever the bind
   host is loopback, with `allowed_hosts` limited to `127.0.0.1`/`localhost`. A TLS
   proxy that preserves `Host: mcp.zapproof.com` then gets **421** on every request.
   Set `ZAPPROOF_MCP_ALLOWED_HOSTS=mcp.zapproof.com` in production.

2. **No rate limit.** The score API's limiter only covers `/v1/`; a public MCP
   endpoint has none. `ClientRateLimit` below keys on the transport client address,
   which `--proxy-headers` rewrites from X-Forwarded-For, and bounds its own memory.

The endpoint is read-only and unauthenticated by design: the tool exposes public
evidence only, and nothing here can mutate the index.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque

from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from repindex.api.usage import (
    UsageRecorder,
    client_ip,
    now_seconds,
    store_from_env,
)
from repindex.mcp.server import server

DEFAULT_ALLOWED_HOSTS = "127.0.0.1,localhost,::1"

logger = logging.getLogger(__name__)

# Only the JSON-RPC envelope is inspected to label a request. The body is not stored,
# and a call larger than this is left unlabelled rather than buffered without limit.
MAX_RECORDED_BODY = 64 * 1024


class ClientRateLimit:
    """Per-client request limiter for the MCP transport.

    Uvicorn rewrites `scope["client"]` from X-Forwarded-For when `--proxy-headers` is
    on, so behind the proxy this is the real caller rather than 127.0.0.1 for
    everyone.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        limit: int,
        window_seconds: float = 60.0,
        max_keys: int = 50_000,
    ) -> None:
        self.app = app
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        key = client_ip(scope)
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())

        while hits and now - hits[0] > self.window_seconds:
            hits.popleft()

        if len(hits) >= self.limit:
            response = PlainTextResponse("Too Many Requests", status_code=429)
            await response(scope, receive, send)
            return

        hits.append(now)
        self._bound_memory(now)
        await self.app(scope, receive, send)

    def _bound_memory(self, now: float) -> None:
        """Keep the bucket table from growing without limit."""
        if len(self._hits) <= self.max_keys:
            return
        stale = [
            key
            for key, hits in self._hits.items()
            if not hits or now - hits[-1] > self.window_seconds
        ]
        for key in stale:
            del self._hits[key]
        if len(self._hits) > self.max_keys:
            # Last resort: forgetting counters is better than unbounded memory.
            self._hits.clear()


def request_label(path: str, method: str, body: bytes) -> str:
    """The telemetry label for a request: a route, plus the tool name if it was a call.

    A JSON-RPC body may contain an address the caller is checking. Only `method` and
    `params.name` are read; arguments are never stored.
    """
    if method != "POST" or not body:
        return path
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return path
    if not isinstance(payload, dict) or payload.get("method") != "tools/call":
        return path
    params = payload.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    if isinstance(name, str) and name and name.replace("_", "").isalnum():
        return f"{path}/tools/call/{name}"[:200]
    return f"{path}/tools/call"


async def _captured(receive: Receive) -> tuple[bytes, Receive]:
    """Read the request body once and hand the app a replayable `receive`."""
    messages: list[Message] = []
    total = 0
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            break
        total += len(message.get("body", b""))
        if not message.get("more_body", False) or total > MAX_RECORDED_BODY:
            break

    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.request"
    )[:MAX_RECORDED_BODY]

    async def replay() -> Message:
        if messages:
            return messages.pop(0)
        return await receive()

    return body, replay


class UsageRecord:
    """Record the MCP endpoint in the same `usage.db` the score API writes.

    The store is opened on the first request, not at import: importing this module in
    a test run must not open (or create) a developer's telemetry database.
    """

    def __init__(self, app: ASGIApp, store: UsageRecorder | None = None) -> None:
        self.app = app
        self._store = store
        self._ready = store is not None
        self._errors: set[type[BaseException]] = set()

    def _usage(self) -> UsageRecorder | None:
        if not self._ready:
            self._store = store_from_env()
            self._ready = True
        return self._store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        store = self._usage()
        if scope["type"] != "http" or store is None:
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        body, replay = await _captured(receive)
        status = {"code": 500}

        async def capture_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = int(message["status"])
            await send(message)

        await self.app(scope, replay, capture_send)

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        try:
            store.record(
                at=now_seconds(),
                path=request_label(
                    str(scope.get("path", "/mcp")), str(scope.get("method", "")), body
                ),
                status=status["code"],
                latency_ms=(time.perf_counter() - started) * 1000,
                user_agent=headers.get("user-agent"),
                ip=client_ip(scope),
            )
        except Exception as exc:  # noqa: BLE001 - telemetry must never fail a request
            if type(exc) not in self._errors:
                self._errors.add(type(exc))
                logger.warning("MCP usage telemetry write failed (%s)", type(exc).__name__)


def host_patterns(raw: str) -> list[str]:
    """Expand `a.example,10.0.0.1` into exact and wildcard-port Host patterns."""
    patterns: list[str] = []
    for item in raw.split(","):
        host = item.strip()
        if not host:
            continue
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        patterns.extend([host, f"{host}:*"])
    return patterns


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def create_app(
    *,
    allowed_hosts: str | None = None,
    max_sessions: int | None = None,
    rate_limit: int | None = None,
    path: str | None = None,
    usage_store: UsageRecorder | None = None,
    stateless: bool | None = None,
    json_response: bool | None = None,
) -> ASGIApp:
    """Build the remote MCP app. Arguments default to the environment.

    `ZAPPROOF_MCP_STATELESS=1` and `ZAPPROOF_MCP_JSON_RESPONSE=1` make every request
    self-contained and avoid an SSE stream, which is what a serverless host with
    several instances needs: no session affinity, no open connection held between
    calls. The local and VPS defaults stay stateful so existing behaviour is
    unchanged.
    """
    hosts = allowed_hosts or os.environ.get("ZAPPROOF_MCP_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS)
    sessions = max_sessions or int(os.environ.get("ZAPPROOF_MCP_MAX_SESSIONS", "200"))
    limit = rate_limit or int(os.environ.get("ZAPPROOF_MCP_RATE_LIMIT", "120"))
    http_path = path or os.environ.get("ZAPPROOF_MCP_PATH", "/mcp")
    stateless_http = (
        stateless if stateless is not None else _env_flag("ZAPPROOF_MCP_STATELESS")
    )
    json_response = (
        json_response
        if json_response is not None
        else _env_flag("ZAPPROOF_MCP_JSON_RESPONSE")
    )

    starlette_app = server.streamable_http_app(
        streamable_http_path=http_path,
        json_response=json_response,
        stateless_http=stateless_http,
        max_sessions=sessions,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=host_patterns(hosts),
        ),
    )
    # Telemetry is outermost so a 429 is recorded too; otherwise the caller count
    # silently omits exactly the abusive traffic an operator needs to see.
    limited = ClientRateLimit(starlette_app, limit=limit)
    recorded = UsageRecord(limited, usage_store)
    # The wrappers hide the Starlette router; a host that combines this app with
    # another (repindex.hosted) needs the session-manager lifespan to run.
    recorded.lifespan_context = starlette_app.router.lifespan_context  # type: ignore[attr-defined]
    return recorded


app = create_app()


def main() -> None:
    """Run the remote endpoint with the flags the docstring requires."""
    import uvicorn

    uvicorn.run(
        "repindex.mcp.http_app:app",
        host=os.environ.get("ZAPPROOF_MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("ZAPPROOF_MCP_PORT", "8001")),
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("ZAPPROOF_MCP_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
