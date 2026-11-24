"""Generate reports/genuine_vs_signaling_v0.md (prompt task A7, gate checks GA.8, GA.9).

Aggregates only. No individual address is named in the report body; entity-level
detail lives in the database and, later, behind the API with provenance.

Every headline number is computed **directly from the database here**, not by running
the reproduce command. That distinction is what makes GA.9 a real check: the gate
re-runs the command and compares, so the two paths have to agree independently.

Writes:
  reports/genuine_vs_signaling_v0.md
  reports/genuine_vs_signaling_v0.reproduce.json

Run:  uv run python scripts/generate_report.py
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from repindex.classify.rules import DEFAULT_THRESHOLDS
from repindex.common.db import connect_readonly
from repindex.common.provenance import (
    CommitObserver,
    DatabaseChangedError,
    PendingWALError,
    attest_identity,
    require_unchanged,
)
from repindex.common.version import METHODOLOGY_VERSION

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "repindex.db"
THRESHOLDS = REPO_ROOT / "config" / "thresholds.yaml"
SENSITIVITY = REPO_ROOT / "reports" / "sensitivity.json"
COVERAGE = REPO_ROOT / "reports" / "coverage.json"
TOKEN_CENSUS = REPO_ROOT / "reports" / "token_census.json"
OUT = REPO_ROOT / "reports" / "genuine_vs_signaling_v0.md"
OUT_REPRO = REPO_ROOT / "reports" / "genuine_vs_signaling_v0.reproduce.json"

METRIC_CMD = "uv run python scripts/metrics_report.py"

# The perturbation keys `scripts/sensitivity.py` writes: both global directions plus
# both directions for every `RuleThresholds` field. The report claims every threshold
# and weight was scaled, so an artefact missing any of these does not support that
# sentence (review N3).
EXPECTED_PERTURBATIONS = frozenset(
    {"global_down_25", "global_up_25"}
    | {
        f"{field_name}_{direction}_25"
        for field_name in asdict(DEFAULT_THRESHOLDS)
        for direction in ("down", "up")
    }
)


def _sensitivity_problem(
    sensitivity: dict[str, Any],
    flagged_share: float,
    settlements: int,
    sellers: int,
    scored: int,
) -> str | None:
    """Why a sensitivity artefact cannot describe this report's number, or None.

    A matching database hash is not enough. The artefact can come from the pre-L1
    rules-only implementation, from a different perturbation definition, from another
    database state, or be internally inconsistent about its own maximum (reviews L1,
    M3). Every check is either a field the producer writes or an arithmetic relation
    inside the artefact; none can be satisfied by a stale file that describes another
    quantity.
    """
    if sensitivity.get("scored_only") is not True:
        return "predates the scored-only definition (no `scored_only: true`)"
    if sensitivity.get("baseline_from") != "stored_classifications":
        return (
            "was not produced from the stored classifications "
            f"(baseline_from={sensitivity.get('baseline_from')!r}); its stability "
            "figure would describe a rules-only baseline"
        )
    if sensitivity.get("perturbation_pct") != 25:
        return (
            f"records a {sensitivity.get('perturbation_pct')!r}% perturbation, but "
            "this report says 25%"
        )
    if sensitivity.get("flag_cutoff") != 0.5:
        return (
            f"records a flag cutoff of {sensitivity.get('flag_cutoff')!r}, but this "
            "report says 0.5"
        )
    baseline = sensitivity.get("baseline_share_pct")
    if not isinstance(baseline, (int, float)) or round(float(baseline), 4) != flagged_share:
        return (
            f"starts from {baseline}%, but this database's headline is "
            f"{flagged_share:.4f}% (scored sellers at the 0.5 cutoff)"
        )
    baseline_share = sensitivity.get("baseline_share")
    if not isinstance(baseline_share, (int, float)) or round(
        float(baseline_share) * 100, 4
    ) != round(float(baseline), 4):
        return (
            "has a baseline_share that does not match its own baseline_share_pct "
            f"({baseline_share!r} vs {baseline}%)"
        )
    for key, expected in (
        ("settlement_count", settlements),
        ("seller_count", sellers),
        ("scored_seller_count", scored),
    ):
        if sensitivity.get(key) != expected:
            return (
                f"records {key}={sensitivity.get(key)!r}, but the database has {expected}"
            )
    override_count = sensitivity.get("override_seller_count")
    if not isinstance(override_count, int) or isinstance(override_count, bool):
        return (
            "does not record how many scored sellers a correction held fixed "
            f"(override_seller_count={override_count!r})"
        )
    perturbations = sensitivity.get("perturbations")
    if not isinstance(perturbations, dict) or not perturbations:
        return "has no perturbation table"
    # N3: the report sentence promises every classifier threshold and weight was
    # scaled. Require exactly the key set the producer writes for this classifier; a
    # truncated table can otherwise recompute its own maximum below the limit and
    # remove the instability warning while the sentence stays false.
    missing = EXPECTED_PERTURBATIONS - set(perturbations)
    if missing:
        return (
            f"is missing perturbations {sorted(missing)[:3]}, so it did not scale "
            "every classifier threshold and weight as the report states"
        )
    unexpected = set(perturbations) - EXPECTED_PERTURBATIONS
    if unexpected:
        return f"has perturbations this classifier does not define: {sorted(unexpected)[:3]}"
    shifts: list[float] = []
    for label, entry in perturbations.items():
        shift = entry.get("shift_pp") if isinstance(entry, dict) else None
        if not isinstance(shift, (int, float)) or isinstance(shift, bool):
            return f"perturbation {label!r} has no numeric shift_pp"
        if not math.isfinite(float(shift)):
            return f"perturbation {label!r} has a non-finite shift_pp ({shift!r})"
        shifts.append(abs(float(shift)))
    computed = round(max(shifts), 4)
    reported = sensitivity.get("max_share_shift_pp")
    if not isinstance(reported, (int, float)) or isinstance(reported, bool):
        return f"reports no numeric maximum shift ({reported!r})"
    if not math.isfinite(float(reported)):
        return f"reports a non-finite maximum shift ({reported!r})"
    if round(float(reported), 4) != computed:
        return (
            f"reports a maximum shift of {reported!r} pp, but its own perturbations "
            f"give {computed} pp"
        )
    return None


def main() -> int:
    if not DB_PATH.is_file():
        print(f"{DB_PATH} missing — run scripts/run_phase_a.py first")
        return 1
    # M2/N1/P1: a main-file hash cannot describe a database with uncheckpointed WAL
    # frames, and a hash taken only before the reads can name a state the numbers did
    # not come from. The observer opens before the hash and is checked after the
    # confirming hash, so a commit that happens while the multi-second hash runs is
    # seen even if its bytes are restored afterwards (review P1).
    try:
        with CommitObserver(DB_PATH):
            attested = attest_identity(DB_PATH)

            thresholds = yaml.safe_load(THRESHOLDS.read_text(encoding="utf-8")) or {}
            limit_pp = float(
                thresholds.get("phaseA", {}).get("sensitivity_unstable_share_shift_pp", 15)
            )

            # F9: the committed report kept a pre-change headline because nothing tied
            # it to the database or the classifier that produced it. Refuse stale inputs.
            sensitivity = (
                json.loads(SENSITIVITY.read_text(encoding="utf-8"))
                if SENSITIVITY.is_file()
                else {}
            )
            if sensitivity.get("db_sha256") != attested:
                print(
                    "reports/sensitivity.json was produced from a different database than "
                    "the one on disk (or predates the provenance field). Re-run "
                    "`uv run python scripts/sensitivity.py` before generating the report."
                )
                return 1

            conn = connect_readonly(DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                settlements = conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
                window = conn.execute(
                    "SELECT MIN(block_number) AS lo, MAX(block_number) AS hi, "
                    "MIN(block_time) AS t0, MAX(block_time) AS t1 FROM settlements"
                ).fetchone()
                sellers = conn.execute("SELECT COUNT(*) FROM features").fetchone()[0]
                scored = conn.execute(
                    "SELECT COUNT(*) FROM scores WHERE status='scored'"
                ).fetchone()[0]
                insufficient = conn.execute(
                    "SELECT COUNT(*) FROM scores WHERE status='insufficient_evidence'"
                ).fetchone()[0]
                # K1: count only sellers the index judges. A below-minimum seller's
                # stored classification is not a published likelihood (the API returns
                # null and `judged: false`), so it cannot enter a headline phrased in
                # the API's vocabulary. The unjudged group is counted separately, not
                # hidden.
                flagged = conn.execute(
                    "SELECT COUNT(*) FROM classifications c "
                    "JOIN scores s ON s.identifier = c.seller AND s.status = 'scored' "
                    "WHERE c.signaling_likelihood >= 0.5"
                ).fetchone()[0]
                flagged_settlements = conn.execute(
                    "SELECT COALESCE(SUM(f.settlement_count),0) AS n FROM features f "
                    "JOIN classifications c ON c.seller = f.seller "
                    "JOIN scores s ON s.identifier = c.seller AND s.status = 'scored' "
                    "WHERE c.signaling_likelihood >= 0.5"
                ).fetchone()["n"]
                below_min = conn.execute(
                    "SELECT COUNT(*) FROM classifications c "
                    "LEFT JOIN scores s ON s.identifier = c.seller "
                    "WHERE c.signaling_likelihood >= 0.5 AND COALESCE(s.status, '') != 'scored'"
                ).fetchone()[0]
                below_min_settlements = conn.execute(
                    "SELECT COALESCE(SUM(f.settlement_count),0) AS n FROM features f "
                    "JOIN classifications c ON c.seller = f.seller "
                    "LEFT JOIN scores s ON s.identifier = c.seller "
                    "WHERE c.signaling_likelihood >= 0.5 AND COALESCE(s.status, '') != 'scored'"
                ).fetchone()["n"]
                cluster_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
                nonzero = conn.execute(
                    "SELECT COUNT(*) FROM classifications c "
                    "JOIN scores s ON s.identifier = c.seller AND s.status = 'scored' "
                    "WHERE c.signaling_likelihood > 0"
                ).fetchone()[0]
            finally:
                conn.close()

            # N1: every number below comes from the reads above, and no later step
            # reads the database again. Confirm the identity taken before those reads
            # still holds; the observer then confirms no other connection committed at
            # any point since before the attestation hash.
            require_unchanged(DB_PATH, attested)
    except (PendingWALError, DatabaseChangedError) as exc:
        print(f"{exc} The report is not generated from a state it cannot name.")
        return 1

    flagged_share = round(100.0 * flagged_settlements / settlements, 4) if settlements else 0.0
    below_min_share = (
        round(100.0 * below_min_settlements / settlements, 4) if settlements else 0.0
    )
    below_min_noun = "seller" if below_min == 1 else "sellers"

    # L1/M3: a sensitivity file can match the database hash and still describe a
    # different quantity — the old all-seller count, a rules-only baseline the
    # overrides do not enter, or a hand-inconsistent maximum. The stability figure is
    # published beside the headline, so require the artefact to answer this question.
    problem = _sensitivity_problem(sensitivity, flagged_share, settlements, sellers, scored)
    if problem is not None:
        print(
            f"reports/sensitivity.json {problem}. Re-run "
            "`uv run python scripts/sensitivity.py`."
        )
        return 1

    coverage = json.loads(COVERAGE.read_text(encoding="utf-8")) if COVERAGE.is_file() else {}
    max_shift = float(sensitivity.get("max_share_shift_pp", 0.0))
    # Both were validated above; print the artefact's values so the report does not
    # carry constants that no command reproduces (review finding 7).
    flag_cutoff = float(sensitivity.get("flag_cutoff", 0.5))
    perturbation_pct = int(sensitivity.get("perturbation_pct", 25))

    # G6: the old headline said "unsupported token or mechanism share" immediately
    # above this report's window, but the number came from the Phase 0 seven-day
    # census over an earlier window and counted tokens only. The score database holds
    # only tokens the decoder supports, so an unsupported share over *this* window
    # cannot be measured here. Recompute the census figures from its raw counts and
    # label them with their own window.
    if not TOKEN_CENSUS.is_file():
        print(
            "reports/token_census.json is missing — the Phase 0 unsupported-share "
            "census cannot be reproduced, and this report will not guess it."
        )
        return 1
    census = json.loads(TOKEN_CENSUS.read_text(encoding="utf-8"))
    census_window = census.get("window", {})
    census_total = int(census.get("total_settlements_in_window") or 0)
    unresolved_token_settlements = sum(
        int(token.get("settlement_count") or 0)
        for token in census.get("tokens", [])
        if token.get("resolved") is not True
    )
    phase0_token_pct = (
        round(100.0 * unresolved_token_settlements / census_total, 4) if census_total else None
    )
    mechanism_paths = census.get("mechanism_paths", {})
    eip3009_events = int(mechanism_paths.get("eip3009_authorization_used") or 0)
    unresolved_mechanism_events = int(mechanism_paths.get("unattributed_events") or 0)
    mechanism_total = eip3009_events + unresolved_mechanism_events
    phase0_mechanism_pct = (
        round(100.0 * unresolved_mechanism_events / mechanism_total, 6)
        if mechanism_total
        else None
    )
    if phase0_token_pct is None or phase0_mechanism_pct is None:
        print("reports/token_census.json has no settlement counts; the census is incomplete")
        return 1
    unstable = max_shift > limit_pp

    hours = 0.0
    if window and window["t1"] and window["t0"]:
        hours = (int(window["t1"]) - int(window["t0"])) / 3600.0

    lines: list[str] = []
    lines.append(f"# Genuine commerce versus signaling-like activity — {METHODOLOGY_VERSION}")
    lines.append("")
    if unstable:
        lines.append(
            f"> ## ⚠️ THIS ESTIMATE IS UNSTABLE\n>\n"
            f"> Perturbing the classifier's thresholds and weights by {perturbation_pct}% "
            "moves the "
            f"headline share by up to **{max_shift:.2f} percentage points**, against a "
            f"limit of {limit_pp:.0f}. Treat the percentage below as a direction, not a "
            f"measurement."
        )
        lines.append("")
    lines.append(
        f"**{flagged_share:.2f}% of settlements in this window belong to scored sellers "
        f"with a signaling_likelihood of {flag_cutoff:g} or above.**"
    )
    lines.append("")
    lines.append(
        f"The evidence minimum excludes **{below_min:,}** {below_min_noun} whose stored "
        f"classification is at or above the {flag_cutoff:g} cutoff "
        f"(**{below_min_share:.2f}%** of "
        "settlements in this window). No likelihood is published for them — `/v1/score` "
        "returns `insufficient_evidence` with a null likelihood and `judged: false` — so "
        "they are reported separately here and not counted in the headline."
    )
    lines.append("")
    lines.append("## Data window")
    lines.append("")
    lines.append(f"- Blocks **{window['lo']:,}–{window['hi']:,}**")
    lines.append(f"- Spans **{hours:.1f} hours**")
    lines.append(f"- **{settlements:,}** settlements")
    lines.append("")
    lines.append("## Coverage")
    lines.append("")
    lines.append(
        "**Unsupported-share census (Phase 0, an earlier window"
        + (
            f": blocks {int(census_window['from_block']):,}–{int(census_window['to_block']):,}"
            if census_window.get("from_block") is not None
            and census_window.get("to_block") is not None
            else ""
        )
        + ").** This database stores only tokens the decoder supports, so an "
        "unsupported share cannot be measured over the window above. The seven-day "
        f"census measured **{phase0_token_pct}%** of settlements in an unsupported "
        f"token and **{phase0_mechanism_pct}%** unresolved Permit2 mechanism events. "
        "Those figures describe the census window, not this report's."
    )
    lines.append("")
    for token in coverage.get("supported", []):
        lines.append(f"- Contract `{token['contract']}`: {token['settlements']:,} settlements")
    for mechanism in coverage.get("mechanisms", []):
        lines.append(f"- Mechanism `{mechanism['mechanism']}`: {mechanism['settlements']:,}")
    lines.append("")
    lines.append("## Aggregate results")
    lines.append("")
    lines.append(f"- Sellers observed: **{sellers:,}**")
    lines.append(
        f"- Scored sellers at or above the {flag_cutoff:g} likelihood cutoff: **{flagged:,}**"
    )
    lines.append(
        "- Sellers below the evidence minimum whose stored classification is at or above "
        f"the {flag_cutoff:g} cutoff (no published likelihood): **{below_min:,}**"
    )
    lines.append(f"- Operator clusters proposed: **{cluster_count:,}** (hypotheses, not facts)")
    lines.append(f"- Sellers with a numeric score: **{scored:,}**")
    lines.append(f"- Sellers returning `insufficient_evidence`: **{insufficient:,}**")
    lines.append("")
    if scored == 0 and sellers:
        lines.append(
            "**Nobody received a numeric score.** The evidence minimum is 20 settlements, "
            "5 counterparties and 3 active days; this window spans "
            f"{hours:.1f} hours, so the third condition cannot be met by construction. "
            "That is the method working as designed, not a failure — a short window does "
            "not produce reputations."
        )
        lines.append("")
    if hours < 24 and sellers:
        lines.append(
            f"**This window is too short for two of the rules to judge.** It spans "
            f"{hours:.2f} hours, below the 24-hour minimum the concentration rule requires, "
            f"so `concentrated_in_time` abstained for every seller instead of firing on "
            f"all of them. Scored sellers with a non-zero likelihood: **{nonzero:,}**. "
            f"The {flagged_share:.2f}% figure above describes this short window, not the "
            f"market."
        )
        lines.append("")
    lines.append("## Sensitivity")
    lines.append("")
    lines.append(
        f"Every classifier threshold and weight was scaled by ±{perturbation_pct}%, "
        "one at a time and all at once. Largest move: "
        f"**{max_shift:.2f} percentage points** "
        f"(limit {limit_pp:.0f})."
    )
    if sensitivity.get("perturbations"):
        worst = max(
            sensitivity["perturbations"].items(),
            key=lambda kv: abs(kv[1].get("shift_pp", 0.0)),
        )
        lines.append(f"The result leans most on `{worst[0]}`: {worst[1]['shift_pp']:+.2f} pp.")
    lines.append("")
    lines.append("## What this method cannot know")
    lines.append("")
    lines.append(
        "1. **Settlement counts are cross-checked against a second RPC provider, not an "
        "independent indexer.** Three seeded windows on different days matched exactly in "
        "count and amount on 2026-11-23 (`reports/crosscheck_A.json`). That verifies "
        "decoding and counting against independently fetched logs; it does not prove "
        "chain completeness, and every count here is still one decoder's output. The "
        "original no-zero-cost-indexer limitation was accepted by the owner on "
        "2026-10-23 (`a recorded owner decision (2026-10-23)`)."
    )
    lines.append(
        "2. **An `AuthorizationUsed` event is not provably an x402 settlement.** On chain it "
        "is indistinguishable from an ordinary EIP-3009 transfer. The population is defined "
        "by that event, and the distinction lives off-chain."
    )
    lines.append(
        "3. **The planted-pattern validation is partly circular.** It shows the rules find "
        "patterns the same author planted. It tests mechanics, not real-world truth."
    )
    lines.append(
        "4. **The classifier is rules, not a trained model.** It can only find patterns "
        "someone thought to write down, and the weights are judgement."
    )
    lines.append(
        "5. **A high likelihood is not an accusation.** It describes a payment record. The "
        "project does not label any entity, and no address appears in this report."
    )
    lines.append("")

    # The reproduce table belongs IN the report, not only in the sidecar. Prompt
    # Section 3.1 requires the command printed next to every number, and GA.9 looks for
    # the label in the report body. An earlier version said "the sidecar carries them",
    # which satisfied nobody: the commands ran fine, but all ten sampled checks failed
    # because the report did not contain the labels.
    def entry(label: str, metric: str, expected: object) -> dict[str, object]:
        return {"label": label, "command": f"{METRIC_CMD} {metric}", "expected": expected}

    entries = [
        entry("settlement_count", "settlements", settlements),
        entry("seller_count", "sellers", sellers),
        entry("scored_count", "scored", scored),
        entry("insufficient_count", "insufficient_evidence", insufficient),
        entry("flagged_seller_count", "flagged_sellers", flagged),
        entry("flagged_settlement_share_pct", "flagged_settlement_share_pct", flagged_share),
        entry(
            "flagged_below_minimum_seller_count",
            "flagged_below_minimum_sellers",
            below_min,
        ),
        entry(
            "flagged_below_minimum_settlement_share_pct",
            "flagged_below_minimum_settlement_share_pct",
            below_min_share,
        ),
        entry(
            "nonzero_likelihood_scored_sellers",
            "nonzero_likelihood_scored_sellers",
            nonzero,
        ),
        entry("cluster_count", "clusters", cluster_count),
        entry("window_start_block", "window_start", int(window["lo"])),
        entry("window_end_block", "window_end", int(window["hi"])),
        entry("window_hours", "window_hours", round(hours, 1)),
        entry(
            "phase0_census_window_start_block",
            "phase0_census_window_start_block",
            int(census_window["from_block"]),
        ),
        entry(
            "phase0_census_window_end_block",
            "phase0_census_window_end_block",
            int(census_window["to_block"]),
        ),
        entry(
            "phase0_unsupported_token_share_pct",
            "phase0_unsupported_token_share_pct",
            phase0_token_pct,
        ),
        entry(
            "phase0_unresolved_mechanism_share_pct",
            "phase0_unresolved_mechanism_share_pct",
            phase0_mechanism_pct,
        ),
        entry("flag_cutoff", "flag_cutoff", flag_cutoff),
        entry("sensitivity_max_shift_pp", "sensitivity_max_shift_pp", max_shift),
        entry(
            "sensitivity_perturbation_pct",
            "sensitivity_perturbation_pct",
            perturbation_pct,
        ),
        entry(
            "sensitivity_unstable_share_shift_pp",
            "sensitivity_unstable_share_shift_pp",
            int(limit_pp),
        ),
    ]

    lines.append("## Reproduce")
    lines.append("")
    lines.append(
        "Raw values with the command that produces each. GA.9 re-runs a random sample of "
        "these and compares the printed output to the value here."
    )
    lines.append("")
    lines.append("| label | value | command |")
    lines.append("|---|---|---|")
    for item in entries:
        lines.append(f"| `{item['label']}` | {item['expected']} | `{item['command']}` |")
    lines.append("")

    OUT.write_text("\n".join(lines), encoding="utf-8")

    sidecar = {"report": OUT.name, "entries": entries}
    OUT_REPRO.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")

    print(f"wrote {OUT.relative_to(REPO_ROOT)}")
    print(f"wrote {OUT_REPRO.relative_to(REPO_ROOT)}")
    print(
        f"headline: {flagged_share:.2f}% flagged, unstable={unstable} "
        f"(max shift {max_shift:.2f} pp)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
