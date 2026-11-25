"""MCP server exposing one tool: `check_counterparty` (prompt task B2).

The tool description is written for an agent reading it mid-task, so it states plainly
what the score means, what it does not mean, and — the part agents get wrong — that
`found: false` and `insufficient_evidence` are not negative signals.

Run:  uv run python -m repindex.mcp.server
"""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from repindex import adjudication, snapshot
from repindex.api import queries
from repindex.api.identifiers import (
    KIND_EVM,
    KIND_SOLANA,
    RAIL_BY_KIND,
    InvalidIdentifier,
    classify_identifier,
)
from repindex.api.models import DISCLAIMER_TEXT
from repindex.mcp import client as hosted
from repindex.mcp.shape import score_answer, seal

TOOL_DESCRIPTION = f"""\
Check the payment-backed reputation of a counterparty before paying it.

WHAT THIS MEANS
  Returns facts about observed settled payments: how many settlements, how many
  distinct addresses paid, over how many days, and a rules-based interpretation of
  whether that record looks like independent commerce.

WHAT THIS DOES NOT MEAN
  It is not a judgement about whether anyone is honest, and the interpretation is not
  independently validated. {adjudication.agreement_sentence()}
  The adjudicators were language models, not humans, and their two verdict files were
  identical. Use it as one input among several, never as an approval to pay.

IDENTIFIER
  An EIP-55 address, did:ethr:<address>, did:ethr:8453:<address>, or
  did:pkh:eip155:8453:<address>. A DID qualified with any other chain is rejected:
  the index holds Base evidence only and will not answer a foreign-chain DID with it.
  A base58 Solana address is routed to the Solana index, a separate and shorter
  observation window; if that index is not deployed the answer is status
  rail_not_indexed, not invalid_identifier.
  A Lightning node pubkey (66 hex), a Lightning address (user@domain) or an mpp:<id>
  operator is well-formed but not indexed: it returns status rail_not_indexed, not
  invalid_identifier.

RAIL
  Every answer comes from its own rail's evidence. Base is indexed; the Solana x402
  rail is indexed from a seven-day observation window, and each Solana answer states
  that window. rail="lightning" returns status rail_not_indexed — never
  Base data relabelled as Lightning. Asking for "base" about a Solana address (or
  "solana" about a Base address) returns rail_mismatch rather than a guess.

READING THE ANSWER
  found: false              no seller-side score. This is NOT a low score and NOT a
                            warning. role_observed tells you whether the index has
                            seen the address pay others; that is not a verdict.
  insufficient_evidence     some evidence, too little to score. Also not bad.
  signaling_likelihood      null when not judged. Null is not zero.
  score (0-100)             only present when there is enough evidence.
  evidence_hash             fingerprint of the answer. Store it with the record and
                            compare on re-fetch to see whether the evidence changed.
                            Unsigned: it is not an attestation, and this answer is
                            never an endorsement to pay.
"""

SERVER_INSTRUCTIONS = (
    "Use Zapproof before paying an unfamiliar EVM (0x…) or Solana address, or when an "
    "x402 seller asks to be trusted. It returns evidence of settled payments and a "
    "rules-based interpretation — not an approval to pay.\n"
    "\n"
    "Read the status fields first: found:false and insufficient_evidence mean unknown, "
    "not bad, and must never be turned into a low score. score and "
    "signaling_likelihood come only from settled-payment evidence on the asked rail; a "
    "rail with no indexed evidence returns rail_not_indexed and is never answered with "
    "another rail's data. evidence_hash identifies the answer; it is unsigned and is "
    "not an attestation."
)

server = MCPServer(
    "zapproof",
    title="Zapproof",
    version="0.1.3",
    instructions=SERVER_INSTRUCTIONS,
)


@server.tool(
    name="check_counterparty",
    title="Check counterparty",
    description=TOOL_DESCRIPTION,
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
def check_counterparty(
    identifier: str,
    rail: Literal["base", "solana", "lightning", "any"] = "any",
) -> dict[str, Any]:
    """Look up one identifier. Returns compact JSON, never raises for a bad input."""
    if rail not in ("base", "solana", "lightning", "any"):
        return {
            "error": "invalid_rail",
            "detail": "rail must be one of: base, solana, lightning, any",
            "disclaimer": DISCLAIMER_TEXT,
        }

    try:
        kind, normalised = classify_identifier(identifier)
    except InvalidIdentifier as exc:
        return {
            "error": "invalid_identifier",
            "detail": str(exc),
            "disclaimer": DISCLAIMER_TEXT,
        }

    if kind == KIND_SOLANA:
        if rail == "base":
            return _rail_mismatch(normalised, identifier_rail="solana", asked=rail)
        return _check_solana(normalised, rail)

    # A well-formed identifier on a rail with no index is "missing coverage", never
    # "bad input" and never a Base answer. This covers Lightning node pubkeys and
    # Lightning addresses (the pi402/Aperture population) and mpp:<operator> ids.
    if kind != KIND_EVM:
        return _rail_not_indexed(
            normalised, rail=RAIL_BY_KIND.get(kind, "unknown"), kind=kind
        )

    if rail == "solana":
        return _rail_mismatch(normalised, identifier_rail="base", asked=rail)

    # The index currently holds Base evidence only. Asking for the lightning rail must
    # NOT return Base data relabelled as lightning — an agent would read it as
    # Lightning-side reputation that does not exist. This was the case before the
    # independent review caught it: `rail` was validated and then echoed back, so
    # rail="lightning" returned a Base score tagged lightning.
    if rail == "lightning":
        return _rail_not_indexed(normalised, rail=rail, kind=None)

    # An installed package has no database beside it. `ZAPPROOF_API_URL` points the
    # tool at a hosted index; the answer is shaped identically (see mcp/client.py).
    api_url = hosted.api_url_from_env()
    if api_url:
        return hosted.lookup(api_url, normalised, rail)

    # No database beside an installed package: use the bundled snapshot when there is
    # one. A snapshot is sellers-only and point-in-time; its answers say so.
    if not queries.resolve_db_path().is_file():
        snap = snapshot.default_path()
        if snap is not None:
            return snapshot.answer(snap, normalised, rail)

    # F16: the connect used to sit outside this try, so a missing database raised
    # instead of returning the `index_unavailable` answer the tool promises an agent.
    conn: sqlite3.Connection | None = None
    try:
        conn = queries.connect()
        data = queries.fetch_score(conn, normalised)
        roles = None if data is not None else queries.fetch_observed_roles(conn, normalised)
    except (FileNotFoundError, sqlite3.Error) as exc:
        return {
            "error": "index_unavailable",
            "detail": str(exc),
            "disclaimer": DISCLAIMER_TEXT,
        }
    finally:
        if conn is not None:
            conn.close()

    if data is None:
        return _not_found(normalised, roles, rail)

    # I2: a `dispute_override` entry is a correction, not a classifier rule. The
    # shaping helper reports it separately, so an agent cannot read a human decision
    # as a pattern the classifier detected.
    return score_answer(data, rail)


def _check_solana(identifier: str, rail: str) -> dict[str, Any]:
    """Answer a Solana address from the hosted API or the local Solana index."""
    api_url = hosted.api_url_from_env()
    if api_url:
        return hosted.lookup(api_url, identifier, rail)

    try:
        conn = queries.connect_solana()
    except FileNotFoundError:
        return _rail_not_indexed(identifier, rail="solana", kind=KIND_SOLANA)

    try:
        data = queries.fetch_score(conn, identifier)
        roles = None if data is not None else queries.fetch_observed_roles(conn, identifier)
        coverage = queries.solana_coverage(conn)
    except sqlite3.Error as exc:
        return {
            "error": "index_unavailable",
            "detail": str(exc),
            "disclaimer": DISCLAIMER_TEXT,
        }
    finally:
        conn.close()

    if data is None:
        return _not_found(identifier, roles, rail="solana", coverage=coverage)
    return score_answer(data, rail, evidence_rail="solana", coverage=coverage)


def _rail_mismatch(identifier: str, *, identifier_rail: str, asked: str) -> dict[str, Any]:
    """The identifier and the requested rail disagree; do not guess which one wins."""
    return {
        "error": "rail_mismatch",
        "identifier": identifier,
        "identifier_rail": identifier_rail,
        "requested_rail": asked,
        "detail": (
            f"This identifier is a {identifier_rail} address, but rail={asked!r} was "
            "requested. Use rail='any' or the identifier's own rail."
        ),
        "disclaimer": DISCLAIMER_TEXT,
    }


def _rail_not_indexed(
    identifier: str, *, rail: str, kind: str | None
) -> dict[str, Any]:
    """Answer for a well-formed identifier on a rail with no index.

    `found` is false and `score` is null, but the status names missing coverage. It
    never carries Base evidence: a Lightning node key is a different subject from any
    Base address, and an empty answer is the honest one.
    """
    return seal(
        {
            "found": False,
            "identifier": identifier,
            "identifier_type": kind,
            "rail": rail,
            "evidence_rail": None,
            "coverage": None,
            "indexed": False,
            "score": None,
            "status": "rail_not_indexed",
            "note": (
                f"No {rail} evidence is indexed yet. This is not a low score, and it is "
                "not a statement about the counterparty: this rail simply is not covered."
            ),
            "flags": [],
            "correction": None,
            "methodology_version": queries.METHODOLOGY_VERSION,
            "disclaimer": DISCLAIMER_TEXT,
        }
    )


def _not_found(
    identifier: str,
    roles: dict[str, Any] | None,
    rail: str,
    *,
    coverage: str | None = None,
) -> dict[str, Any]:
    """`found: false` that does not claim the index knows nothing (F13)."""
    body: dict[str, Any] = {
        "found": False,
        "identifier": identifier,
        "rail": rail,
        "evidence_rail": "solana" if rail == "solana" else "base",
        "coverage": coverage,
        "score": None,
        "flags": [],
        "correction": None,
        "methodology_version": queries.METHODOLOGY_VERSION,
        "disclaimer": DISCLAIMER_TEXT,
    }

    if roles is None:
        body.update(
            status="unknown",
            note="No evidence in the index. Unknown is not bad, and this is not a zero.",
        )
        return seal(body)

    if roles["is_seller"]:
        body.update(
            status="not_processed",
            note=(
                "The index has seen this address sell, but no feature record exists for "
                "it yet. Not a low score — the record has not been processed."
            ),
            payer_settlements=roles["payer_settlements"],
            seller_settlements=roles["seller_settlements"],
        )
        return seal(body)

    if roles["is_payer"] and roles["is_relayer"]:
        role: str | None = "payer_and_relayer"
    elif roles["is_payer"]:
        role = "payer"
    elif roles["is_relayer"]:
        role = "relayer"
    else:
        role = None

    body.update(
        status="no_seller_record",
        role_observed=role,
        payer_settlements=roles["payer_settlements"],
        seller_settlements=roles["seller_settlements"],
        note=(
            "Known to the index, but only outside the seller role. There is no seller "
            "record to score — not a low score, and not a statement about conduct."
        ),
    )
    return seal(body)


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
