"""Apply a dispute decision, not only record it (prompt task B5, gate check GB.10).

The independent review (F17) found that `scripts/apply_correction.py` wrote a decision
row and changed nothing, so `DISPUTES.md` promised an outcome the code did not deliver.
This module owns the two halves that were missing:

- **apply** — an upheld correction acts on the data. `remove_label` deletes a wrong
  interpretation, `add_label` records a missing one, `add_settlement` inserts a
  settlement the index failed to ingest, and `override_classification` replaces the
  rules' `signaling_likelihood` for one seller with a value an upheld dispute
  established. The second independent review found that a classification was listed
  as disputable while nothing could change it (G2).
- **re-apply** — `labels`, `entities`, `classifications` and `scores` are derived and
  `scripts/rebuild_derived.py` wipes them. Without re-application the next rebuild
  would silently erase a correction, which is worse than not applying it at all.
  Active corrections are read back from the audit row, so the trail is the source of
  truth.

The corrections table stays append-only: a correction is one INSERT, a reversal is a
second row that points at the first, and nothing updates or deletes either. Undoing
the *effect* of a reversed correction is allowed, because that effect lives in the
derived label tables, not in the audit trail.

Settlements are evidence. A reversed `add_settlement` stops the correction being
re-applied but does not delete the row: `DISPUTES.md` says a settlement that happened
stays in the database.
"""

from __future__ import annotations

import decimal
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any, cast

from repindex.api.identifiers import InvalidIdentifier, normalise_identifier
from repindex.classify import rules
from repindex.common.version import METHODOLOGY_VERSION
from repindex.score import rating

ACTIONS = (
    "none",
    "remove_label",
    "add_label",
    "add_settlement",
    "override_classification",
)
LABEL_TYPES = ("role", "operator_group", "other")
LABEL_ACTIONS = ("remove_label", "add_label")

SETTLEMENT_FIELDS = (
    "tx_hash",
    "log_index",
    "block_number",
    "block_hash",
    "block_time",
    "token",
    "decimals",
    "raw_amount",
    "normalized_amount",
    "payer",
    "seller",
    "facilitator_relayer",
    "nonce",
    "mechanism",
)


class CorrectionError(ValueError):
    """A correction the database must not accept."""


# Ingested transaction and block hashes are 0x followed by 64 lowercase hex characters
# (JSON-RPC nodes return them lowercase). I1: an upper-case correction was a *different*
# value to SQLite's BINARY comparison, so it inserted a second row for the same log.
TX_HASH_RE = re.compile(r"^0x[0-9a-f]{64}$")
# A nonce is either a 32-byte word or a topic from the log; both are 64 hex characters.
NONCE_RE = re.compile(r"^0x[0-9a-f]{1,64}$")


@dataclass(frozen=True)
class Applied:
    """What an upheld correction changed, and whether anything changed at all."""

    correction_id: int
    action: str
    changed: bool
    detail: str


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CorrectionError(f"`{key}` must be a non-empty string")
    return value.strip()


def _normalise_address(value: str, field: str) -> str:
    """Normalise an operator-supplied address the way the read path does.

    The third independent review (H2) found that a checksummed or DID subject missed
    the lowercase rows the API reads: `remove_label` reported success while deleting
    nothing, and `add_label` wrote a row the API never returned. Ingestion stores
    lowercase addresses, so a correction must name the same identity.
    """
    try:
        return normalise_identifier(value)
    except InvalidIdentifier as exc:
        raise CorrectionError(
            f"`{field}` is not an address this index can look up: {exc}"
        ) from exc


def _validate(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action not in ACTIONS:
        raise CorrectionError(f"unknown action `{action}`; expected one of {ACTIONS}")
    if action == "none":
        return {}
    if action in LABEL_ACTIONS:
        label_type = _text(payload, "label_type")
        if label_type not in LABEL_TYPES:
            raise CorrectionError(f"`label_type` must be one of {LABEL_TYPES}")
        label_value = _text(payload, "label_value")
        cleaned: dict[str, Any] = {"label_type": label_type, "label_value": label_value}
        if action == "add_label":
            confidence = payload.get("confidence", 1.0)
            if not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
                raise CorrectionError("`confidence` must be a number in [0, 1]")
            cleaned["confidence"] = float(confidence)
        return cleaned

    if action == "override_classification":
        value = payload.get("signaling_likelihood")
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not 0.0 <= float(value) <= 1.0
        ):
            raise CorrectionError("`signaling_likelihood` must be a number in [0, 1]")
        return {"signaling_likelihood": float(value)}

    settlement = payload.get("settlement")
    if not isinstance(settlement, dict):
        raise CorrectionError("`settlement` must be an object with the row's fields")
    missing = [field for field in SETTLEMENT_FIELDS if field not in settlement]
    if missing:
        raise CorrectionError(f"settlement is missing {missing}")
    for field in ("tx_hash", "block_hash", "normalized_amount", "payer", "seller", "nonce"):
        if not isinstance(settlement[field], str) or not settlement[field].strip():
            raise CorrectionError(f"settlement `{field}` must be a non-empty string")
    for field in ("log_index", "block_number", "block_time", "decimals", "raw_amount"):
        value = settlement[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise CorrectionError(f"settlement `{field}` must be a non-negative integer")
    if settlement["decimals"] > 36:
        raise CorrectionError("settlement `decimals` is implausible")
    try:
        amount = decimal.Decimal(settlement["normalized_amount"])
    except decimal.InvalidOperation as exc:
        raise CorrectionError("settlement `normalized_amount` is not a decimal") from exc
    if not amount.is_finite():
        raise CorrectionError("settlement `normalized_amount` is not finite")
    with decimal.localcontext() as ctx:
        ctx.prec = 60
        expected = decimal.Decimal(settlement["raw_amount"]) / (
            decimal.Decimal(10) ** settlement["decimals"]
        )
    if amount != expected:
        raise CorrectionError(
            "settlement `normalized_amount` does not equal raw_amount / 10**decimals"
        )
    # I1: canonicalise the hex fields before anything reads them. Ingestion stores the
    # node's lowercase hashes and settlement rows are deduplicated on (tx_hash,
    # log_index), so an upper-case variant inserted a duplicate log that inflated the
    # seller's evidence (the fourth review observed 25 rows for 24 logs).
    settlement = dict(settlement)
    for field in ("tx_hash", "block_hash", "nonce"):
        settlement[field] = settlement[field].strip().lower()
    if not TX_HASH_RE.fullmatch(settlement["tx_hash"]):
        raise CorrectionError("settlement `tx_hash` must be 0x followed by 64 hex characters")
    if not TX_HASH_RE.fullmatch(settlement["block_hash"]):
        raise CorrectionError("settlement `block_hash` must be 0x followed by 64 hex characters")
    if not NONCE_RE.fullmatch(settlement["nonce"]):
        raise CorrectionError("settlement `nonce` must be 0x followed by hex characters")

    # H2: ingestion stores lowercase addresses. A checksummed address in the payload
    # would insert a second identity, so the corrected settlement would not change the
    # seller the index scores.
    for field in ("token", "payer", "seller"):
        if not isinstance(settlement[field], str) or not settlement[field].strip():
            raise CorrectionError(f"settlement `{field}` must be a non-empty string")
        settlement[field] = _normalise_address(settlement[field], f"settlement `{field}`")
    relayer = settlement["facilitator_relayer"]
    if relayer is None or (isinstance(relayer, str) and not relayer.strip()):
        settlement["facilitator_relayer"] = None
    elif isinstance(relayer, str):
        settlement["facilitator_relayer"] = _normalise_address(
            relayer, "settlement `facilitator_relayer`"
        )
    else:
        raise CorrectionError("settlement `facilitator_relayer` must be a string or null")
    return {"settlement": settlement}


def _label_row(
    conn: sqlite3.Connection, address: str, label_type: str, label_value: str
) -> sqlite3.Row | None:
    return cast(
        "sqlite3.Row | None",
        conn.execute(
            "SELECT * FROM labels WHERE address = ? AND label_type = ? AND label_value = ?",
            (address, label_type, label_value),
        ).fetchone(),
    )


def _classification_row(conn: sqlite3.Connection, seller: str) -> sqlite3.Row | None:
    return cast(
        "sqlite3.Row | None",
        conn.execute(
            "SELECT * FROM classifications WHERE seller = ?", (seller,)
        ).fetchone(),
    )


def _settlement_row(conn: sqlite3.Connection, tx_hash: str, log_index: int) -> sqlite3.Row | None:
    return cast(
        "sqlite3.Row | None",
        conn.execute(
            # COLLATE NOCASE as well as the canonical lowercase form: a row ingested
            # before the I1 fix (or by a different writer) must still block a duplicate.
            "SELECT * FROM settlements WHERE tx_hash = ? COLLATE NOCASE AND log_index = ?",
            (tx_hash, log_index),
        ).fetchone(),
    )


def _refresh_score(
    conn: sqlite3.Connection, seller: str, minimum: rating.EvidenceMinimum
) -> None:
    """Recompute the seller's published score from the current classification (H1)."""
    score = rating.score_one(conn, seller, minimum)
    if score is not None:
        rating.write_score(conn, score)


def _apply(
    conn: sqlite3.Connection,
    subject: str,
    action: str,
    payload: dict[str, Any],
    evidence: str,
    minimum: rating.EvidenceMinimum | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """Change the data for an upheld correction. Returns (changed, detail, previous)."""
    if action == "remove_label":
        row = _label_row(conn, subject, payload["label_type"], payload["label_value"])
        if row is None:
            return False, "no matching label; nothing to remove", None
        conn.execute(
            "DELETE FROM labels WHERE address = ? AND label_type = ? AND label_value = ?",
            (subject, payload["label_type"], payload["label_value"]),
        )
        return True, "removed label", dict(row)

    if action == "add_label":
        existing = _label_row(conn, subject, payload["label_type"], payload["label_value"])
        if existing is not None:
            return False, "label already exists; left unchanged", dict(existing)
        conn.execute(
            "INSERT INTO labels (address, label_type, label_value, method, confidence, "
            "evidence, methodology_version) VALUES (?,?,?,?,?,?,?)",
            (
                subject,
                payload["label_type"],
                payload["label_value"],
                "dispute",
                payload["confidence"],
                evidence,
                METHODOLOGY_VERSION,
            ),
        )
        return True, "added label", None

    if action == "override_classification":
        row = _classification_row(conn, subject)
        if row is None:
            raise CorrectionError(
                "no classification row exists for this seller; an override replaces "
                "an existing classification rather than inventing one"
            )
        previous = dict(row)
        override = float(payload["signaling_likelihood"])
        contributions = json.loads(row["contributions"])
        computed = float(row["signaling_likelihood"])
        prior = next((c for c in contributions if c.get("rule") == "dispute_override"), None)
        if prior is not None:
            # Re-application without a wipe: this seller already carries an override.
            if override == float(row["signaling_likelihood"]):
                if minimum is not None:
                    _refresh_score(conn, subject, minimum)
                return False, "classification already overridden; left unchanged", previous
            # A later override replaces an earlier one. The rules' own value is
            # recorded on the entry the earlier correction appended.
            computed = float(prior.get("computed_before", computed))
            contributions = [c for c in contributions if c.get("rule") != "dispute_override"]
        # The override is published as an explicit contribution, so the response
        # explains why the number is not the one the rules computed. Its contribution
        # is the delta that makes the visible entries sum to the published value.
        contributions.append(
            {
                "rule": "dispute_override",
                "fired": True,
                "contribution": round(override - computed, 6),
                "computed_before": computed,
                "detail": (
                    f"signaling_likelihood set to {override:.6f} by an upheld dispute; "
                    f"the rules computed {computed:.6f}"
                ),
            }
        )
        conn.execute(
            "UPDATE classifications SET signaling_likelihood = ?, contributions = ? "
            "WHERE seller = ?",
            (override, json.dumps(contributions), subject),
        )
        # The published score is computed from the likelihood. Updating one without
        # the other would publish a pair that cannot both be true under the formula
        # until someone ran a rebuild (H1), so both change in this transaction.
        if minimum is not None:
            _refresh_score(conn, subject, minimum)
        return True, f"classification override {computed:.6f} -> {override:.6f}", previous

    settlement = payload["settlement"]
    existing = _settlement_row(conn, settlement["tx_hash"], settlement["log_index"])
    if existing is not None:
        return False, "settlement already present; left unchanged", dict(existing)
    placeholders = ",".join("?" for _ in SETTLEMENT_FIELDS)
    conn.execute(
        f"INSERT INTO settlements ({','.join(SETTLEMENT_FIELDS)}) VALUES ({placeholders})",
        tuple(settlement[field] for field in SETTLEMENT_FIELDS),
    )
    return True, "added settlement", None


def record(
    conn: sqlite3.Connection,
    *,
    subject: str,
    claim: str,
    evidence: str,
    decision: str,
    reason: str,
    applied_by: str,
    action: str = "none",
    payload: dict[str, Any] | None = None,
    methodology_version: str = METHODOLOGY_VERSION,
    minimum: rating.EvidenceMinimum,
) -> Applied:
    """Validate, apply and audit one decision in a single transaction.

    The effect is written before the audit row and committed with it, so a failure
    either leaves both or neither. The stored `new_state` is what `reapply_all` reads:
    the audit trail stays the source of truth rather than a commentary on it.

    The subject is normalised to the lowercase address the index stores, and
    `minimum` is required so a classification override also refreshes the published
    score in the same transaction rather than leaving the two inconsistent (H1, H2).
    """
    if decision not in ("upheld", "not_upheld", "insufficient"):
        raise CorrectionError(f"unknown decision `{decision}`")
    for name, value in (
        ("subject", subject),
        ("claim", claim),
        ("evidence", evidence),
        ("reason", reason),
        ("applied_by", applied_by),
    ):
        if not value or not value.strip():
            raise CorrectionError(f"`{name}` must be non-empty")
    # H2: the read path lowercases identifiers and looks labels up by address. Storing
    # the operator's spelling would let a correction miss the row it names.
    subject = _normalise_address(subject.strip(), "subject")
    cleaned = _validate(action, payload or {})

    if decision != "upheld":
        # A rejected dispute is a decision, not a change: record it and touch nothing.
        cleaned = {"action": "none"}

    previous: dict[str, Any] | None = None
    changed = False
    detail = "recorded; no data change"
    if decision == "upheld" and action != "none":
        changed, detail, previous = _apply(
            conn, subject, action, cleaned, evidence, minimum
        )

    new_state = {"action": action if decision == "upheld" else "none", **cleaned}
    cursor = conn.execute(
        "INSERT INTO corrections (subject, claim, evidence, decision, reason, "
        "methodology_version, applied_by, previous_state, new_state) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            subject,
            claim.strip(),
            evidence.strip(),
            decision,
            reason.strip(),
            methodology_version,
            applied_by.strip(),
            json.dumps(previous, sort_keys=True) if previous is not None else None,
            json.dumps(new_state, sort_keys=True),
        ),
    )
    conn.commit()
    correction_id = cursor.lastrowid
    if correction_id is None:
        raise CorrectionError("the database did not assign a correction id")
    return Applied(int(correction_id), str(new_state["action"]), changed, detail)


def _stored_state(row: sqlite3.Row) -> dict[str, Any] | None:
    try:
        state = json.loads(row["new_state"] or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("action") not in ACTIONS:
        return None
    return state


def active(
    conn: sqlite3.Connection, *, actions: tuple[str, ...] | None = None
) -> list[sqlite3.Row]:
    """Upheld corrections with no reversal, oldest first."""
    rows = conn.execute(
        "SELECT c.* FROM corrections c "
        "LEFT JOIN correction_reversals r ON r.reverses = c.correction_id "
        "WHERE c.decision = 'upheld' AND r.reversal_id IS NULL "
        "ORDER BY c.correction_id"
    ).fetchall()
    if actions is None:
        return list(rows)
    return [row for row in rows if (_stored_state(row) or {}).get("action") in actions]


def reapply_all(
    conn: sqlite3.Connection,
    *,
    actions: tuple[str, ...] | None = None,
    minimum: rating.EvidenceMinimum | None = None,
) -> list[Applied]:
    """Apply every active correction to the current derived tables.

    `scripts/rebuild_derived.py` calls this: settlement corrections before the
    recompute (the recompute must see them) and label corrections after it (the
    recompute wipes them). Re-application is idempotent by construction.

    `minimum` is optional here because the rebuild recomputes every score immediately
    after it re-applies an override, and a per-seller refresh would be thrown away.
    """
    applied: list[Applied] = []
    for row in active(conn, actions=actions):
        state = _stored_state(row)
        if state is None:
            continue
        action = state["action"]
        payload = {k: v for k, v in state.items() if k != "action"}
        subject = row["subject"]
        if action != "add_settlement":
            subject = _normalise_address(subject, "stored correction subject")
        changed, detail, _ = _apply(
            conn, subject, action, payload, row["evidence"], minimum
        )
        applied.append(Applied(int(row["correction_id"]), action, changed, detail))
    conn.commit()
    return applied


def reverse(
    conn: sqlite3.Connection,
    correction_id: int,
    *,
    reason: str,
    applied_by: str,
    minimum: rating.EvidenceMinimum,
) -> Applied:
    """Record a reversal and undo the effect where the prior state allows it.

    Reversing a classification override recomputes the classification from the current
    features and restores the published score in this transaction (H1, I3), so the API
    never serves the reversed likelihood beside a score computed from it, and never
    serves a classification that contradicts the evidence it also reports.
    """
    row = conn.execute(
        "SELECT * FROM corrections WHERE correction_id = ?", (correction_id,)
    ).fetchone()
    if row is None:
        raise CorrectionError(f"no correction with id {correction_id}")
    if conn.execute(
        "SELECT 1 FROM correction_reversals WHERE reverses = ?", (correction_id,)
    ).fetchone():
        raise CorrectionError(f"correction {correction_id} is already reversed")
    if not reason or not reason.strip() or not applied_by or not applied_by.strip():
        raise CorrectionError("a reversal needs a reason and an author")

    state = _stored_state(row)
    changed = False
    detail = "recorded; no data change"
    previous = json.loads(row["previous_state"]) if row["previous_state"] else None
    subject = row["subject"]
    if state is not None and state["action"] != "add_settlement":
        subject = _normalise_address(subject, "stored correction subject")
    if state is not None:
        if state["action"] == "remove_label" and previous is not None:
            conn.execute(
                "INSERT OR IGNORE INTO labels (address, label_type, label_value, method, "
                "confidence, evidence, methodology_version) VALUES (?,?,?,?,?,?,?)",
                (
                    subject,
                    previous["label_type"],
                    previous["label_value"],
                    previous["method"],
                    previous["confidence"],
                    previous["evidence"],
                    previous["methodology_version"],
                ),
            )
            changed, detail = True, "restored the removed label"
        elif state["action"] == "add_label" and previous is None:
            conn.execute(
                "DELETE FROM labels WHERE address = ? AND label_type = ? AND label_value = ? "
                "AND method = 'dispute'",
                (subject, state["label_type"], state["label_value"]),
            )
            changed, detail = True, "removed the label this correction added"
        elif state["action"] == "add_settlement":
            detail = "reversal recorded; the settlement stays (evidence is not deleted)"
        elif state["action"] == "override_classification" and previous is not None:
            # I3: the stored snapshot is the rules' answer from the day of the dispute.
            # By the time the dispute is reversed the features may describe different
            # evidence, and restoring the snapshot published rules that contradicted the
            # API's own settlement counts. Re-run the rules against the current feature
            # row, which is what a rebuild would compute; fall back to the stored
            # snapshot only when the seller has no features at all.
            fresh = rules.classify_one(conn, subject)
            if fresh is None:
                values = (
                    previous["signaling_likelihood"],
                    previous["contributions"],
                    previous["methodology_version"],
                )
            else:
                values = (
                    fresh.signaling_likelihood,
                    json.dumps([asdict(outcome) for outcome in fresh.contributions]),
                    fresh.methodology_version,
                )
            cursor = conn.execute(
                "UPDATE classifications SET signaling_likelihood = ?, contributions = ?, "
                "methodology_version = ? WHERE seller = ?",
                (*values, subject),
            )
            if cursor.rowcount:
                _refresh_score(conn, subject, minimum)
                changed = True
                detail = (
                    "recomputed the classification from the current evidence and restored "
                    "the score"
                    if fresh is not None
                    else "restored the stored pre-dispute classification and score"
                )
                # An earlier override may still be active for this seller (a later
                # dispute replaced it). Re-applying it over the recomputed rules keeps
                # the invariant this branch is about: after the reversal the seller's
                # classification must equal what a rebuild would compute.
                if fresh is not None:
                    earlier = [
                        row
                        for row in active(conn, actions=("override_classification",))
                        if int(row["correction_id"]) != correction_id
                        and _normalise_address(row["subject"], "stored correction subject")
                        == subject
                    ]
                    latest_state = _stored_state(earlier[-1]) if earlier else None
                    if latest_state is not None:
                        _apply(
                            conn,
                            subject,
                            "override_classification",
                            {k: v for k, v in latest_state.items() if k != "action"},
                            earlier[-1]["evidence"],
                            minimum,
                        )
                        detail = (
                            "recomputed from the current evidence and re-applied the "
                            "earlier upheld override"
                        )
            else:
                detail = "reversal recorded; no classification row to restore"

    conn.execute(
        "INSERT INTO correction_reversals (reverses, reason, applied_by) VALUES (?,?,?)",
        (correction_id, reason.strip(), applied_by.strip()),
    )
    conn.commit()
    return Applied(correction_id, state["action"] if state else "none", changed, detail)
