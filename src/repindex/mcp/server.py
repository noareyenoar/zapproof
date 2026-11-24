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

from repindex import adjudication, snapshot
from repindex.api import queries
from repindex.api.identifiers import InvalidIdentifier, normalise_identifier
from repindex.api.models import DISCLAIMER_TEXT
from repindex.mcp import client as hosted
from repindex.mcp.shape import score_answer

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

RAIL
  Only Base evidence is indexed. rail="lightning" returns status
  rail_not_indexed — never Base data relabelled as Lightning.

READING THE ANSWER
  found: false              no seller-side score. This is NOT a low score and NOT a
                            warning. role_observed tells you whether the index has
                            seen the address pay others; that is not a verdict.
  insufficient_evidence     some evidence, too little to score. Also not bad.
  signaling_likelihood      null when not judged. Null is not zero.
  score (0-100)             only present when there is enough evidence.
"""

server = MCPServer(
    "zapproof",
    title="Zapproof",
    version="0.1.0",
    instructions=(
        "A read-only reputation index for AI agents, scored from evidence of settled "
        "payments. Unknown is not bad."
    ),
)


@server.tool(
    name="check_counterparty",
    description=TOOL_DESCRIPTION,
)
def check_counterparty(
    identifier: str,
    rail: Literal["base", "lightning", "any"] = "any",
) -> dict[str, Any]:
    """Look up one identifier. Returns compact JSON, never raises for a bad input."""
    if rail not in ("base", "lightning", "any"):
        return {
            "error": "invalid_rail",
            "detail": "rail must be one of: base, lightning, any",
            "disclaimer": DISCLAIMER_TEXT,
        }

    try:
        normalised = normalise_identifier(identifier)
    except InvalidIdentifier as exc:
        return {
            "error": "invalid_identifier",
            "detail": str(exc),
            "disclaimer": DISCLAIMER_TEXT,
        }

    # The index currently holds Base evidence only. Asking for the lightning rail must
    # NOT return Base data relabelled as lightning — an agent would read it as
    # Lightning-side reputation that does not exist. This was the case before the
    # independent review caught it: `rail` was validated and then echoed back, so
    # rail="lightning" returned a Base score tagged lightning.
    if rail == "lightning":
        return {
            "found": False,
            "identifier": normalised,
            "rail": rail,
            "evidence_rail": None,
            "score": None,
            "status": "rail_not_indexed",
            "note": (
                "No Lightning evidence is indexed yet. This is not a low score, and it is "
                "not a statement about the counterparty: this rail simply is not covered."
            ),
            "flags": [],
            "correction": None,
            "methodology_version": queries.METHODOLOGY_VERSION,
            "disclaimer": DISCLAIMER_TEXT,
        }

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


def _not_found(identifier: str, roles: dict[str, Any] | None, rail: str) -> dict[str, Any]:
    """`found: false` that does not claim the index knows nothing (F13)."""
    body: dict[str, Any] = {
        "found": False,
        "identifier": identifier,
        "rail": rail,
        "evidence_rail": "base",
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
        return body

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
        return body

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
    return body


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
