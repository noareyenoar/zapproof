"""Call Zapproof before an x402 payment is signed.

This is an integration example, not an SDK and not a gate. It registers one
before-payment hook, performs one read-only lookup of the address the agent is about
to pay, hands the answer to a callback, and always lets the payment proceed.

Three rules the code exists to keep:

1. **Fail open.** A timeout, a network error, a 5xx or an answer we do not understand
   returns ``None`` and the payment continues. Zapproof must never become a single
   point of failure for someone else's payment.
2. **No verdict.** The hook never returns an abort. There is no score threshold. The
   record is evidence, not approval, and "unknown" is not "bad".
3. **No payer data.** Only the payee address (``payTo``) is sent. No wallet address,
   signature, amount, resource URL, or request body.

Verified against the x402 Python client on 2026-11-24: the hook context exposes
``selected_requirements`` with ``pay_to`` and ``network`` (CAIP-2). If your installed
SDK version uses different names, read them from its types instead of trusting this
file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# The public index. Point base_url at a self-hosted deployment to keep lookups private.
DEFAULT_BASE_URL = "https://zapproof-hc75zyauja-uc.a.run.app"
DEFAULT_TIMEOUT_SECONDS = 1.5
USER_AGENT = "zapproof-x402-guard-example/0.1 (+https://github.com/noareyenoar/zapproof)"

# The index covers Base mainnet only. Anything else is skipped, not guessed at.
BASE_MAINNET = "eip155:8453"

_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_MAX_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True)
class GuardAnswer:
    """The part of a Zapproof answer this example is willing to log.

    ``status`` is ``"scored"``, ``"insufficient_evidence"``, or ``"unknown"`` (the
    address is not in the index as a seller). ``score`` is None unless status is
    ``"scored"``; None is never a zero.
    """

    found: bool
    identifier: str
    status: str
    score: float | None = None
    settlements: int | None = None
    counterparties: int | None = None
    active_days: int | None = None
    as_of_block: int | None = None
    methodology_version: str | None = None
    reason: str | None = None


def check(
    pay_to: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> GuardAnswer | None:
    """Look up one payee. Returns None when the evidence could not be obtained.

    Synchronous and dependency-free on purpose: it is easy to call from a thread, a
    test, or a CLI. The async hook below wraps it with ``asyncio.to_thread``.
    """
    if not isinstance(pay_to, str) or not _ADDRESS.fullmatch(pay_to):
        # Not an EVM address: do not send the string anywhere, and do not block.
        return None

    url = f"{base_url.rstrip('/')}/v1/score/{pay_to.lower()}"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            raw = response.read(_MAX_RESPONSE_BYTES)
    except Exception:
        # Timeout, DNS, TLS, connection reset, HTTP 4xx/5xx: fail open.
        return None

    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return _parse(payload, pay_to)


def attach_guard(
    client: Any,
    *,
    on_evidence: Callable[[GuardAnswer], None] | None = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Callable[[Any], Any]:
    """Register a fail-open lookup on an x402 client and return the hook.

    ``client`` is an ``x402`` client with ``on_before_payment_creation``. The hook
    runs before the payment payload is created or signed and always returns None,
    which means "continue". Returning an abort here would convert an observation into
    a gate — this example deliberately does not do that.
    """

    async def hook(context: Any) -> None:
        with contextlib.suppress(Exception):
            requirements = getattr(context, "selected_requirements", None)
            network = getattr(requirements, "network", None)
            pay_to = getattr(requirements, "pay_to", None)
            if network == BASE_MAINNET and isinstance(pay_to, str):
                answer = await asyncio.to_thread(
                    check, pay_to, base_url=base_url, timeout=timeout
                )
                if answer is not None and on_evidence is not None:
                    # A broken logger must not break a payment.
                    with contextlib.suppress(Exception):
                        on_evidence(answer)
        # Always proceed. This is your client; whether to act on the evidence is your
        # policy, not our verdict.
        return None

    client.on_before_payment_creation(hook)
    return hook


def format_evidence(answer: GuardAnswer) -> str:
    """One log line. No interpretation, no recommendation."""
    if not answer.found:
        reason = answer.reason or "no seller-side record in this index"
        return f"Zapproof {answer.identifier}: unknown — {reason} (not a low score)"
    if answer.status == "insufficient_evidence":
        return (
            f"Zapproof {answer.identifier}: insufficient_evidence — "
            f"settlements={answer.settlements} counterparties={answer.counterparties} "
            f"active_days={answer.active_days} as_of_block={answer.as_of_block} "
            f"(not bad; no score was issued)"
        )
    score = "null" if answer.score is None else f"{answer.score:.1f}"
    return (
        f"Zapproof {answer.identifier}: scored {score} — "
        f"settlements={answer.settlements} counterparties={answer.counterparties} "
        f"active_days={answer.active_days} as_of_block={answer.as_of_block} "
        f"methodology={answer.methodology_version} (one input, not an approval)"
    )


def _parse(payload: Any, pay_to: str) -> GuardAnswer | None:
    if not isinstance(payload, dict):
        return None

    identifier = payload.get("identifier")
    if not isinstance(identifier, str):
        identifier = pay_to.lower()

    if payload.get("found") is False:
        reason = payload.get("reason")
        return GuardAnswer(
            found=False,
            identifier=identifier,
            status="unknown",
            reason=reason if isinstance(reason, str) else None,
        )

    if payload.get("found") is not True:
        return None

    status = payload.get("status")
    if status not in ("scored", "insufficient_evidence"):
        # A status we do not understand is not evidence; fail open.
        return None

    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}

    return GuardAnswer(
        found=True,
        identifier=identifier,
        status=status,
        score=_score_or_none(payload.get("score")),
        settlements=_int_or_none(evidence.get("settlements")),
        counterparties=_int_or_none(evidence.get("counterparties")),
        active_days=_int_or_none(evidence.get("active_days")),
        as_of_block=_int_or_none(evidence.get("as_of_block")),
        methodology_version=(
            payload.get("methodology_version")
            if isinstance(payload.get("methodology_version"), str)
            else None
        ),
    )


def _score_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
