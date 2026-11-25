"""Gate A — ingestion, labeling, classifier, rating v0 (prompt Section 7).

⚠️  WRITTEN BEFORE PHASE A IS IMPLEMENTED (prompt rule 3.4.1). Running it now is
    expected to FAIL on nearly every check. That is the correct behaviour.

Run:  make gate-A   (or: uv run python gates/gate_A.py)
"""

from __future__ import annotations

import re
import sqlite3
import sys
from typing import Any

from _shared import (
    BANNED_TERMS,
    REPO_ROOT,
    REPORTS_DIR,
    CheckResult,
    main_wrapper,
    newest_review,
    read_json,
    rel,
    run,
)
from repindex.common.db import connect_readonly

DB = REPO_ROOT / "data" / "repindex.db"
COVERAGE_JSON = REPORTS_DIR / "coverage.json"
COVERAGE_REPORT = REPORTS_DIR / "coverage_report.md"
PLANTED = REPORTS_DIR / "planted_pattern_results.json"
SENSITIVITY = REPORTS_DIR / "sensitivity.json"
V0_REPORT = REPORTS_DIR / "genuine_vs_signaling_v0.md"
V0_REPRO = REPORTS_DIR / "genuine_vs_signaling_v0.reproduce.json"
CROSSCHECK_A = REPORTS_DIR / "crosscheck_A.json"


def _connect() -> sqlite3.Connection | None:
    if not DB.is_file():
        return None
    try:
        conn = connect_readonly(DB)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


# --------------------------------------------------------------------------- #
# GA.1 — tests, lint, types
# --------------------------------------------------------------------------- #


def check_GA_1(thresholds: dict[str, Any]) -> CheckResult:
    """Unit and integration tests pass, ruff is clean, mypy is clean on core modules."""
    a = thresholds.get("phaseA", {})
    result = CheckResult(
        id="GA.1",
        title="Tests pass, ruff clean, mypy clean",
        status="FAIL",
        measured="not measured",
        threshold="pytest exit 0, ruff exit 0, mypy exit 0",
    )

    codes: dict[str, int] = {}
    codes["pytest"], out_test = run(["uv", "run", "pytest", "-q"])
    if codes["pytest"] != 0:
        tail = "\n".join(out_test.strip().splitlines()[-8:])
        result.notes.append(f"pytest failed:\n{tail}")
        result.measured = f"pytest exit {codes['pytest']}"
        return result

    codes["ruff"], out_ruff = run(["uv", "run", "ruff", "check", "."])
    codes["mypy"], out_mypy = run(["uv", "run", "mypy"])

    result.measured = ", ".join(f"{k} exit {v}" for k, v in codes.items())
    for name in ("ruff", "mypy"):
        if codes[name] != 0:
            out = out_ruff if name == "ruff" else out_mypy
            result.notes.append(f"{name} failed:\n" + "\n".join(out.strip().splitlines()[-8:]))
            return result

    # Coverage is a separate check in the prompt (GB.2 for Phase B), but Phase A's
    # core modules should not be untested either.
    if a.get("core_test_coverage_min_pct") is not None:
        result.notes.append(
            "coverage threshold lives under phaseB in config/thresholds.yaml; not "
            "asserted here to avoid inventing a Phase A threshold"
        )

    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GA.2 — data integrity
# --------------------------------------------------------------------------- #


def check_GA_2(thresholds: dict[str, Any]) -> CheckResult:
    """No duplicate keys, no null mandatory fields, raw-to-normalized round-trips."""
    allowed = int(thresholds.get("phaseA", {}).get("duplicate_records_allowed", 0))
    result = CheckResult(
        id="GA.2",
        title="Zero duplicates, no nulls, exact amount round-trip",
        status="FAIL",
        measured="not measured",
        threshold=f"duplicates <= {allowed}; no nulls; raw->normalized->raw exact for every row",
        evidence=[rel(DB)],
    )

    conn = _connect()
    if conn is None:
        result.notes.append(f"{rel(DB)} not found — Phase A ingestion has not run")
        return result

    try:
        tables = _tables(conn)
        if "settlements" not in tables:
            result.notes.append(f"no `settlements` table (found: {sorted(tables) or 'none'})")
            return result

        duplicates = conn.execute(
            "SELECT COUNT(*) FROM (SELECT tx_hash, log_index FROM settlements "
            "GROUP BY tx_hash, log_index HAVING COUNT(*) > 1)"
        ).fetchone()[0]

        mandatory = (
            "tx_hash",
            "log_index",
            "block_number",
            "block_time",
            "payer",
            "seller",
            "token",
            "raw_amount",
            "decimals",
        )
        missing_columns = [
            column
            for column in mandatory
            if column not in {row["name"] for row in conn.execute("PRAGMA table_info(settlements)")}
        ]
        if missing_columns:
            result.notes.append(f"missing mandatory columns: {missing_columns}")
            return result

        null_clause = " OR ".join(f"{column} IS NULL" for column in mandatory)
        nulls = conn.execute(f"SELECT COUNT(*) FROM settlements WHERE {null_clause}").fetchone()[0]

        # Round-trip: normalized must be exactly raw / 10**decimals, and re-deriving
        # raw from the stored normalized value must reproduce the integer exactly.
        bad_roundtrip = conn.execute(
            "SELECT COUNT(*) FROM settlements "
            "WHERE CAST(ROUND(normalized_amount * POWER(10, decimals)) AS INTEGER) != raw_amount"
        ).fetchone()[0]

        # Amounts must be integers in base units, never floats.
        float_amounts = conn.execute(
            "SELECT COUNT(*) FROM settlements WHERE raw_amount != CAST(raw_amount AS INTEGER)"
        ).fetchone()[0]

        total = conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
        result.measured = (
            f"{total:,} rows; duplicates {duplicates}; nulls {nulls}; "
            f"round-trip failures {bad_roundtrip}; non-integer amounts {float_amounts}"
        )

        if duplicates > allowed:
            result.notes.append(f"{duplicates} duplicate (tx_hash, log_index) pairs")
            return result
        if nulls:
            result.notes.append(f"{nulls} rows with a null mandatory field")
            return result
        if bad_roundtrip or float_amounts:
            result.notes.append("amount handling is not exact")
            return result
        if total == 0:
            result.notes.append("table is empty")
            return result

        result.status = "PASS"
        return result
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# GA.3 — cross-check against a second source
# --------------------------------------------------------------------------- #


def check_GA_3(thresholds: dict[str, Any]) -> CheckResult:
    """Cross-check count and total amount against a second independent source."""
    a = thresholds.get("phaseA", {})
    windows = int(a.get("crosscheck_windows", 3))
    max_diff = float(a.get("crosscheck_max_relative_diff", 0.02))

    result = CheckResult(
        id="GA.3",
        title="Agreement with a second source on several random windows",
        status="FAIL",
        measured="not measured",
        threshold=f"{windows} windows on different days; count and amount within {max_diff:.0%}",
        evidence=[rel(CROSSCHECK_A)],
    )

    data = read_json(CROSSCHECK_A)
    if data is None:
        result.notes.append(
            f"{rel(CROSSCHECK_A)} not found — no cross-check has been run. "
            "NOTE: gate check G0.3 failed because no zero-cost independent source exists, "
            "and the owner accepted that limitation on 2026-10-23. This check inherits the "
            "same constraint and cannot pass until a second source is available or the "
            "owner records a replacement decision."
        )
        return result

    runs = data.get("windows")
    if not isinstance(runs, list) or len(runs) < windows:
        result.notes.append(f"need {windows} windows, found {len(runs or [])}")
        return result

    worst = 0.0
    for entry in runs:
        try:
            count_diff = float(entry["count_relative_diff"])
            amount_diff = float(entry["amount_relative_diff"])
        except (KeyError, TypeError, ValueError):
            result.notes.append(f"malformed window entry: {entry!r}")
            return result
        worst = max(worst, count_diff, amount_diff)
        if count_diff > max_diff or amount_diff > max_diff:
            result.notes.append(
                f"window {entry.get('window', '?')}: count {count_diff:.4%}, "
                f"amount {amount_diff:.4%} exceeds {max_diff:.0%}"
            )
            return result

    result.measured = f"{len(runs)} windows, worst relative difference {worst:.4%}"
    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GA.4 — coverage
# --------------------------------------------------------------------------- #


def check_GA_4(thresholds: dict[str, Any]) -> CheckResult:
    """Coverage report lists supported and unsupported tokens/mechanisms, with share."""
    result = CheckResult(
        id="GA.4",
        title="Coverage lists every token and mechanism, and the headline states the gap",
        status="FAIL",
        measured="not measured",
        threshold="coverage lists supported and unsupported; headline states unsupported share",
        evidence=[rel(COVERAGE_JSON), rel(COVERAGE_REPORT)],
    )

    data = read_json(COVERAGE_JSON)
    if data is None:
        result.notes.append(f"{rel(COVERAGE_JSON)} missing — Phase A task A1 has not run")
        return result

    supported = data.get("supported")
    unsupported = data.get("unsupported")
    share = data.get("unsupported_share")
    if not isinstance(supported, list) or not isinstance(unsupported, list):
        result.notes.append("coverage.json must carry `supported` and `unsupported` lists")
        return result
    if not isinstance(share, (int, float)):
        result.notes.append("coverage.json must carry a numeric `unsupported_share`")
        return result

    result.measured = (
        f"{len(supported)} supported, {len(unsupported)} unsupported, "
        f"unsupported share {float(share):.4%}"
    )

    if not COVERAGE_REPORT.is_file():
        result.notes.append(f"{rel(COVERAGE_REPORT)} missing")
        return result

    headline = "\n".join(COVERAGE_REPORT.read_text(encoding="utf-8").splitlines()[:20])
    if f"{float(share) * 100:.2f}" not in headline and f"{float(share) * 100:.1f}" not in headline:
        result.notes.append(
            "the report's first 20 lines do not state the unsupported share; prompt task A1 "
            "requires it in the headline, not a footnote"
        )
        return result

    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GA.5 — rebuild determinism
# --------------------------------------------------------------------------- #


def check_GA_5(thresholds: dict[str, Any]) -> CheckResult:
    """Wipe derived tables, rebuild twice, and compare score-table hashes."""
    result = CheckResult(
        id="GA.5",
        title="Rebuilding the derived tables twice produces identical scores",
        status="FAIL",
        measured="not measured",
        threshold="both rebuilds exit 0 and produce the same scores hash",
    )

    script = REPO_ROOT / "scripts" / "rebuild_derived.py"
    if not script.is_file():
        result.notes.append(f"{rel(script)} missing — the rebuild path Phase A must provide")
        return result

    hashes: list[str] = []
    for attempt in (1, 2):
        code, out = run(["uv", "run", "python", str(script), "--hash-only"])
        if code != 0:
            result.notes.append(f"rebuild {attempt} exited {code}:\n{out.strip()[-400:]}")
            return result
        lines = [line.strip() for line in out.strip().splitlines() if line.strip()]
        if not lines:
            result.notes.append(f"rebuild {attempt} printed nothing")
            return result
        hashes.append(lines[-1])

    result.measured = f"hashes {hashes[0][:16]}… vs {hashes[1][:16]}…"
    if hashes[0] != hashes[1]:
        result.notes.append(
            "the two rebuilds differ, so the same database state does not produce the same "
            "scores — prompt task A5 requires byte-identical output"
        )
        return result

    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GA.6 — reorg handling
# --------------------------------------------------------------------------- #


def check_GA_6(thresholds: dict[str, Any]) -> CheckResult:
    """A simulated reorg rolls ingestion back and recovers with no duplicates."""
    result = CheckResult(
        id="GA.6",
        title="Ingestion rolls back and recovers from a reorg without duplicating",
        status="FAIL",
        measured="not measured",
        threshold="the reorg test exists and passes",
    )

    test_file = REPO_ROOT / "tests" / "test_reorg.py"
    if not test_file.is_file():
        result.notes.append(f"{rel(test_file)} missing — Phase A task A1 must provide it")
        return result

    code, out = run(["uv", "run", "pytest", "-q", str(test_file)])
    result.measured = f"pytest exit {code}"
    if code != 0:
        result.notes.append("\n".join(out.strip().splitlines()[-10:]))
        return result

    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GA.7 — planted patterns
# --------------------------------------------------------------------------- #


def check_GA_7(thresholds: dict[str, Any]) -> CheckResult:
    """Planted-pattern recall and synthetic-genuine false-positive rate."""
    a = thresholds.get("phaseA", {})
    min_recall = float(a.get("planted_pattern_recall_min", 0.90))
    max_fpr = float(a.get("planted_genuine_fpr_max", 0.10))

    result = CheckResult(
        id="GA.7",
        title="The classifier finds planted patterns without flagging synthetic genuine ones",
        status="FAIL",
        measured="not measured",
        threshold=f"recall >= {min_recall:.0%}; false-positive rate <= {max_fpr:.0%}",
        evidence=[rel(PLANTED)],
    )

    # Run the fixtures rather than trusting the JSON on disk. Independent review found
    # the earlier version only read a precomputed file, so a change to the classifier
    # would not have been caught: the gate would keep reading yesterday's result.
    script = REPO_ROOT / "scripts" / "planted_patterns.py"
    if not script.is_file():
        result.notes.append(f"{rel(script)} missing — Phase A task A6 has not run")
        return result

    code, out = run(["uv", "run", "python", str(script)], timeout=600)
    if code != 0:
        result.notes.append(f"planted_patterns.py exited {code}:\n{out.strip()[-500:]}")
        return result

    data = read_json(PLANTED)
    if data is None:
        result.notes.append(f"{rel(PLANTED)} missing after running the fixtures")
        return result

    try:
        recall = float(data["recall"])
        fpr = float(data["synthetic_genuine_fpr"])
    except (KeyError, TypeError, ValueError):
        result.notes.append(
            "planted results must carry numeric `recall` and `synthetic_genuine_fpr`"
        )
        return result

    result.measured = f"recall {recall:.2%}, synthetic-genuine FPR {fpr:.2%}"

    if recall < min_recall:
        result.notes.append(f"recall {recall:.2%} below {min_recall:.0%}")
        return result
    if fpr > max_fpr:
        result.notes.append(f"false-positive rate {fpr:.2%} above {max_fpr:.0%}")
        return result

    if not data.get("circularity_disclosed"):
        result.notes.append(
            "prompt task A6 requires the report to say this test is partly circular and "
            "tests mechanics, not real-world truth; set `circularity_disclosed` when it does"
        )
        return result

    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GA.8 — sensitivity
# --------------------------------------------------------------------------- #


def check_GA_8(thresholds: dict[str, Any]) -> CheckResult:
    """Sensitivity analysis exists and the headline carries the instability statement."""
    a = thresholds.get("phaseA", {})
    limit = float(a.get("sensitivity_unstable_share_shift_pp", 15))

    result = CheckResult(
        id="GA.8",
        title="Sensitivity analysis exists, and instability is stated in the headline if large",
        status="FAIL",
        measured="not measured",
        threshold=f"analysis present; instability stated in the headline if shift > {limit}pp",
        evidence=[rel(SENSITIVITY), rel(V0_REPORT)],
    )

    data = read_json(SENSITIVITY)
    if data is None:
        result.notes.append(f"{rel(SENSITIVITY)} missing — Phase A task A6 has not run")
        return result

    shift = data.get("max_share_shift_pp")
    if not isinstance(shift, (int, float)):
        result.notes.append("sensitivity.json must carry a numeric `max_share_shift_pp`")
        return result

    result.measured = f"max share shift {float(shift):.2f} percentage points (limit {limit})"

    if float(shift) > limit:
        if not V0_REPORT.is_file():
            result.notes.append(f"{rel(V0_REPORT)} missing, so the headline cannot be checked")
            return result
        headline = "\n".join(V0_REPORT.read_text(encoding="utf-8").splitlines()[:25]).lower()
        if "unstable" not in headline:
            result.notes.append(
                f"shift {float(shift):.2f}pp exceeds {limit}pp, so the report's first 25 lines "
                "must state that the estimate is unstable; they do not"
            )
            return result

    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GA.9 — reproducible numbers
# --------------------------------------------------------------------------- #


def check_GA_9(thresholds: dict[str, Any]) -> CheckResult:
    """Re-run a random sample of the report's reproduce commands and compare."""
    result = CheckResult(
        id="GA.9",
        title="A random sample of the report's numbers reproduces",
        status="FAIL",
        measured="not measured",
        threshold="10 sampled commands reproduce their stated value exactly",
        evidence=[rel(V0_REPORT), rel(V0_REPRO)],
    )

    if not V0_REPORT.is_file():
        result.notes.append(f"{rel(V0_REPORT)} missing — Phase A task A7 has not run")
        return result

    data = read_json(V0_REPRO)
    if data is None:
        result.notes.append(
            f"{rel(V0_REPRO)} missing. Every number in the report must carry a reproduce "
            "command (prompt task A7). This sidecar is how the gate finds them."
        )
        return result

    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        result.notes.append("reproduce sidecar carries no `entries`")
        return result

    report_text = V0_REPORT.read_text(encoding="utf-8")
    import random

    rng = random.Random(20261023)
    sample = rng.sample(entries, min(10, len(entries)))

    failures: list[str] = []
    for entry in sample:
        label = entry.get("label")
        command = entry.get("command")
        expected = entry.get("expected")
        if not label or not command or expected is None:
            failures.append(f"malformed entry: {entry!r}")
            continue
        if str(label) not in report_text:
            failures.append(f"label {label!r} does not appear in the report")
            continue
        if str(expected) not in report_text:
            failures.append(f"value {expected!r} for {label!r} does not appear in the report")
            continue
        code, out = run(["bash", "-lc", str(command)], timeout=600)
        actual = out.strip().splitlines()[-1] if out.strip() else ""
        if code != 0:
            failures.append(f"{label}: command exited {code}")
        elif actual != str(expected):
            failures.append(f"{label}: printed {actual!r}, report says {expected!r}")

    result.measured = f"{len(sample)} commands sampled, {len(failures)} failed"
    if failures:
        result.notes.extend(failures[:10])
        return result

    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GA.10 — language audit
# --------------------------------------------------------------------------- #


def check_GA_10(thresholds: dict[str, Any]) -> CheckResult:
    """Banned terms from prompt Section 3.2 do not appear in generated output."""
    result = CheckResult(
        id="GA.10",
        title="Language audit passes on generated reports",
        status="FAIL",
        measured="not measured",
        threshold="no banned term used as an assertion in reports/ or public strings",
        evidence=[rel(REPORTS_DIR)],
    )

    # Audit the product's own output. Meta documents are excluded on purpose: the
    # review files and the gate reports discuss the rule, and quoting it is not a
    # violation. Measured 2026-10-23, the naive version of this check reported 15
    # hits, every one of them a reviewer's own `grep` command — and gate_A.md then
    # contained those hits, which would have made the count grow on every run.
    META = ("review_", "gate_", "gate_changes", "failure_analysis")
    targets = [p for p in sorted(REPORTS_DIR.glob("*.md")) if not p.name.startswith(META)]
    if not targets:
        result.notes.append("no generated product reports to audit")
        return result

    hits: list[str] = []
    for path in targets:
        text = path.read_text(encoding="utf-8", errors="replace")
        # Fenced code blocks hold commands, not prose. A grep expression that names
        # the banned terms is the check, not a use of them.
        in_fence = False
        for line_number, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for term in BANNED_TERMS:
                if re.search(rf"\b{re.escape(term)}\w*\b", line, re.IGNORECASE):
                    hits.append(f"{rel(path)}:{line_number}: {term!r}")

    result.measured = f"{len(targets)} reports scanned, {len(hits)} hit(s)"
    if hits:
        result.notes.extend(hits[:10])
        result.notes.append(
            "Prompt Section 3.2 forbids these as assertions about an address or entity. "
            "Quoting the rule is not a violation; using the word to describe someone is."
        )
        return result

    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GA.11 — insufficient evidence
# --------------------------------------------------------------------------- #


def check_GA_11(thresholds: dict[str, Any]) -> CheckResult:
    """Scores below the evidence minimum render as insufficient_evidence, never zero."""
    result = CheckResult(
        id="GA.11",
        title="Below-minimum evidence renders as insufficient_evidence, never as zero",
        status="FAIL",
        measured="not measured",
        threshold="a passing test proves the literal string is returned and no zero is",
    )

    test_file = REPO_ROOT / "tests" / "test_insufficient_evidence.py"
    if not test_file.is_file():
        result.notes.append(f"{rel(test_file)} missing — Phase A task A5 must provide it")
        return result

    code, out = run(["uv", "run", "pytest", "-q", str(test_file)])
    result.measured = f"pytest exit {code}"
    if code != 0:
        result.notes.append("\n".join(out.strip().splitlines()[-10:]))
        return result

    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GA.12 — independent review
# --------------------------------------------------------------------------- #


def check_GA_12(thresholds: dict[str, Any]) -> CheckResult:
    """An independent reviewer ran and left no unresolved findings."""
    review_path, all_reviews = newest_review("A")
    result = CheckResult(
        id="GA.12",
        title="Independent reviewer ran with no unresolved findings",
        status="FAIL",
        measured="not measured",
        threshold="the newest reports/review_A*.md declares 0 unresolved findings",
        evidence=[rel(p) for p in all_reviews] or [rel(review_path)],
    )

    if not review_path.is_file():
        result.notes.append(
            f"{rel(review_path)} missing — the reviewer has not run. It must specifically "
            "hunt for leakage between the synthetic test and the real data, and for "
            "circular validation (prompt GA.12)."
        )
        return result

    matches = re.findall(
        r"^\s*(?:[-*]\s*)?unresolved\s+findings?\s*[:=]\s*(\d+)\s*$",
        review_path.read_text(encoding="utf-8"),
        re.IGNORECASE | re.MULTILINE,
    )
    if not matches:
        result.notes.append(
            "the review must state 'unresolved findings: <n>' so this check can read it"
        )
        return result

    total = max(int(m) for m in matches)
    result.measured = f"{review_path.name} declares {total} unresolved finding(s)"
    if total > 0:
        result.notes.append("unresolved findings count as FAIL (prompt rule 3.4.7)")
        return result

    result.status = "PASS"
    return result


def check_GA_13(thresholds: dict[str, Any]) -> CheckResult:
    """Blinded adjudication agreement, with its own controls (prompt task A6).

    Replaces the human spot-check. The owner decided on 2026-10-28 that reading fifty
    sellers by hand is not human work; the deeper problem was that the old check could
    not fail. Measured: 436 of 444 sellers had likelihood exactly 0.00, so forty-two
    of the fifty cards carried no evidence of anything, and agreement came out near
    84% whether or not the classifier worked.

    The design and its limits are in `docs/SPOT_CHECK_RUBRIC.md`, written before any
    adjudication ran. Every condition below is stricter than the old one. The negative
    control is what replaces human scrutiny: with no human in the loop it is the only
    thing between a broken classifier and a green light.

    Returns FAIL rather than WARN when the sample is not discriminative. The plan said
    WARN, but WARN does not block in this gate's runner, so a null result would have
    read as success — the exact failure this check exists to remove.
    """
    phase_a = thresholds.get("phaseA", {})
    min_agreement = float(phase_a.get("spot_check_agreement_min", 0.70))
    min_flagged = int(phase_a.get("min_flagged_in_sample", 10))
    inter_min = float(phase_a.get("inter_rater_min", 0.70))
    negative_max = float(phase_a.get("negative_control_max", 0.40))
    max_abstention = float(phase_a.get("max_abstention", 0.30))

    result_json = REPORTS_DIR / "adjudication" / "result.json"

    result = CheckResult(
        id="GA.13",
        title="Blinded adjudication agrees, and the adjudication itself can fail",
        status="FAIL",
        measured="not measured",
        threshold=(
            f">= {min_flagged} flagged in sample; agreement >= {min_agreement:.0%} both "
            f"adjudicators; inter-rater >= {inter_min:.0%}; negative control <= "
            f"{negative_max:.0%}; no advertised service flagged; abstention <= "
            f"{max_abstention:.0%}"
        ),
        evidence=[rel(result_json), "docs/SPOT_CHECK_RUBRIC.md"],
    )

    data = read_json(result_json)
    if data is None:
        result.notes.append(
            f"{rel(result_json)} missing — the adjudication has not run. Build the packets "
            "with scripts/adjudication_packets.py, have two independent adjudicators answer "
            "from packets.json alone, then score them with scripts/validate_adjudication.py."
        )
        return result

    checks = data.get("checks") or {}
    required = (
        "discriminative",
        "agreement_adjudicator_1",
        "agreement_adjudicator_2",
        "inter_rater",
        "negative_control",
        "external_anchor",
        "abstention",
    )
    missing = [name for name in required if name not in checks]
    if missing:
        result.notes.append(f"result.json is missing checks: {missing}")
        return result

    result.measured = (
        f"{data.get('flagged_sellers', 0)} flagged in sample; "
        f"agreement {checks['agreement_adjudicator_1']['value']:.1%} / "
        f"{checks['agreement_adjudicator_2']['value']:.1%}; "
        f"inter-rater {checks['inter_rater']['value']:.1%}; "
        f"negative control {checks['negative_control']['value']:.1%}; "
        f"abstention {checks['abstention']['value']:.1%}; "
        f"{checks['external_anchor']['value']} advertised services flagged"
    )

    if not checks["discriminative"]["pass"]:
        result.notes.append(
            f"NOT DISCRIMINATIVE: the sample contains {checks['discriminative']['value']} "
            f"flagged sellers, below {min_flagged}. A sample where almost every seller has "
            "no evidence cannot tell a working classifier from a broken one, so this is a "
            "FAIL rather than a pass. Backfill more data or fix the classifier first."
        )
        return result

    if not checks["negative_control"]["pass"]:
        result.notes.append(
            f"the adjudication agreed with a deliberately broken classifier "
            f"{checks['negative_control']['value']:.1%} of the time, above "
            f"{negative_max:.0%}. That means it is not reading the evidence; the "
            "agreement numbers above mean nothing until this is fixed."
        )
        return result

    if not checks["external_anchor"].get("verified", False):
        result.notes.append(
            "the external anchor could not be fetched, so the false-positive check against "
            "advertised services is UNVERIFIED rather than zero"
        )
        return result

    for name in required:
        if not checks[name]["pass"]:
            result.notes.append(
                f"{name}: {checks[name]['value']} against a threshold of "
                f"{checks[name]['threshold']}"
            )
    if result.notes:
        return result

    result.status = "PASS"
    return result


CHECKS = (
    check_GA_1,
    check_GA_2,
    check_GA_3,
    check_GA_4,
    check_GA_5,
    check_GA_6,
    check_GA_7,
    check_GA_8,
    check_GA_9,
    check_GA_10,
    check_GA_11,
    check_GA_12,
    check_GA_13,
)


if __name__ == "__main__":
    sys.exit(
        main_wrapper(
            "A",
            list(CHECKS),
            # The database is deliberately NOT in the snapshot. GA.5 rebuilds the
            # derived tables, which mutates the file, so a hash that included it could
            # never be reproduced after the gate ran — independent review found exactly
            # that. The snapshot describes the source artefacts, which the gate does not
            # change.
            [COVERAGE_JSON, PLANTED, SENSITIVITY, V0_REPORT, V0_REPRO],
        )
    )
