"""Field shaping shared by the local-DB and hosted-API answer paths.

`check_counterparty` must return the same keys and the same status vocabulary whether
the record came from `data/repindex.db` or from a hosted index. These helpers are
small, but a second hand-written copy is exactly how the two paths drift apart.
"""

from __future__ import annotations

from typing import Any

from repindex.api import queries
from repindex.api.models import DISCLAIMER_TEXT, SCHEMA_VERSION
from repindex.classify import rules as rule_defs
from repindex.common.canonical import evidence_hash


def fired_rules(contributions: list[dict[str, Any]]) -> list[str]:
    """Only real classifier rules.

    A `dispute_override` is a correction, not a rule (review finding I2). Listing it
    among the rules made an agent read a human decision as a detected pattern.
    """
    return [
        str(row["rule"])
        for row in contributions
        if row.get("fired") and row.get("rule") in rule_defs.RULE_NAMES
    ]


def correction_entry(contributions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The correction that replaced part of the computed answer, if any."""
    return next((row for row in contributions if row.get("rule") == "dispute_override"), None)


def operator_group_ids(labels: list[dict[str, Any]]) -> list[Any]:
    """Operator-group ids carried on labels. Not a verdict — a hypothesis."""
    return [
        label["entity_id"]
        for label in labels
        if label.get("label_type") == "operator_group" and label.get("entity_id")
    ]


def seal(payload: dict[str, Any]) -> dict[str, Any]:
    """Make an answer self-describing and fingerprint its own bytes.

    Applied to every answer the tool returns, found or not: an agent that stores a
    `found: false` can compare it later against the same address and tell whether the
    index's position changed. Unsigned on purpose — see
    `reports/meetings/2026-11-25/06-lookup-receipt-research.md`.
    """
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload.setdefault("not_an_endorsement", True)
    payload["evidence_hash"] = evidence_hash(payload)
    return payload


def score_answer(
    data: dict[str, Any],
    rail: str,
    *,
    evidence_rail: str = "base",
    coverage: str | None = None,
) -> dict[str, Any]:
    """Shape one `queries.fetch_score` record into the tool's answer.

    Shared with `repindex.snapshot`, which prebuilds these answers for the published
    package: a snapshot answer and a live answer are produced by the same code. The
    evidence rail is a parameter because a Solana answer must never claim Base
    evidence, and `coverage` states how the rail was observed.
    """
    fired = fired_rules(data["rules"])
    answer = {
        "found": True,
        "identifier": data["identifier"],
        "rail": rail,
        "evidence_rail": evidence_rail,  # what the evidence actually came from
        "coverage": coverage,
        "score": data["score"],
        "status": data["status"],
        "confidence_band": data["confidence_band"],
        "evidence_summary": {
            "settlements": data["settlements"],
            "counterparties": data["counterparties"],
            "active_days": data["active_days"],
            "window": data["window"],
        },
        "signaling_likelihood": data["signaling_likelihood"],
        "judged": data["status"] == "scored",
        "rules_fired": fired,
        "flags": fired,
        "correction": correction_entry(data["rules"]),
        "label_provenance": data["labels"],
        "operator_groups": operator_group_ids(data["labels"]),
        "methodology_version": queries.METHODOLOGY_VERSION,
        "as_of": data["as_of_block"],
        "disclaimer": DISCLAIMER_TEXT,
    }
    return seal(answer)
