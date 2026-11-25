"""Wipe the derived tables, rebuild them, and hash the result (gate check GA.5).

Prompt task A5 requires that the same database state produce byte-identical scores.
This script is how that is proved: it deletes everything derived from settlements,
recomputes it, and prints one hash. GA.5 runs it twice and compares.

Only derived tables are wiped. `settlements` and `ingest_checkpoints` are inputs and
survive, so the rebuild genuinely recomputes rather than re-fetching.

Dispute corrections are re-applied around the rebuild (prompt task B5): settlement
corrections before the recompute, so features see the restored record; classification
overrides after the rules run and before scores are computed, so the published score
uses the disputed value; label corrections after it, because the recompute rewrites
`labels` and would otherwise erase them. Without this, the next rebuild would silently
undo every correction.

Usage:
    uv run python scripts/rebuild_derived.py --hash-only
    uv run python scripts/rebuild_derived.py --db data/repindex.db
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
from pathlib import Path

import yaml

from repindex import corrections
from repindex.classify import features as feat
from repindex.classify import rules
from repindex.ingest import schema
from repindex.labels import clusters, roles
from repindex.score import rating

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "repindex.db"
THRESHOLDS = REPO_ROOT / "config" / "thresholds.yaml"

DERIVED_TABLES = (
    "features",
    "classifications",
    "settlement_flags",
    "scores",
    "labels",
    "entity_members",
    "entities",
    "addresses",
)


def scores_hash(conn: sqlite3.Connection) -> str:
    """Hash the scores table, ordered, so the same content gives the same digest."""
    digest = hashlib.sha256()
    for row in conn.execute(
        "SELECT identifier, status, score, confidence_band, evidence_settlements, "
        "evidence_counterparties, evidence_active_days, data_window_start, "
        "data_window_end, as_of_block, methodology_version "
        "FROM scores ORDER BY identifier"
    ):
        digest.update("|".join("" if v is None else str(v) for v in tuple(row)).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def rebuild(conn: sqlite3.Connection) -> None:
    # Settlement corrections are inputs to the recompute, so they must be in place
    # before features are recomputed; label corrections are written over the tables
    # the recompute has just rebuilt.
    corrections.reapply_all(conn, actions=("add_settlement",))

    for table in DERIVED_TABLES:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()

    roles.assign_roles(conn)
    found, _ = clusters.find_clusters(conn)
    clusters.write_clusters(conn, found)
    computed = feat.compute_features(conn)
    feat.write_features(conn, computed)
    rules.write_classifications(conn, rules.classify_all(computed))
    rules.flag_settlements(conn)

    # An upheld classification dispute replaces the rules' value. It has to be in
    # place before scoring, because `compute_scores` reads classifications.
    corrections.reapply_all(conn, actions=("override_classification",))

    thresholds = yaml.safe_load(THRESHOLDS.read_text(encoding="utf-8")) or {}
    minimum = rating.EvidenceMinimum.from_thresholds(thresholds)
    rating.write_scores(conn, rating.compute_scores(conn, minimum))

    corrections.reapply_all(conn, actions=corrections.LABEL_ACTIONS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hash-only", action="store_true", help="print only the hash")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()

    if not args.db.is_file():
        print(f"{args.db} missing — run scripts/run_phase_a.py first")
        return 1

    conn = schema.connect(args.db)
    try:
        if not args.hash_only:
            rows = conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
            print(f"rebuilding derived tables from {rows:,} settlements")
        rebuild(conn)
        digest = scores_hash(conn)
        if not args.hash_only:
            scored = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
            print(f"scores rows: {scored:,}")
    finally:
        conn.close()

    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
