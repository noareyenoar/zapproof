"""FastAPI application for the Zapproof score API.

Read-only by construction: every score-database connection is opened `mode=ro`, and
there is no route that mutates anything. Gate check GB.4 asserts both. Usage telemetry
is middleware, not a route, and writes a separate database (`usage.py`); the README and
deployment guide say so rather than claiming the process never writes.

Every response carries a disclaimer. The owner chose on 2026-10-30 to expose the score
while blinded adjudication put agreement at 61.5%, so the number and its limitation
travel together — see gate check GB.12.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from repindex import adjudication
from repindex.api import queries
from repindex.api.identifiers import InvalidIdentifier, normalise_identifier
from repindex.api.models import (
    DISCLAIMER_TEXT,
    EntityResponse,
    Evidence,
    HealthResponse,
    Interpretation,
    LabelProvenance,
    MethodologyResponse,
    NotFoundResponse,
    ScoreResponse,
    Window,
)
from repindex.api.usage import (
    UsageRecorder,
    client_ip,
    now_seconds,
    route_template,
    store_from_env,
)

# Requests per minute per client address.
#
# An agent choosing between candidates may check many counterparties before paying
# one, so the ceiling has to sit well above the project's own 20 rps target: at 120/min
# the limiter capped the load test at 2 rps, which measured the limiter rather than the
# service. 3,000/min is 50 rps — headroom for a burst, still a hard stop for a runaway
# loop against a single read-only connection. Overridable for a deployment through
# `RATE_LIMIT_PER_MINUTE`; 0 disables the limiter and exists for tests only.
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "3000"))

# The A2A card is served only when a real, v1.0-conformant JSON-RPC endpoint exists.
# The static card still points at `zapproof.example` and cannot answer `message/send`,
# so serving it by default would advertise a service that does not work. Gate B checks
# the file's presence; this flag controls the route.
A2A_CARD_ENV = "ZAPPROOF_A2A_CARD"

# OpenAPI `servers` is populated only when the deployment knows its public URL. An
# empty list is honest; a placeholder host would publish an endpoint that cannot answer.
PUBLIC_URL_ENV = "ZAPPROOF_PUBLIC_URL"
PUBLIC_URL = os.environ.get(PUBLIC_URL_ENV, "").rstrip("/")

@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Open the telemetry store at startup so a bad path is reported at boot (G4)."""
    _usage_store()
    yield


app = FastAPI(
    title="Zapproof",
    version="0.1.0",
    description=(
        "A read-only reputation index for AI agents, scored from evidence of settled "
        "payments. Unknown is not bad: an identifier with no evidence returns "
        "found: false, never a score of zero. "
        "Agent instructions, including the x402 402-pay-retry loop, are at /skill.md; "
        "a Thai translation is at /skill.th.md."
    ),
    lifespan=lifespan,
    servers=[{"url": PUBLIC_URL, "description": "Public API"}] if PUBLIC_URL else None,
    contact={
        "name": "Zapproof",
        # The repository is private during launch; do not publish a 404 link from
        # the OpenAPI document.
        "url": "https://zapproof.web.app",
    },
    license_info={"name": "MIT", "identifier": "MIT"},
)


@app.exception_handler(StarletteHTTPException)
async def http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Every error response carries the disclaimer too.

    GB.12 says *every* response. Before this handler existed, 404, 405, 422 and 429
    answered with FastAPI's bare `{"detail": ...}` and no marker — the independent
    review found four response classes without it while the gate's sweep sampled four
    200s and passed. The gate was checking the easy half.
    """
    body = exc.detail if isinstance(exc.detail, dict) else {"detail": exc.detail}
    body.setdefault("disclaimer", DISCLAIMER_TEXT)
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def request_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's own 422s bypass the Starlette handler above.

    The review found `/v1/entity/abc` returning a bare `{"detail": [...]}` with no
    disclaimer. The `StarletteHTTPException` handler never sees this class, so the
    first fix covered only the errors raised by our own code. This closes the rest.
    """
    return JSONResponse(
        status_code=422,
        content={
            "detail": jsonable_encoder(exc.errors()),
            "disclaimer": DISCLAIMER_TEXT,
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception(_: Request, exc: Exception) -> JSONResponse:
    """Never leak a stack trace. Gate check GB.4 tests this."""
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "detail": "the request could not be completed",
            "disclaimer": DISCLAIMER_TEXT,
        },
    )


_rate_buckets: dict[str, deque[float]] = defaultdict(deque)


def _rate_limited(client: str) -> bool:
    # 0 (or a negative value) disables the limiter. The comparison below used to run
    # anyway, so `0` denied every request — the documented off switch took the whole
    # API down. The second independent review found it live (G3).
    if RATE_LIMIT_PER_MINUTE <= 0:
        return False
    now = time.monotonic()
    bucket = _rate_buckets[client]
    while bucket and now - bucket[0] > 60:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_PER_MINUTE:
        return True
    bucket.append(now)
    return False


def _client(request: Request) -> str:
    return client_ip(request.scope)


@app.middleware("http")
async def rate_limit(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Apply the per-IP limit to every /v1 route, not only the score route.

    F6: /v1/health, /v1/methodology and /v1/entity had no limit at all while task B1
    requires per-IP rate limiting. A middleware cannot raise HTTPException into the
    handlers (Starlette's ExceptionMiddleware sits inside user middleware), so it
    returns the 429 directly — with the disclaimer, as GB.12 requires.
    """
    if request.url.path.startswith("/v1/") and _rate_limited(_client(request)):
        return JSONResponse(
            status_code=429,
            content={
                "detail": "rate limit exceeded",
                "disclaimer": DISCLAIMER_TEXT,
            },
        )
    return await call_next(request)


logger = logging.getLogger(__name__)

_USAGE_STORE: UsageRecorder | None = None
_USAGE_READY = False

# H4: a failed telemetry write used to be suppressed without a trace, so an operator
# could not tell "no callers" from "telemetry is broken". Each exception class is
# logged once: a full disk must not flood the log on every request.
_USAGE_WRITE_ERRORS: set[type[BaseException]] = set()


def _usage_store() -> UsageRecorder | None:
    """Open the telemetry store once. None means disabled or unavailable."""
    global _USAGE_STORE, _USAGE_READY
    if not _USAGE_READY:
        _USAGE_STORE = store_from_env()
        _USAGE_READY = True
    return _USAGE_STORE


@app.middleware("http")
async def record_usage(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Record route template, status, latency, agent class and a hashed IP (B7).

    Added after the rate limiter, so it is the outermost middleware and sees 429s too.
    A failure to record never breaks a score response and the caller never learns
    whether logging worked, but the failure itself is logged once per exception class
    so an operator can distinguish a quiet service from a broken store (H4).
    """
    started = time.perf_counter()
    response = await call_next(request)

    store = _usage_store()
    if store is not None:
        try:
            store.record(
                at=now_seconds(),
                path=route_template(request.scope.get("route")),
                status=response.status_code,
                latency_ms=(time.perf_counter() - started) * 1000,
                user_agent=request.headers.get("user-agent"),
                ip=_client(request),
            )
        except Exception as exc:  # noqa: BLE001 - telemetry must never fail a request
            error_class = type(exc)
            if error_class not in _USAGE_WRITE_ERRORS:
                _USAGE_WRITE_ERRORS.add(error_class)
                logger.warning(
                    "usage telemetry write failed (%s: %s); score responses are "
                    "unaffected. Further %s failures will not be logged.",
                    error_class.__name__,
                    exc,
                    error_class.__name__,
                )
    return response


def _etag(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return '"' + hashlib.sha256(canonical.encode()).hexdigest()[:32] + '"'


def _respond(payload: dict[str, Any], request: Request) -> Response:
    """Attach an ETag, honour If-None-Match, and never cache stale scores."""
    tag = _etag(payload)
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers={"ETag": tag})
    return JSONResponse(
        content=payload,
        headers={"ETag": tag, "Cache-Control": "public, max-age=60"},
    )


def _not_found(identifier: str, roles: dict[str, Any] | None) -> dict[str, Any]:
    """`found: false` without claiming the index knows nothing about the address (F13).

    The product scores sellers. An address that has only ever paid is therefore
    `found: false` — but saying "no evidence for this identifier" about an address
    with thousands of observed payments is false, and the review caught it with one.
    """
    if roles is None:
        return NotFoundResponse(identifier=identifier).model_dump()

    if roles["is_seller"]:
        return NotFoundResponse(
            identifier=identifier,
            reason="no seller-side score has been derived for this identifier yet",
            note=(
                "The index has seen this address sell, but no feature record exists "
                "for it. This is not a low score; the record has not been processed."
            ),
            payer_settlements=roles["payer_settlements"],
            seller_settlements=roles["seller_settlements"],
        ).model_dump()

    if roles["is_payer"] and roles["is_relayer"]:
        role: Literal["payer", "relayer", "payer_and_relayer"] | None = "payer_and_relayer"
    elif roles["is_payer"]:
        role = "payer"
    elif roles["is_relayer"]:
        role = "relayer"
    else:
        role = None

    return NotFoundResponse(
        identifier=identifier,
        reason="no seller-side evidence for this identifier",
        note=(
            "This address is known to the index, but only outside the seller role. "
            "There is no seller record to score — that is not a low score, and not a "
            "statement about the address's conduct."
        ),
        role_observed=role,
        payer_settlements=roles["payer_settlements"],
        seller_settlements=roles["seller_settlements"],
    ).model_dump()


def _validate_identifier(identifier: str) -> str:
    """Return the lowercase address for an EIP-55 address or a supported DID."""
    try:
        return normalise_identifier(identifier)
    except InvalidIdentifier as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def public_base(request: Request) -> str:
    """The absolute base URL this response should name.

    Behind a proxy the scheme is only correct when the forwarded header is honoured.
    `ZAPPROOF_PUBLIC_URL` is authoritative when a deployment knows its public URL;
    otherwise the request's own host is used, which is right for a direct connection.
    """
    if PUBLIC_URL:
        return PUBLIC_URL
    proto = request.headers.get("x-forwarded-proto", "").split(",")[-1].strip()
    scheme = proto if proto in ("http", "https") else request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


def _skill_response(name: str, request: Request) -> Response:
    """Serve one agent skill with this deployment's base URL substituted.

    One source file is both the repository copy and what an agent fetches over HTTP.
    The token form keeps a developer checkout from naming an endpoint that is not
    deployed; the substitution happens per request here.
    """
    path = Path(__file__).parent / "static" / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="skill not built")
    text = path.read_text(encoding="utf-8").replace("__API_URL__", public_base(request))
    return Response(
        content=text,
        media_type="text/markdown; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/skill.md", include_in_schema=False)
def skill_md(request: Request) -> Response:
    """The English agent skill."""
    return _skill_response("skill.md", request)


@app.get("/skill.th.md", include_in_schema=False)
def skill_th_md(request: Request) -> Response:
    """The Thai agent skill; machine-readable fields stay in English."""
    return _skill_response("skill.th.md", request)


@app.get("/.well-known/agent-card.json", include_in_schema=False)
def agent_card() -> Response:
    """Serve the A2A agent card when explicitly enabled.

    Its validation status is UNVERIFIED and its `url` is a placeholder — see
    docs/a2a_notes.md. Publishing it before a conformant JSON-RPC endpoint exists
    would advertise a service that cannot answer.
    """
    if os.environ.get(A2A_CARD_ENV, "0") != "1":
        raise HTTPException(status_code=404, detail="agent card is not published")
    card = Path(__file__).parent / "static" / "agent-card.json"
    if not card.is_file():
        raise HTTPException(status_code=404, detail="agent card not built")
    return Response(
        content=card.read_text(encoding="utf-8"),
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/v1/health", response_model=HealthResponse)
def health() -> HealthResponse:
    conn = queries.connect()
    try:
        return HealthResponse(
            as_of_block=queries.as_of_block(conn),
            sellers_indexed=queries.sellers_indexed(conn),
            methodology_version=queries.METHODOLOGY_VERSION,
        )
    finally:
        conn.close()


@app.get("/v1/methodology", response_model=MethodologyResponse)
def methodology() -> MethodologyResponse:
    return MethodologyResponse(
        version=queries.METHODOLOGY_VERSION,
        summary=(
            "Sellers are scored from observed payment records. The evidence is exact; "
            "the interpretation is rules-based and was compared against a blinded "
            "adjudication whose limits are listed in adjudication_caveats."
        ),
        signals=[
            "distinct payer count relative to settlement count",
            "reciprocal payments between payer and seller",
            "amount entropy across settlements",
            "concentration of activity in time",
        ],
        not_signals=[
            "repeat buyers (a successful business has them)",
            "uniform amounts when payers are diverse (a posted price)",
            "a single relayer (facilitators serve many sellers)",
            "operator-group membership (a hypothesis, not a fact)",
        ],
        agreement_with_blinded_readers=queries.AGREEMENT_WITH_BLINDED_READERS,
        adjudication_caveats=[
            "The adjudicators were language models, not humans (owner decision "
            "2026-10-28). They share training data and priors with the author of the "
            "classifier.",
            adjudication.agreement_caveat(),
            "The two inter-rater verdict files are identical, so the recorded 100% "
            "inter-rater agreement compares a file with itself. It is not evidence of "
            "independent model agreement.",
            "No model names, prompts or run metadata were recorded, so the "
            "adjudication cannot be reproduced from the repository.",
        ],
        limitation=adjudication.limitation_text(),
    )


@app.get(
    "/v1/score/{identifier}",
    response_model=ScoreResponse | NotFoundResponse,
    response_model_exclude_none=False,
)
def score(identifier: str, request: Request) -> Response:
    normalised = _validate_identifier(identifier)
    conn = queries.connect()
    try:
        data = queries.fetch_score(conn, normalised)
        roles = None if data is not None else queries.fetch_observed_roles(conn, normalised)
    finally:
        conn.close()

    if data is None:
        return _respond(_not_found(normalised, roles), request)

    payload = ScoreResponse(
        identifier=data["identifier"],
        status=data["status"],
        score=data["score"],
        confidence_band=data["confidence_band"],
        evidence=Evidence(
            settlements=data["settlements"],
            counterparties=data["counterparties"],
            active_days=data["active_days"],
            window=Window(**data["window"]),
            as_of_block=data["as_of_block"],
        ),
        interpretation=Interpretation(
            signaling_likelihood=data["signaling_likelihood"],
            judged=data["status"] == "scored",
            rules=data["rules"],
            methodology_version=queries.METHODOLOGY_VERSION,
        ),
        label_provenance=[LabelProvenance(**label) for label in data["labels"]],
        methodology_version=queries.METHODOLOGY_VERSION,
    ).model_dump()

    return _respond(payload, request)


@app.get("/v1/entity/{group_id}", response_model=EntityResponse)
def entity(group_id: int) -> EntityResponse:
    # SQLite binds Python ints as 64-bit and raises OverflowError beyond that, which
    # surfaced as a 500. The independent review found it with a 38-digit id. An
    # out-of-range integer is a bad request, not a server fault.
    if not (0 <= group_id <= 2**63 - 1):
        raise HTTPException(status_code=422, detail="group_id is out of range")

    conn = queries.connect()
    try:
        data = queries.fetch_entity(conn, group_id)
    finally:
        conn.close()

    if data is None:
        raise HTTPException(status_code=404, detail="no such operator group")
    return EntityResponse(**data)
