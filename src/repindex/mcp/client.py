"""Answers from a hosted Zapproof index.

An installed package has no `data/repindex.db` next to it, so `zapproof-mcp` calls a
hosted index when `ZAPPROOF_API_URL` is set. The mapping is lossy in one direction
only: a transport failure becomes `index_unavailable`, never an exception, and never a
score. Unknown stays unknown.

Status codes from `/v1/score/{identifier}`:
  200  the API's own answer, `found: true` or `found: false`
  422  malformed identifier -> the tool's `invalid_identifier`
  429  rate limited -> `index_unavailable` (the caller should retry, not conclude)
  5xx  -> `index_unavailable`
  404, other -> `index_unavailable`: a wrong base URL must not look like "no evidence"
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx

from repindex.api import queries
from repindex.api.models import DISCLAIMER_TEXT
from repindex.mcp.shape import correction_entry, fired_rules, operator_group_ids

DEFAULT_TIMEOUT_SECONDS = 10.0


def api_url_from_env() -> str | None:
    """The configured hosted index, if any. Empty means use the local database."""
    value = os.environ.get("ZAPPROOF_API_URL", "").strip()
    return value or None


def lookup(
    base_url: str,
    identifier: str,
    rail: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """One score lookup over HTTP. Never raises."""
    url = f"{base_url.rstrip('/')}/v1/score/{quote(identifier, safe='')}"
    try:
        response = httpx.get(url, timeout=timeout, headers={"accept": "application/json"})
    except httpx.HTTPError as exc:
        return _unavailable(f"hosted index request failed: {type(exc).__name__}: {exc}")

    if response.status_code == 422:
        return {
            "error": "invalid_identifier",
            "detail": _detail(response),
            "disclaimer": DISCLAIMER_TEXT,
        }
    if response.status_code != 200:
        return _unavailable(f"hosted index returned HTTP {response.status_code}")

    try:
        body = response.json()
    except ValueError:
        return _unavailable("hosted index returned a body that is not JSON")

    if not isinstance(body, dict) or "found" not in body:
        return _unavailable("hosted index returned an unexpected body")

    if body["found"] is True:
        return _found(body, rail)
    if body["found"] is False:
        return _not_found(body, rail)
    return _unavailable("hosted index returned a non-boolean `found`")


def _found(body: dict[str, Any], rail: str) -> dict[str, Any]:
    interpretation = body.get("interpretation") or {}
    contributions = interpretation.get("rules") or []
    labels = body.get("label_provenance") or []
    evidence = body.get("evidence") or {}
    fired = fired_rules(contributions)

    return {
        "found": True,
        "identifier": body.get("identifier"),
        "rail": rail,
        "evidence_rail": "base",
        "score": body.get("score"),
        "status": body.get("status"),
        "confidence_band": body.get("confidence_band"),
        "evidence_summary": {
            "settlements": evidence.get("settlements"),
            "counterparties": evidence.get("counterparties"),
            "active_days": evidence.get("active_days"),
            "window": evidence.get("window"),
        },
        "signaling_likelihood": interpretation.get("signaling_likelihood"),
        "judged": bool(interpretation.get("judged")),
        "rules_fired": fired,
        "flags": fired,
        "correction": correction_entry(contributions),
        "label_provenance": labels,
        "operator_groups": operator_group_ids(labels),
        "methodology_version": body.get("methodology_version") or queries.METHODOLOGY_VERSION,
        "as_of": evidence.get("as_of_block"),
        "disclaimer": body.get("disclaimer") or DISCLAIMER_TEXT,
    }


def _not_found(body: dict[str, Any], rail: str) -> dict[str, Any]:
    """Rebuild the tool's three-way not-found status from the API's flat shape.

    The API distinguishes the cases with `role_observed` and the settlement counts;
    the tool names them `unknown`, `not_processed`, and `no_seller_record`. Both say
    `found: false`, and none of them is a zero.
    """
    payer = body.get("payer_settlements")
    seller = body.get("seller_settlements")
    role = body.get("role_observed")

    payload: dict[str, Any] = {
        "found": False,
        "identifier": body.get("identifier"),
        "rail": rail,
        "evidence_rail": "base",
        "score": None,
        "flags": [],
        "correction": None,
        "methodology_version": queries.METHODOLOGY_VERSION,
        "disclaimer": body.get("disclaimer") or DISCLAIMER_TEXT,
    }

    if payer is None and seller is None:
        payload.update(
            status="unknown",
            note="No evidence in the index. Unknown is not bad, and this is not a zero.",
        )
        return payload

    if role is None:
        payload.update(
            status="not_processed",
            note=(
                "The index has seen this address sell, but no feature record exists "
                "for it yet. Not a low score — the record has not been processed."
            ),
            payer_settlements=payer,
            seller_settlements=seller,
        )
        return payload

    payload.update(
        status="no_seller_record",
        role_observed=role,
        payer_settlements=payer,
        seller_settlements=seller,
        note=(
            "Known to the index, but only outside the seller role. There is no seller "
            "record to score — not a low score, and not a statement about conduct."
        ),
    )
    return payload


def _unavailable(detail: str) -> dict[str, Any]:
    return {
        "error": "index_unavailable",
        "detail": detail,
        "disclaimer": DISCLAIMER_TEXT,
    }


def _detail(response: httpx.Response) -> str:
    """The API's error detail, or a truncated body when it is not JSON."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return response.text[:200]
