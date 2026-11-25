"""Print a single metric from the database, for the report's reproduce commands.

Prompt task A7 requires every number in the report to carry a command that
reproduces it, and gate check GA.9 re-runs a random sample of those commands and
compares. Each metric prints exactly one line — its value — so the comparison is
unambiguous.

Usage:  uv run python scripts/metrics_report.py <metric>
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import yaml

from repindex.common.db import connect_readonly

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "repindex.db"
SENSITIVITY = REPO_ROOT / "reports" / "sensitivity.json"
TOKEN_CENSUS = REPO_ROOT / "reports" / "token_census.json"
THRESHOLDS = REPO_ROOT / "config" / "thresholds.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metric")
    # The default is the production index, so the commands in the report are
    # unchanged. Tests point this at a throwaway database.
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()

    if not args.db.is_file():
        print(f"database missing: {args.db}")
        return 1

    conn = connect_readonly(args.db)
    conn.row_factory = sqlite3.Row
    try:
        metric = args.metric
        if metric == "settlements":
            value = conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
        elif metric == "sellers":
            value = conn.execute("SELECT COUNT(*) FROM features").fetchone()[0]
        elif metric == "scored":
            value = conn.execute("SELECT COUNT(*) FROM scores WHERE status='scored'").fetchone()[0]
        elif metric == "insufficient_evidence":
            value = conn.execute(
                "SELECT COUNT(*) FROM scores WHERE status='insufficient_evidence'"
            ).fetchone()[0]
        elif metric == "flagged_sellers":
            # The report speaks the API's vocabulary (review K1): a seller below the
            # evidence minimum has no published signaling_likelihood, so its stored
            # classification must not count as one the public number can name.
            value = conn.execute(
                "SELECT COUNT(*) FROM classifications c "
                "JOIN scores s ON s.identifier = c.seller AND s.status = 'scored' "
                "WHERE c.signaling_likelihood >= 0.5"
            ).fetchone()[0]
        elif metric == "flagged_below_minimum_sellers":
            # The other half of that split, published separately so excluding it from
            # the headline does not hide how large the unjudged group is.
            value = conn.execute(
                "SELECT COUNT(*) FROM classifications c "
                "LEFT JOIN scores s ON s.identifier = c.seller "
                "WHERE c.signaling_likelihood >= 0.5 AND COALESCE(s.status, '') != 'scored'"
            ).fetchone()[0]
        elif metric == "nonzero_likelihood_scored_sellers":
            value = conn.execute(
                "SELECT COUNT(*) FROM classifications c "
                "JOIN scores s ON s.identifier = c.seller AND s.status = 'scored' "
                "WHERE c.signaling_likelihood > 0"
            ).fetchone()[0]
        elif metric == "flagged_settlement_share_pct":
            row = conn.execute(
                "SELECT SUM(f.settlement_count) AS flagged FROM features f "
                "JOIN classifications c ON c.seller = f.seller "
                "JOIN scores s ON s.identifier = c.seller AND s.status = 'scored' "
                "WHERE c.signaling_likelihood >= 0.5"
            ).fetchone()
            total = conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
            value = round(100.0 * (row["flagged"] or 0) / total, 4) if total else 0.0
        elif metric == "flagged_below_minimum_settlement_share_pct":
            row = conn.execute(
                "SELECT SUM(f.settlement_count) AS flagged FROM features f "
                "JOIN classifications c ON c.seller = f.seller "
                "LEFT JOIN scores s ON s.identifier = c.seller "
                "WHERE c.signaling_likelihood >= 0.5 AND COALESCE(s.status, '') != 'scored'"
            ).fetchone()
            total = conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
            value = round(100.0 * (row["flagged"] or 0) / total, 4) if total else 0.0
        elif metric == "clusters":
            value = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        elif metric == "window_start":
            value = conn.execute("SELECT MIN(block_number) FROM settlements").fetchone()[0]
        elif metric == "window_end":
            value = conn.execute("SELECT MAX(block_number) FROM settlements").fetchone()[0]
        elif metric == "window_hours":
            row = conn.execute(
                "SELECT MIN(block_time) AS t0, MAX(block_time) AS t1 FROM settlements"
            ).fetchone()
            value = (
                round((int(row["t1"]) - int(row["t0"])) / 3600.0, 1)
                if row["t0"] is not None and row["t1"] is not None
                else 0.0
            )
        elif metric == "phase0_census_window_start_block":
            data = json.loads(TOKEN_CENSUS.read_text(encoding="utf-8"))
            value = int(data["window"]["from_block"])
        elif metric == "phase0_census_window_end_block":
            data = json.loads(TOKEN_CENSUS.read_text(encoding="utf-8"))
            value = int(data["window"]["to_block"])
        elif metric == "phase0_unsupported_token_share_pct":
            # Recompute from the census's own counts. The earlier version echoed the
            # stored aggregate and never measured anything (second review, G6).
            data = json.loads(TOKEN_CENSUS.read_text(encoding="utf-8"))
            total = int(data.get("total_settlements_in_window") or 0)
            unresolved = sum(
                int(token.get("settlement_count") or 0)
                for token in data.get("tokens", [])
                if token.get("resolved") is not True
            )
            value = round(100.0 * unresolved / total, 4) if total else 0.0
        elif metric == "phase0_unresolved_mechanism_share_pct":
            data = json.loads(TOKEN_CENSUS.read_text(encoding="utf-8"))
            paths = data.get("mechanism_paths", {})
            attributed = int(paths.get("eip3009_authorization_used") or 0)
            unresolved = int(paths.get("unattributed_events") or 0)
            denominator = attributed + unresolved
            value = round(100.0 * unresolved / denominator, 6) if denominator else 0.0
        elif metric == "sensitivity_max_shift_pp":
            data = json.loads(SENSITIVITY.read_text(encoding="utf-8"))
            value = float(data["max_share_shift_pp"])
        elif metric == "flag_cutoff":
            data = json.loads(SENSITIVITY.read_text(encoding="utf-8"))
            value = float(data["flag_cutoff"])
        elif metric == "sensitivity_perturbation_pct":
            data = json.loads(SENSITIVITY.read_text(encoding="utf-8"))
            value = int(data["perturbation_pct"])
        elif metric == "sensitivity_unstable_share_shift_pp":
            data = yaml.safe_load(THRESHOLDS.read_text(encoding="utf-8")) or {}
            limit = float(data.get("phaseA", {}).get("sensitivity_unstable_share_shift_pp", 15))
            # Print 15, not 15.0: the report says "limit 15" and GA.9 compares the
            # printed line to the value in the table as text.
            value = int(limit) if limit.is_integer() else limit
        else:
            print(f"unknown metric: {metric}")
            return 1
    finally:
        conn.close()

    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
