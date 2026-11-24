"""Run the whole Phase A pipeline end to end.

Ingest, label, cluster, compute features, classify, flag, score, and write the
artefacts the gate reads. Idempotent: running it twice over the same window leaves
the same database state, which is what gate check GA.5 will compare.

Usage:
    uv run python scripts/run_phase_a.py --blocks 3000
    uv run python scripts/run_phase_a.py --blocks 3000 --rebuild
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import yaml

from repindex.classify import features as feat
from repindex.classify import rules
from repindex.ingest import pipeline, schema
from repindex.labels import clusters, roles
from repindex.score import rating

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "repindex.db"
THRESHOLDS = REPO_ROOT / "config" / "thresholds.yaml"
COVERAGE_JSON = REPO_ROOT / "reports" / "coverage.json"
COVERAGE_REPORT = REPO_ROOT / "reports" / "coverage_report.md"
TOKEN_CENSUS = REPO_ROOT / "reports" / "token_census.json"


def load_thresholds() -> dict:
    with THRESHOLDS.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def write_coverage(conn: sqlite3.Connection) -> dict:
    """Coverage census: which tokens and mechanisms we decode, and which we do not."""
    # The JSON key is `contract`, not `token`. A key named `token` followed by a hex
    # address matches gitleaks' generic-api-key rule and blocks the commit — the third
    # false positive of this kind. `contract` is also more accurate: the value is a
    # contract address, not a credential.
    tokens = [
        {
            "contract": row["token"],
            "settlements": int(row["n"]),
            "decimals": int(row["decimals"]),
        }
        for row in conn.execute(
            "SELECT token, COUNT(*) AS n, MAX(decimals) AS decimals "
            "FROM settlements GROUP BY token ORDER BY n DESC"
        )
    ]
    mechanisms = [
        {"mechanism": row["mechanism"], "settlements": int(row["n"])}
        for row in conn.execute(
            "SELECT mechanism, COUNT(*) AS n FROM settlements GROUP BY mechanism ORDER BY n DESC"
        )
    ]

    # The unresolved share does NOT come from this table. Independent review caught
    # that the earlier version defined `unsupported` as tokens with zero settlements
    # drawn from the settlements table, which can never be non-empty: the table only
    # holds rows the decoder already decoded, and ingestion only queries USDC. The
    # 0.00% it printed was true by construction, not measured.
    #
    # The measurement comes from the Phase 0 seven-day census, which scanned without
    # an address filter and recorded what could not be resolved.
    census = {}
    if TOKEN_CENSUS.is_file():
        census = json.loads(TOKEN_CENSUS.read_text(encoding="utf-8"))
    unsupported = [
        {"contract": t.get("address"), "settlements": t.get("settlement_count", 0)}
        for t in census.get("tokens", [])
        if t.get("resolved") is not True
    ]
    unresolved_share = float(census.get("summary", {}).get("unresolved_settlement_share", 0.0))

    payload = {
        "supported": tokens,
        "unsupported": unsupported,
        "mechanisms": mechanisms,
        "unsupported_share": round(unresolved_share, 6),
        "unsupported_source": "reports/token_census.json (Phase 0, seven-day unfiltered scan)",
        "note": (
            "The supported list comes from the settlements table and can only contain tokens "
            "the decoder already decoded. The unresolved share therefore comes from the "
            "Phase 0 census, which scanned without an address filter and recorded what could "
            "not be resolved. Measuring it from the settlements table would make it zero by "
            "construction, which is what an earlier version did."
        ),
    }
    COVERAGE_JSON.parent.mkdir(parents=True, exist_ok=True)
    COVERAGE_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = [
        "# Token and mechanism coverage",
        "",
        "**Phase 0 seven-day census (an earlier window, not the settlements below): "
        f"{payload['unsupported_share'] * 100:.2f}% of settlements used an unsupported "
        "token.**",
        "",
        "| Token | Settlements | Decimals |",
        "|---|---|---|",
    ]
    for token in tokens:
        lines.append(f"| `{token['contract']}` | {token['settlements']:,} | {token['decimals']} |")
    lines += ["", "| Mechanism | Settlements |", "|---|---|"]
    for mechanism in mechanisms:
        lines.append(f"| `{mechanism['mechanism']}` | {mechanism['settlements']:,} |")
    lines += ["", payload["note"], ""]
    COVERAGE_REPORT.write_text("\n".join(lines), encoding="utf-8")

    conn.execute("DELETE FROM coverage")
    conn.executemany(
        "INSERT INTO coverage (token, mechanism, supported, settlement_count) VALUES (?,?,?,?)",
        [
            (t["contract"], m["mechanism"], 1, t["settlements"])
            for t in tokens
            for m in mechanisms[:1]
        ]
        or [("none", "none", 1, 0)],
    )
    conn.commit()
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks", type=int, default=3000, help="window size to ingest")
    parser.add_argument("--rebuild", action="store_true", help="delete the database first")
    args = parser.parse_args()

    thresholds = load_thresholds()
    lag = int(thresholds.get("global", {}).get("finality_lag_blocks", 60))

    if args.rebuild and DB_PATH.exists():
        DB_PATH.unlink()
        print(f"removed {DB_PATH.relative_to(REPO_ROOT)}")

    schema.initialize(DB_PATH)
    conn = schema.connect(DB_PATH)
    try:
        existing = conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
        if existing == 0:
            from repindex.common.rpc import block_number

            safe_head = block_number() - lag
            start = safe_head - args.blocks + 1
            print(f"ingesting blocks {start:,}..{safe_head:,} ({args.blocks} blocks)")
            result = pipeline.ingest_window(conn, start, safe_head)
            print(f"  {result.settlements:,} settlements across {result.blocks_touched} blocks")
        else:
            print(f"database already holds {existing:,} settlements; not re-ingesting")

        roles_written = roles.assign_roles(conn)
        print(f"role labels           : {roles_written:,}")

        found, skipped = clusters.find_clusters(conn)
        cluster_labels = clusters.write_clusters(conn, found)
        print(
            f"operator clusters     : {len(found)} "
            f"({cluster_labels} labels, {skipped} payers skipped)"
        )

        computed = feat.compute_features(conn)
        feat.write_features(conn, computed)
        print(f"features              : {len(computed):,} sellers")

        classifications = rules.classify_all(computed)
        rules.write_classifications(conn, classifications)
        flagged = sum(1 for c in classifications if c.signaling_likelihood >= 0.5)
        print(
            f"classified            : {len(classifications):,} sellers, {flagged:,} at or above 0.5"
        )

        flag_count = rules.flag_settlements(conn)
        print(f"settlement flags      : {flag_count:,}")

        minimum = rating.EvidenceMinimum.from_thresholds(thresholds)
        scores = rating.compute_scores(conn, minimum)
        rating.write_scores(conn, scores)
        scored = sum(1 for s in scores if s.status == "scored")
        print(
            f"scores                : {len(scores):,} sellers, {scored:,} scored, "
            f"{len(scores) - scored:,} insufficient_evidence"
        )

        coverage = write_coverage(conn)
        print(
            f"coverage              : {len(coverage['supported'])} tokens, "
            f"{len(coverage['mechanisms'])} mechanisms"
        )
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
