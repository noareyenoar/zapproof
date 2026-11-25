"""Read-only database queries behind the API.

Every connection is opened with `mode=ro`. Gate check GB.4 asserts that no endpoint
can write; this module makes that true at the connection level, not merely by
convention.

All identifiers are bound as parameters. There is no string interpolation into SQL
anywhere in this file.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repindex import adjudication
from repindex.common.db import connect_readonly

# Re-exported: the API and MCP report the version through this module.
from repindex.common.version import METHODOLOGY_VERSION as METHODOLOGY_VERSION

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = REPO_ROOT / "data" / "repindex.db"
DEFAULT_SOLANA_DB = REPO_ROOT / "data" / "solana.db"


# Measured 2026-11-16 by blinded adjudication. Quoted in /v1/methodology so a caller
# can see how much weight the interpretation deserves without asking us. The value is
# derived in `repindex.adjudication` from the committed report, never typed here.
AGREEMENT_WITH_BLINDED_READERS = adjudication.AGREEMENT_RATE


def resolve_db_path() -> Path:
    """Where the index lives.

    An installed package has no `data/` directory next to the source, so `REPINDEX_DB`
    is the only way a `uvx` install can point at a local snapshot. Unset means the
    source checkout's default, which keeps every existing test and gate unchanged.
    """
    override = os.environ.get("REPINDEX_DB")
    return Path(override).expanduser() if override else DEFAULT_DB


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the index read-only. A missing file is an error, not an empty result."""
    db_path = path or resolve_db_path()
    if not db_path.is_file():
        raise FileNotFoundError(
            f"index database not found at {db_path} (set REPINDEX_DB to a snapshot, "
            "or ZAPPROOF_API_URL to use a hosted index)"
        )
    conn = connect_readonly(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_solana_db_path() -> Path:
    """Where the Solana evidence index lives, if it has been built."""
    override = os.environ.get("REPINDEX_SOLANA_DB")
    return Path(override).expanduser() if override else DEFAULT_SOLANA_DB


def connect_solana(path: Path | None = None) -> sqlite3.Connection:
    """Open the Solana index read-only. A missing file is an error, not an empty result."""
    db_path = path or resolve_solana_db_path()
    if not db_path.is_file():
        raise FileNotFoundError(
            f"Solana index not found at {db_path} (set REPINDEX_SOLANA_DB or build it "
            "with scripts/build_solana_db.py)"
        )
    conn = connect_readonly(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def solana_coverage(conn: sqlite3.Connection) -> str:
    """One sentence stating the window, the classifier and the limits of this rail."""
    meta = {
        row["key"]: json.loads(row["value"])
        for row in conn.execute("SELECT key, value FROM solana_meta")
    }

    def stamp(seconds: int) -> str:
        return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%MZ")

    start = meta.get("window_start")
    end = meta.get("window_end")
    window = f"{stamp(start)} to {stamp(end)}" if start and end else "an unrecorded window"
    return (
        f"Solana rail: observation window {window}. The classifier is the x402scan "
        f"fee-payer allowlist ({meta.get('allowlist_date', 'undated')}, "
        f"{meta.get('allowlist_size', 'unknown')} addresses); this is a sample, not a "
        "census, and payer independence is not proven. `as_of_block` is a Solana slot."
    )


def as_of_block(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(block_number) FROM settlements").fetchone()
    return int(row[0] or 0)


def sellers_indexed(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM features").fetchone()[0])


def fetch_score(conn: sqlite3.Connection, identifier: str) -> dict[str, Any] | None:
    """Everything needed for one identifier, or None when there is no evidence at all.

    Returns None rather than a zero score when the address is absent from `features`.
    That distinction is the project's central rule and gate check GB.8 tests it.

    The identifier must already be canonical: every caller runs `classify_identifier`
    first. Lowercasing here would be wrong for Solana, whose base58 addresses are
    case-sensitive.
    """
    seller = identifier

    row = conn.execute(
        "SELECT f.settlement_count, f.unique_payers, f.active_days, "
        "       c.signaling_likelihood, c.contributions, "
        "       s.status, s.score, s.confidence_band "
        "FROM features f "
        "LEFT JOIN classifications c ON c.seller = f.seller "
        "LEFT JOIN scores s ON s.identifier = f.seller "
        "WHERE f.seller = ?",
        (seller,),
    ).fetchone()

    if row is None:
        return None

    window = conn.execute(
        "SELECT MIN(block_number) AS lo_block, MAX(block_number) AS hi_block, "
        "       MIN(block_time) AS lo_time, MAX(block_time) AS hi_time "
        "FROM settlements WHERE seller = ?",
        (seller,),
    ).fetchone()

    labels = [
        {
            "label_type": r["label_type"],
            "label_value": r["label_value"],
            "entity_id": _entity_id(r["label_value"]),
            "method": r["method"],
            "confidence": float(r["confidence"]),
            "evidence": r["evidence"],
        }
        for r in conn.execute(
            "SELECT label_type, label_value, method, confidence, evidence "
            "FROM labels WHERE address = ? ORDER BY label_type, label_value",
            (seller,),
        )
    ]

    contributions = json.loads(row["contributions"]) if row["contributions"] else []

    # F7: an insufficient-evidence seller must not read as a *zero* likelihood.
    # 0.0 is the most benign value of this field, and a caller filtering on it would
    # treat the thinnest possible seller as the cleanest possible seller. The field is
    # null unless the status is `scored`; the rules still travel so a caller can see
    # what was and was not judged.
    status = row["status"] or "insufficient_evidence"
    likelihood = (
        float(row["signaling_likelihood"])
        if status == "scored" and row["signaling_likelihood"] is not None
        else None
    )

    # J1: an override recorded while the seller was below the evidence minimum stores
    # a likelihood this function deliberately does not publish (F7: null unless
    # scored). Leaving the `dispute_override` entry numeric made one response say
    # "set to 0.9" beside `signaling_likelihood: null`. Report the correction, but
    # without a published numeric effect.
    if status != "scored":
        contributions = [
            {
                "rule": "dispute_override",
                "fired": False,
                "contribution": 0.0,
                "detail": (
                    "override recorded; not scored because the evidence is below the "
                    "minimum, so no likelihood is published"
                ),
            }
            if entry.get("rule") == "dispute_override"
            else entry
            for entry in contributions
        ]

    return {
        "identifier": seller,
        "status": status,
        "score": float(row["score"]) if row["score"] is not None else None,
        "confidence_band": row["confidence_band"] or "low",
        "settlements": int(row["settlement_count"]),
        "counterparties": int(row["unique_payers"]),
        "active_days": int(row["active_days"]),
        "window": {
            "start_block": int(window["lo_block"]),
            "end_block": int(window["hi_block"]),
            "start_time": int(window["lo_time"]),
            "end_time": int(window["hi_time"]),
        },
        "as_of_block": int(window["hi_block"]),
        "signaling_likelihood": likelihood,
        "rules": contributions,
        "labels": labels,
    }


def _entity_id(label_value: str | None) -> int | None:
    """The group id behind an operator-group label (`cluster:61` -> 61)."""
    if not label_value:
        return None
    prefix, _, value = label_value.partition(":")
    if prefix in ("cluster", "entity") and value.isdigit():
        return int(value)
    return None


def fetch_observed_roles(conn: sqlite3.Connection, identifier: str) -> dict[str, Any] | None:
    """What the index has seen this address do, if it has seen it at all.

    F13: `fetch_score` returns None for any address without a seller record, and the
    API used to describe that as "no evidence at all". An address with thousands of
    payments *as a payer* is well known to the index; saying otherwise is false. This
    lookup lets the caller distinguish "nothing known" from "known, but never a seller".
    """
    row = conn.execute(
        "SELECT is_payer, is_seller, is_relayer FROM addresses WHERE address = ?",
        (identifier,),
    ).fetchone()
    if row is None:
        return None

    payer_settlements = int(
        conn.execute(
            "SELECT COUNT(*) FROM settlements WHERE payer = ?", (identifier,)
        ).fetchone()[0]
    )
    seller_settlements = int(
        conn.execute(
            "SELECT COUNT(*) FROM settlements WHERE seller = ?", (identifier,)
        ).fetchone()[0]
    )
    return {
        "is_payer": bool(row["is_payer"]),
        "is_seller": bool(row["is_seller"]),
        "is_relayer": bool(row["is_relayer"]),
        "payer_settlements": payer_settlements,
        "seller_settlements": seller_settlements,
    }


def fetch_entity(conn: sqlite3.Connection, group_id: int) -> dict[str, Any] | None:
    entity = conn.execute(
        "SELECT entity_id, confidence, method, evidence, methodology_version "
        "FROM entities WHERE entity_id = ?",
        (group_id,),
    ).fetchone()
    if entity is None:
        return None

    members = [
        r["address"]
        for r in conn.execute(
            "SELECT address FROM entity_members WHERE entity_id = ? ORDER BY address",
            (group_id,),
        )
    ]
    return {
        "group_id": int(entity["entity_id"]),
        "members": members,
        "group_size": len(members),
        "confidence": float(entity["confidence"]),
        "method": entity["method"],
        "evidence": entity["evidence"],
        "methodology_version": entity["methodology_version"],
    }
