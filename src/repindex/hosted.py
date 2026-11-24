"""One ASGI app for a serverless host: REST API and remote MCP on the same origin.

Cloud Run (and any scale-to-zero host) is simpler with one service and one URL:
`/v1/...` is the score API, `/mcp` is the Streamable HTTP MCP endpoint, and
`/skill.md` is served by the API. Firebase Hosting can put one stable domain in
front of the site; the API itself stays on the Cloud Run URL so per-client rate
limits and caller counts see the real address.

Starlette's `Mount` matches `/mcp/...` but not the bare `/mcp` that MCP clients use,
so this module dispatches on the path directly. It also owns the lifespan: a plain
mount does not run the mounted app's lifespan, and the MCP app needs its session
manager started there.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import cast

from starlette.types import ASGIApp, Receive, Scope, Send

from repindex.api.app import app as api_app
from repindex.mcp.http_app import create_app as create_mcp_app

MCP_PATH = "/mcp"


@contextlib.asynccontextmanager
async def _no_lifespan() -> AsyncIterator[None]:
    yield


def _lifespan(app: ASGIApp) -> contextlib.AbstractAsyncContextManager[None]:
    """The app's own lifespan, or a no-op for an app that has none.

    `repindex.mcp.http_app.create_app` wraps its Starlette app in telemetry and
    rate-limit ASGI layers, so the lifespan is exposed on the wrapper itself.
    """
    direct = getattr(app, "lifespan_context", None)
    if direct is not None:
        return cast("contextlib.AbstractAsyncContextManager[None]", direct(app))
    router = getattr(app, "router", None)
    context = getattr(router, "lifespan_context", None)
    return (
        cast("contextlib.AbstractAsyncContextManager[None]", context(app))
        if context is not None
        else _no_lifespan()
    )


class HostedApp:
    """Dispatch HTTP by path and run both apps' lifespans once at startup."""

    def __init__(self, api: ASGIApp, mcp: ASGIApp, mcp_path: str = MCP_PATH) -> None:
        self.api = api
        self.mcp = mcp
        self.mcp_path = mcp_path.rstrip("/") or "/"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == self.mcp_path or path.startswith(self.mcp_path + "/"):
            await self.mcp(scope, receive, send)
            return
        await self.api(scope, receive, send)

    async def _lifespan(self, _: Scope, receive: Receive, send: Send) -> None:
        message = await receive()
        if message["type"] != "lifespan.startup":
            return

        async with contextlib.AsyncExitStack() as stack:
            try:
                await stack.enter_async_context(_lifespan(self.api))
                await stack.enter_async_context(_lifespan(self.mcp))
            except Exception as exc:  # noqa: BLE001 - report startup failure to the server
                await send({"type": "lifespan.startup.failed", "message": str(exc)})
                return
            await send({"type": "lifespan.startup.complete"})

            message = await receive()
            if message["type"] == "lifespan.shutdown":
                await stack.aclose()
                await send({"type": "lifespan.shutdown.complete"})


def create_app(
    *,
    api: ASGIApp | None = None,
    mcp: ASGIApp | None = None,
    mcp_path: str = MCP_PATH,
    **mcp_kwargs: object,
) -> HostedApp:
    """Compose the API and MCP apps. Arguments are for tests; production uses env."""
    api_application = api or api_app
    mcp_application = mcp or create_mcp_app(**mcp_kwargs)  # type: ignore[arg-type]
    return HostedApp(api_application, mcp_application, mcp_path)


app = create_app()


def main() -> None:
    """Run the combined app the way the container does (no access log)."""
    import os

    import uvicorn

    uvicorn.run(
        "repindex.hosted:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        access_log=False,
    )


if __name__ == "__main__":
    main()
