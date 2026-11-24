"""Apply a dispute decision with an audit trail (prompt task B5, gate check GB.10).

The review (F17) found this script recorded a decision and changed nothing. An upheld
correction now acts on the data through `repindex.corrections`, and the audit row
carries the exact change so `scripts/rebuild_derived.py` can re-apply it after the
derived tables are wiped.

Append-only by construction: this script can insert a decision or a reversal, and has
no code path that updates or deletes an existing correction. That is what makes the
trail worth having — an editable log proves nothing.

Usage:
    uv run python scripts/apply_correction.py --subject 0x... --claim "..." \
        --evidence "tx 0x..." --decision upheld --reason "settlement was missing" \
        --action add_settlement --settlement missing.json --applied-by owner

    uv run python scripts/apply_correction.py --subject 0x... --claim "not one operator" \
        --evidence "signed message from each key" --decision upheld \
        --reason "cluster formed from shared payers alone" \
        --action remove_label --label-type operator_group --label-value cluster:61 \
        --applied-by owner

    uv run python scripts/apply_correction.py --subject 0x... \
        --claim "these payments are not one closed loop" --evidence "invoice records" \
        --decision upheld --reason "issued invoices and unrelated payers" \
        --action override_classification --likelihood 0.35 --applied-by owner

    uv run python scripts/apply_correction.py --reverse 3 --reason "evidence retracted" \
        --applied-by owner

    uv run python scripts/apply_correction.py --show

The change is visible in the derived tables immediately, and a classification
override also refreshes the seller's published score in the same transaction — the
API never serves a likelihood and a score that cannot both be true. Run
`uv run python scripts/rebuild_derived.py` to recompute features and scores from an
added settlement; the rebuild re-applies the correction rather than erasing it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import yaml

from repindex import corrections
from repindex.ingest import schema
from repindex.score import rating

REPO_ROOT = Path(__file__).resolve().parent.parent
DB = REPO_ROOT / "data" / "repindex.db"
THRESHOLDS = REPO_ROOT / "config" / "thresholds.yaml"


def _minimum() -> rating.EvidenceMinimum:
    """The human-owned evidence minimum, so a refreshed score uses the same bar."""
    thresholds = yaml.safe_load(THRESHOLDS.read_text(encoding="utf-8")) or {}
    return rating.EvidenceMinimum.from_thresholds(thresholds)


def _connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise SystemExit(f"{path} missing — run the pipeline first")
    return schema.connect(path)


def show(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT correction_id, subject, decision, reason, methodology_version, "
        "applied_at, applied_by, previous_state, new_state "
        "FROM corrections ORDER BY correction_id"
    ).fetchall()
    reversals = conn.execute(
        "SELECT reversal_id, reverses, reason, applied_at, applied_by "
        "FROM correction_reversals ORDER BY reversal_id"
    ).fetchall()
    print(
        json.dumps(
            {"corrections": [dict(r) for r in rows], "reversals": [dict(r) for r in reversals]},
            indent=2,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--subject")
    parser.add_argument("--claim")
    parser.add_argument("--evidence")
    parser.add_argument("--decision", choices=["upheld", "not_upheld", "insufficient"])
    parser.add_argument("--reason")
    parser.add_argument("--applied-by", default="owner")
    parser.add_argument("--action", choices=corrections.ACTIONS, default="none")
    parser.add_argument("--label-type", choices=corrections.LABEL_TYPES)
    parser.add_argument("--label-value")
    parser.add_argument("--confidence", type=float)
    parser.add_argument(
        "--likelihood",
        type=float,
        help="replacement signaling_likelihood in [0, 1] (override_classification)",
    )
    parser.add_argument(
        "--settlement",
        type=Path,
        help="JSON file holding one settlement row (add_settlement)",
    )
    parser.add_argument("--reverse", type=int, metavar="CORRECTION_ID")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()

    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(args.db)
        if args.show:
            show(conn)
            return 0

        if args.reverse is not None:
            if not args.reason:
                print("--reason is required to reverse a correction")
                return 1
            applied = corrections.reverse(
                conn,
                args.reverse,
                reason=args.reason,
                applied_by=args.applied_by,
                minimum=_minimum(),
            )
            print(f"reversal of correction {applied.correction_id}: {applied.detail}")
            return 0

        missing = [
            name
            for name in ("subject", "claim", "evidence", "decision", "reason")
            if not getattr(args, name)
        ]
        if missing:
            print(f"missing required arguments: {missing}")
            return 1

        payload: dict[str, object] = {}
        if args.action in corrections.LABEL_ACTIONS:
            payload = {"label_type": args.label_type, "label_value": args.label_value}
            if args.confidence is not None:
                payload["confidence"] = args.confidence
        elif args.action == "add_settlement":
            if args.settlement is None:
                print("--settlement is required for --action add_settlement")
                return 1
            payload = {"settlement": json.loads(args.settlement.read_text(encoding="utf-8"))}
        elif args.action == "override_classification":
            if args.likelihood is None:
                print("--likelihood is required for --action override_classification")
                return 1
            payload = {"signaling_likelihood": args.likelihood}

        applied = corrections.record(
            conn,
            subject=args.subject,
            claim=args.claim,
            evidence=args.evidence,
            decision=args.decision,
            reason=args.reason,
            applied_by=args.applied_by,
            action=args.action,
            payload=payload,
            minimum=_minimum(),
        )
        print(
            f"recorded correction {applied.correction_id}: {args.decision} for "
            f"{args.subject} ({applied.action}: {applied.detail})"
        )
        return 0
    except (
        corrections.CorrectionError,
        schema.SchemaError,
        sqlite3.IntegrityError,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        # Opening the database can fail before the action runs (review K2): a legacy
        # file the schema cannot migrate raises SchemaError, and a constraint that
        # slips past the pre-checks raises IntegrityError. Both are operator errors
        # with a useful message, not tracebacks.
        print(f"error: {exc}")
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
