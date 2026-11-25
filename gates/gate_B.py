"""Gate B — score API, MCP tool, agent card, public analysis (prompt Section 8).

⚠️  WRITTEN BEFORE PHASE B IS IMPLEMENTED (prompt rule 3.4.1). Running it now is
    expected to FAIL on nearly every check. That is the correct behaviour.

Two checks go beyond the prompt's GB.1-GB.11, added by owner decisions:

  GB.12 — every API and MCP response must carry a disclaimer saying the score has not
          passed its own validation. The owner chose to expose the score at 60% agreement
          on 2026-10-30, so the limitation has to travel with the number.
  GB.13 — identity binding. The owner decided Zapproof will act as a Lightning trust
          provider, and ADR 0003 records that attribution can never come from routing.
          Without a working binding there is no attribution at all.

Four further checks come from the independent Gate B review (reports/review_B.md):

  GB.14 — provenance (F12): the gate must not report a measurement it did not take
          from the commit and database it names.
  GB.15 — packaging (F8: prompt task B6 was never done).
  GB.16 — public analysis (F8: prompt task B4).
  GB.17 — usage metrics (F8: prompt task B7), which the market gate depends on.

Run:  make gate-B   (or: uv run python gates/gate_B.py)
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

from _shared import (
    BANNED_TERMS,
    REPO_ROOT,
    REPORTS_DIR,
    CheckResult,
    git_commit,
    git_dirty_paths,
    main_wrapper,
    newest_review,
    read_json,
    rel,
    run,
    sha256_file,
)

OPENAPI_JSON = REPORTS_DIR / "openapi.json"
COVERAGE_JSON = REPORTS_DIR / "coverage_B.json"
LOADTEST_JSON = REPORTS_DIR / "loadtest_B.json"
FUZZ_JSON = REPORTS_DIR / "fuzz_identifier.json"
WRITE_PROBE = REPORTS_DIR / "write_probe.json"
SECRET_SCAN = REPORTS_DIR / "secret_scan.json"
DETERMINISM_JSON = REPORTS_DIR / "determinism_B.json"
MCP_TEST_JSON = REPORTS_DIR / "mcp_test.json"
AGENT_CARD = REPO_ROOT / "src" / "repindex" / "api" / "static" / "agent-card.json"
DISPUTES = REPO_ROOT / "DISPUTES.md"
CORRECTIONS_TEST = REPO_ROOT / "tests" / "test_corrections.py"
BINDING_TEST = REPO_ROOT / "tests" / "test_binding.py"
BINDING_E2E_SCRIPT = REPO_ROOT / "scripts" / "binding_e2e.py"
BINDING_E2E = REPORTS_DIR / "binding_e2e.json"
DISCLAIMER_LOG = REPORTS_DIR / "disclaimer_check.json"
DATA_DB = REPO_ROOT / "data" / "repindex.db"
DATA_WAL = REPO_ROOT / "data" / "repindex.db-wal"
MAKEFILE = REPO_ROOT / "Makefile"
DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
DEPLOY_DOC = REPO_ROOT / "docs" / "deploy_local.md"
ANALYSIS_DIR = REPORTS_DIR / "analysis"
PUBLIC_ANALYSIS = REPORTS_DIR / "phase_B_public_analysis.md"
PUBLIC_ANALYSIS_PROV = ANALYSIS_DIR / "provenance.json"
METRICS_SCRIPT = REPO_ROOT / "scripts" / "metrics.py"
METRICS_JSON = REPORTS_DIR / "usage_metrics.json"
GATE_CHANGES = REPORTS_DIR / "gate_changes.md"

# Committed paths the gate measures. Reports and data are outputs, not source.
SOURCE_PATHS = [
    "src",
    "scripts",
    "gates",
    "tests",
    "docs",
    "config",
    "pyproject.toml",
    "Makefile",
    "Dockerfile",
    "docker-compose.yml",
]

# Producers are expensive; each one runs at most once per gate invocation.
_EVIDENCE: dict[str, tuple[int, str]] = {}


def _producer(name: str, cmd: list[str], timeout: int = 900) -> tuple[int, str]:
    if name not in _EVIDENCE:
        _EVIDENCE[name] = run(cmd, timeout=timeout)
    return _EVIDENCE[name]


def _verify_api() -> tuple[int, str]:
    return _producer("verify_api", ["uv", "run", "python", "scripts/verify_api.py"], 300)


def _tail(out: str, lines: int = 10) -> str:
    return "\n".join(out.strip().splitlines()[-lines:])


# (stat key, hash): a one-entry cache; the DB is 1.6 GB to hash.
_DB_IDENTITY: list[tuple[tuple[int, int], str]] = []


def _db_hash() -> str | None:
    """The main-file hash, or None while the WAL holds changes or the file is gone.

    M2: a main-file hash cannot describe a database with uncheckpointed WAL frames.
    N1: the WAL is checked on every call and the cache is keyed on the main file's
    (size, mtime_ns), so a database that changes after the first check is hashed
    again instead of being judged against an identity taken before the change.
    """
    wal = DATA_DB.with_name(DATA_DB.name + "-wal")
    if wal.is_file() and wal.stat().st_size > 0:
        return None
    try:
        stat = DATA_DB.stat()
    except OSError:
        return None
    key = (stat.st_size, stat.st_mtime_ns)
    if _DB_IDENTITY and _DB_IDENTITY[0][0] == key:
        return _DB_IDENTITY[0][1]
    digest = sha256_file(DATA_DB)
    if digest is None:
        return None
    _DB_IDENTITY.clear()
    _DB_IDENTITY.append((key, digest))
    return digest


def _freshness(data: dict[str, Any] | None, result: CheckResult) -> bool:
    """Reject an artefact that was produced from other code or other data (F12)."""
    if data is None:
        return False
    db = _db_hash()
    if db is None:
        result.notes.append(
            "the database is missing or has uncheckpointed WAL frames, so its "
            "main-file hash does not describe its state; checkpoint it before "
            "attesting artefacts (review M2)"
        )
        return False
    if data.get("db_sha256") != db:
        result.notes.append(
            "artefact was produced from a different database than the one on disk "
            f"({str(data.get('db_sha256'))[:16]}… vs {str(db)[:16]}…)"
        )
        return False
    if data.get("commit") != git_commit():
        result.notes.append(
            "artefact was produced from a different commit than HEAD "
            f"({str(data.get('commit'))[:12]}… vs {git_commit()[:12]}…)"
        )
        return False
    return True


# The phrase every response must carry. Tests assert it is present; the gate greps.
DISCLAIMER_MARKER = "not independently validated"


# --------------------------------------------------------------------------- #
# GB.1 — OpenAPI schema and contract tests
# --------------------------------------------------------------------------- #


def check_GB_1(thresholds: dict[str, Any]) -> CheckResult:
    """OpenAPI schema generated, and schema-driven contract tests pass."""
    result = CheckResult(
        id="GB.1",
        title="OpenAPI schema exists and contract tests pass",
        status="FAIL",
        measured="not measured",
        threshold="reports/openapi.json present; schemathesis (or equivalent) exit 0",
        evidence=[rel(OPENAPI_JSON)],
    )

    code, out = _verify_api()
    if code != 0:
        result.notes.append(f"the schema producer failed (exit {code}):\n{_tail(out)}")
        return result

    if not OPENAPI_JSON.is_file():
        result.notes.append(f"{rel(OPENAPI_JSON)} was not written by the producer")
        return result

    schema = read_json(OPENAPI_JSON)
    if schema is None or "paths" not in schema:
        result.notes.append(f"{rel(OPENAPI_JSON)} is not a valid OpenAPI document")
        return result

    paths = [p for p in schema["paths"] if p.startswith("/v1/")]
    result.measured = f"{len(paths)} versioned path(s)"
    if not paths:
        result.notes.append("no /v1/ paths found in the schema")
        return result

    code, out = run(["uv", "run", "pytest", "-q", "tests/test_api_contract.py"], timeout=900)
    if code != 0:
        result.notes.append("contract tests failed:\n" + "\n".join(out.strip().splitlines()[-10:]))
        return result

    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GB.2 — coverage
# --------------------------------------------------------------------------- #


def check_GB_2(thresholds: dict[str, Any]) -> CheckResult:
    """Line coverage of core modules."""
    minimum = float(thresholds.get("phaseB", {}).get("core_test_coverage_min_pct", 85))
    result = CheckResult(
        id="GB.2",
        title="Line coverage of core modules meets the minimum",
        status="FAIL",
        measured="not measured",
        threshold=f"coverage >= {minimum:.0f}%",
        evidence=[rel(COVERAGE_JSON)],
    )

    # Run the measurement rather than trust an artifact. F1: the committed artifact
    # claimed 95.09% from tests that were not in the repository.
    code, out = _producer(
        "coverage",
        [
            "uv",
            "run",
            "pytest",
            "-q",
            "--cov=src/repindex/api",
            "--cov=src/repindex/mcp",
            f"--cov-report=json:{COVERAGE_JSON}",
            "--cov-report=term-missing",
        ],
        timeout=900,
    )
    if code != 0:
        result.notes.append(f"the coverage run failed (exit {code}):\n{_tail(out)}")
        return result

    data = read_json(COVERAGE_JSON)
    if data is None:
        result.notes.append(f"{rel(COVERAGE_JSON)} was not written by the coverage run")
        return result

    total = data.get("totals", {}).get("percent_covered")
    if not isinstance(total, (int, float)):
        result.notes.append("coverage json has no totals.percent_covered")
        return result

    files = data.get("files", {})
    mcp = next(
        (v for k, v in files.items() if k.replace("\\", "/").endswith("mcp/server.py")),
        None,
    )
    mcp_pct = float(mcp.get("summary", {}).get("percent_covered", 0.0)) if mcp else 0.0

    result.measured = f"{float(total):.2f}% (mcp/server.py {mcp_pct:.1f}%)"
    if float(total) < minimum:
        result.notes.append(f"{float(total):.2f}% is below {minimum:.0f}%")
        return result
    if mcp_pct <= 0.0:
        result.notes.append(
            "mcp/server.py has 0% coverage; the artifact's scope is not the suite's scope"
        )
        return result

    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GB.3 — load test
# --------------------------------------------------------------------------- #


def check_GB_3(thresholds: dict[str, Any]) -> CheckResult:
    """Sustained load, cached p95, and error rate."""
    b = thresholds.get("phaseB", {})
    rps = float(b.get("api_sustained_rps", 20))
    seconds = float(b.get("api_load_test_seconds", 120))
    p95_max = float(b.get("api_p95_ms_cached", 300))
    err_max = float(b.get("api_error_rate_max", 0.01))

    result = CheckResult(
        id="GB.3",
        title="Load test: throughput, cached p95 latency, error rate",
        status="FAIL",
        measured="not measured",
        threshold=(
            f"{rps:.0f} rps for {seconds:.0f}s; p95 <= {p95_max:.0f} ms; errors <= {err_max:.0%}"
        ),
        evidence=[rel(LOADTEST_JSON)],
    )

    # Run the load test rather than trust an artifact (F2). The old script measured
    # the rate limiter's ceiling and reported it as service throughput.
    code, out = _producer(
        "loadtest", ["uv", "run", "python", "scripts/loadtest.py"], timeout=600
    )
    if code != 0:
        result.notes.append(f"the load test failed (exit {code}):\n{_tail(out)}")
        return result

    data = read_json(LOADTEST_JSON)
    if data is None:
        result.notes.append(f"{rel(LOADTEST_JSON)} was not written by the load test")
        return result

    for key in (
        "achieved_rps",
        "p95_ms",
        "error_rate",
        "duration_seconds",
        "generating_seconds",
        "machine",
        "target_rps",
        "requests",
        "throttled",
    ):
        if key not in data:
            result.notes.append(f"load test result is missing `{key}`")
            return result
    if float(data["generating_seconds"]) <= 0:
        result.notes.append("the load test recorded no request window")
        return result

    if not _freshness(data, result):
        return result

    result.measured = (
        f"{data['achieved_rps']} rps, p95 {data['p95_ms']} ms, "
        f"errors {data['error_rate']:.2%}, throttled {data['throttled']}, {data['machine']}"
    )

    expected_requests = float(data["target_rps"]) * float(data["duration_seconds"])
    if float(data["achieved_rps"]) < rps:
        result.notes.append("throughput below target")
    if float(data["p95_ms"]) > p95_max:
        result.notes.append("p95 above target")
    if float(data["error_rate"]) > err_max:
        result.notes.append("error rate above target")
    if float(data["duration_seconds"]) < seconds:
        result.notes.append("the run was shorter than the required duration")
    if int(data["throttled"]) != 0:
        result.notes.append(
            f"{data['throttled']} request(s) were throttled at the target rate; the "
            "service cannot carry the load it claims"
        )
    if float(data["requests"]) < expected_requests * 0.9:
        result.notes.append(
            f"only {data['requests']} requests for an expected {expected_requests:.0f}; "
            "the client did not generate the load it reports"
        )
    if result.notes:
        return result

    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GB.4 — security
# --------------------------------------------------------------------------- #


def check_GB_4(thresholds: dict[str, Any]) -> CheckResult:
    """Dependency audit, secret scan, no write endpoints, fuzz-safe parsing."""
    result = CheckResult(
        id="GB.4",
        title="Security: dependencies, secrets, read-only, fuzz-safe identifiers",
        status="FAIL",
        measured="not measured",
        threshold="no high/critical advisories; secret scan clean; no write routes; fuzz passes",
        evidence=[rel(FUZZ_JSON), rel(WRITE_PROBE), rel(SECRET_SCAN)],
    )

    code, out = run(["uv", "run", "pip-audit", "--format", "json"], timeout=900)
    if code not in (0, 1):
        result.notes.append(f"pip-audit could not run (exit {code})")
        return result
    # pip-audit prints a human line ("Found 19 known vulnerabilities...") before the
    # JSON, so the payload starts at the first brace rather than the first character.
    # `run()` returns stdout followed by stderr, and pip-audit writes its human
    # summary to stderr. So the JSON is at the front and there may be trailing text
    # after it: raw_decode parses the first JSON value and ignores the rest, which
    # json.loads cannot do.
    start = out.find("{")
    if start < 0:
        result.notes.append("pip-audit produced no JSON")
        return result
    try:
        audit, _ = json.JSONDecoder().raw_decode(out[start:])
    except json.JSONDecodeError:
        result.notes.append("pip-audit output could not be parsed")
        return result

    # pip-audit's JSON carries no severity field, so "no high or critical" cannot be
    # judged from it. Any advisory therefore fails. That is stricter than the prompt
    # asks and is the only defensible reading when the data needed to apply the stated
    # rule is absent: passing because a field is missing is how a security gate becomes
    # decoration.
    vulnerable = [
        (dep.get("name"), dep.get("version"), len(dep.get("vulns", [])))
        for dep in audit.get("dependencies", [])
        if dep.get("vulns")
    ]
    if vulnerable:
        result.measured = f"{len(vulnerable)} package(s) with advisories"
        result.notes.append(
            "advisories found (severity unavailable, treated as blocking): "
            + ", ".join(f"{n} {v} ({c})" for n, v, c in vulnerable[:5])
        )
        return result

    # F6: the old threshold claimed a secret scan and a write test that the gate never
    # ran. Both are measured now.
    gitleaks = shutil.which("gitleaks")
    if not gitleaks:
        result.notes.append("gitleaks is not installed; the secret scan was not measured")
        return result
    scan_code, scan_out = run(
        [
            gitleaks,
            "git",
            "--no-banner",
            "--redact",
            "--report-format",
            "json",
            "--report-path",
            str(SECRET_SCAN),
        ],
        timeout=600,
    )
    if scan_code != 0:
        result.notes.append(
            "gitleaks found a potential secret (exit "
            f"{scan_code}): {_tail(scan_out, 5)}"
        )
        return result

    code, out = _verify_api()
    if code != 0:
        result.notes.append(f"the API evidence producer failed (exit {code}):\n{_tail(out)}")
        return result

    fuzz = read_json(FUZZ_JSON)
    writes = read_json(WRITE_PROBE)
    if fuzz is None or writes is None:
        result.notes.append("the fuzz or write-probe artefact is missing")
        return result
    if not _freshness(fuzz, result):
        return result
    if fuzz.get("crashes"):
        result.notes.append(f"{fuzz['crashes']} parser crash(es) under fuzzing")
        return result
    if writes.get("writes_attempted") or not writes.get("all_refused"):
        result.notes.append("an endpoint attempted a write; the API must be read-only")
        return result
    if not writes.get("db_unchanged"):
        result.notes.append("the database hash changed across the write probe")
        return result

    result.measured = (
        f"0 high/critical advisories; gitleaks clean; {fuzz.get('cases', 0)} fuzz cases, "
        f"0 crashes; writes refused, db unchanged"
    )
    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GB.5 — determinism
# --------------------------------------------------------------------------- #


def check_GB_5(thresholds: dict[str, Any]) -> CheckResult:
    """The same request at the same as_of_block returns a byte-identical body."""
    result = CheckResult(
        id="GB.5",
        title="Identical requests at the same as_of_block return identical bytes",
        status="FAIL",
        measured="not measured",
        threshold="two responses for the same identifier and block hash identically",
        evidence=[rel(DETERMINISM_JSON)],
    )

    code, out = _verify_api()
    if code != 0:
        result.notes.append(f"the API evidence producer failed (exit {code}):\n{_tail(out)}")
        return result

    data = read_json(DETERMINISM_JSON)
    if data is None:
        result.notes.append(f"{rel(DETERMINISM_JSON)} was not written")
        return result
    if not _freshness(data, result):
        return result

    first, second = data.get("first_hash"), data.get("second_hash")
    control = data.get("negative_control_hash")
    if not first or not second:
        result.notes.append("determinism result must carry two hashes")
        return result
    if not control:
        result.notes.append("determinism result must carry a negative-control hash")
        return result

    result.measured = f"{str(first)[:16]}… vs {str(second)[:16]}… (control differs)"
    if first != second:
        result.notes.append(
            "the two responses differ. A response that changes between identical reads "
            "cannot be reproduced by a third party, which is the project's whole claim."
        )
        return result
    if control == first:
        result.notes.append(
            "the negative control has the same hash; the check cannot distinguish "
            "one identifier's response from another's"
        )
        return result

    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GB.6 — MCP tool
# --------------------------------------------------------------------------- #


def check_GB_6(thresholds: dict[str, Any]) -> CheckResult:
    """The MCP tool works over stdio for the three required cases."""
    result = CheckResult(
        id="GB.6",
        title="MCP tool answers invalid, unknown, and insufficient-evidence cases",
        status="FAIL",
        measured="not measured",
        threshold="all three cases pass via the stdio client",
        evidence=[rel(MCP_TEST_JSON)],
    )

    code, out = _producer(
        "mcp_stdio", ["uv", "run", "python", "scripts/mcp_stdio_test.py"], timeout=300
    )
    if code != 0:
        result.notes.append(f"the MCP stdio test failed (exit {code}):\n{_tail(out)}")
        return result

    data = read_json(MCP_TEST_JSON)
    if data is None:
        result.notes.append(f"{rel(MCP_TEST_JSON)} was not written by the stdio test")
        return result
    if not _freshness(data, result):
        return result

    cases = data.get("cases", {})
    required = ("invalid_identifier", "unknown_address", "insufficient_evidence")
    missing = [c for c in required if c not in cases]
    if missing:
        result.notes.append(f"MCP test is missing cases: {missing}")
        return result

    failed = [c for c in required if not cases[c].get("pass")]
    result.measured = f"{len(required) - len(failed)}/{len(required)} cases passed"
    if failed:
        for c in failed:
            result.notes.append(f"{c}: {cases[c].get('detail', 'no detail')}")
        return result

    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GB.7 — agent card
# --------------------------------------------------------------------------- #


def check_GB_7(thresholds: dict[str, Any]) -> CheckResult:
    """Agent card is served and valid, or its status is explicitly UNVERIFIED."""
    result = CheckResult(
        id="GB.7",
        title="Agent card validates against the A2A specification, or is marked UNVERIFIED",
        status="FAIL",
        measured="not measured",
        threshold=("card validates, or docs/a2a_notes.md records UNVERIFIED with a reason"),
        evidence=[rel(AGENT_CARD), "docs/a2a_notes.md"],
    )

    notes_path = REPO_ROOT / "docs" / "a2a_notes.md"
    if not AGENT_CARD.is_file():
        result.notes.append(f"{rel(AGENT_CARD)} missing")
        return result

    card = read_json(AGENT_CARD)
    if card is None:
        result.notes.append(f"{rel(AGENT_CARD)} is not valid JSON")
        return result

    for field in ("name", "description", "url", "version"):
        if not card.get(field):
            result.notes.append(f"agent card is missing `{field}`")
            return result

    if not notes_path.is_file():
        result.notes.append("docs/a2a_notes.md is missing; the card's status is unstated")
        return result

    notes = notes_path.read_text(encoding="utf-8")
    has_unverified = "UNVERIFIED" in notes
    # F15: the card carried a placeholder protocolVersion presented as fact. A card
    # may assert a version only when the notes record which specification that version
    # came from and when it was checked.
    has_spec_version = bool(
        re.search(r"spec(?:ification)?\s+version\s*[:=]", notes, re.IGNORECASE)
    )
    has_checked_date = bool(
        re.search(r"(checked|fetched|retrieved)\s+(on\s+)?\d{4}-\d{2}-\d{2}", notes, re.IGNORECASE)
    )
    asserted_version = card.get("protocolVersion")

    if asserted_version and not (has_spec_version and has_checked_date):
        result.measured = f"card asserts protocolVersion {asserted_version!r}"
        result.notes.append(
            "the card asserts a protocol version but docs/a2a_notes.md records neither "
            "the specification version nor the date it was checked. Remove the field or "
            "verify it (F15)."
        )
        return result

    if has_unverified:
        result.measured = "card present; validation explicitly recorded as UNVERIFIED"
        result.status = "PASS"
        if not asserted_version:
            result.notes.append(
                "the card omits protocolVersion because no specification was fetched"
            )
        return result

    result.measured = "card present"
    result.notes.append(
        "no UNVERIFIED note in docs/a2a_notes.md, so the card is being asserted as valid. "
        "If validation was not actually performed against the published schema, say so."
    )
    result.status = "NEEDS_HUMAN"
    return result


# --------------------------------------------------------------------------- #
# GB.8 — unknown is not zero
# --------------------------------------------------------------------------- #


def check_GB_8(thresholds: dict[str, Any]) -> CheckResult:
    """An unknown identifier never produces a numeric score, at API and MCP level."""
    result = CheckResult(
        id="GB.8",
        title="Unknown is not zero, at both the API and the MCP tool",
        status="FAIL",
        measured="not measured",
        threshold="a passing test proves found:false and no numeric score",
    )

    test_file = REPO_ROOT / "tests" / "test_unknown_is_not_zero.py"
    if not test_file.is_file():
        result.notes.append(f"{rel(test_file)} missing")
        return result

    code, out = run(["uv", "run", "pytest", "-q", str(test_file)])
    result.measured = f"pytest exit {code}"
    if code != 0:
        result.notes.append("\n".join(out.strip().splitlines()[-10:]))
        return result

    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GB.9 — language
# --------------------------------------------------------------------------- #


def _published_report_names() -> tuple[str, ...]:
    """The report files `scripts/export_public.py` copies into the public repository.

    Read from the exporter instead of keeping a second list: a report that becomes
    public joins the audit, and one that stays internal does not fail GB.9 for quoting
    the rule it follows. If the exporter cannot be read, every report is audited — the
    stricter set.
    """
    path = REPO_ROOT / "scripts" / "export_public.py"
    try:
        spec = importlib.util.spec_from_file_location("_zapproof_export_public", path)
        if spec is None or spec.loader is None:
            raise ImportError("export_public.py has no loader")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return tuple(str(name) for name in module.COPY_REPORTS)
    except Exception:  # a gate must not crash on its own allowlist
        return tuple(str(p.relative_to(REPORTS_DIR)) for p in REPORTS_DIR.rglob("*.md"))


def check_GB_9(thresholds: dict[str, Any]) -> CheckResult:
    """Banned terms from prompt Section 3.2 do not appear in API strings or docs."""
    result = CheckResult(
        id="GB.9",
        title="Language audit on API strings, MCP descriptions and published docs",
        status="FAIL",
        measured="not measured",
        threshold="no banned term used as an assertion about an entity",
    )

    # Meta documents are excluded. The review files discuss the rule and quote it, and
    # quoting is not a violation — the same false positive gate_A hit, where the only
    # hits were a reviewer's own grep expression and the phrase "a fake number" about
    # a value rather than an entity.
    META = ("review_", "gate_", "gate_changes", "failure_analysis", "PROGRESS")

    targets: list[Path] = []
    docs_dir = REPO_ROOT / "docs"
    if docs_dir.is_dir():
        # `docs/temp/` holds dated research evidence, not published surfaces. The LN
        # node security report (2026-11-06) names attack categories — "a fraudulent
        # close", "fake release binary" — which describe attacks, not an address or
        # entity. Scanning it made GB.9 fail on 2026-11-16 for vocabulary the gate
        # exists to catch only when used as an assertion about an entity.
        targets.extend(
            p
            for p in sorted(docs_dir.rglob("*.md"))
            if not p.name.startswith(META) and "temp" not in p.relative_to(docs_dir).parts[:-1]
        )
    # Under `reports/`, only what is actually published is audited: the analysis tree
    # plus the names `scripts/export_public.py` copies into the public repository.
    # Internal meeting and planning records necessarily quote the rule — "never call an
    # address or entity scam/fraud/fake" — and that quotation is not a violation. The
    # allowlist is read from the exporter so a report that becomes public joins the
    # audit without a second list to keep in sync.
    analysis_dir = REPORTS_DIR / "analysis"
    if analysis_dir.is_dir():
        targets.extend(
            p
            for p in sorted(analysis_dir.rglob("*.md"))
            if not p.name.startswith(META)
        )
    for name in _published_report_names():
        candidate = REPORTS_DIR / name
        if (
            candidate.suffix == ".md"
            and candidate.is_file()
            and not candidate.name.startswith(META)
        ):
            targets.append(candidate)
    api_dir = REPO_ROOT / "src" / "repindex" / "api"
    if api_dir.is_dir():
        targets.extend(sorted(api_dir.rglob("*.py")))

    # F19: the check's title names MCP descriptions and published docs, but it did not
    # scan the MCP server, the README, the dispute policy or the served card, and it
    # skipped fenced text. A term inside a fence is still text a reader can see.
    for extra in (
        REPO_ROOT / "src" / "repindex" / "mcp" / "server.py",
        REPO_ROOT / "README.md",
        DISPUTES,
        AGENT_CARD,
    ):
        if extra.is_file():
            targets.append(extra)

    if not targets:
        result.notes.append("nothing to audit")
        return result

    hits: list[str] = []
    for path in targets:
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            for term in BANNED_TERMS:
                if re.search(rf"\b{re.escape(term)}\w*\b", line, re.IGNORECASE):
                    hits.append(f"{rel(path)}:{number}: {term!r}")

    result.measured = f"{len(targets)} files scanned, {len(hits)} hit(s)"
    if hits:
        result.notes.extend(hits[:10])
        return result
    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GB.10 — disputes
# --------------------------------------------------------------------------- #


def check_GB_10(thresholds: dict[str, Any]) -> CheckResult:
    """Dispute process documented, and corrections apply with an audit trail."""
    result = CheckResult(
        id="GB.10",
        title="DISPUTES.md exists and the corrections script has an audited test",
        status="FAIL",
        measured="not measured",
        threshold="DISPUTES.md present; corrections test passes and writes an audit record",
        evidence=[rel(DISPUTES), rel(CORRECTIONS_TEST)],
    )

    if not DISPUTES.is_file():
        result.notes.append(f"{rel(DISPUTES)} missing — Phase B task B5 has not run")
        return result
    if not CORRECTIONS_TEST.is_file():
        result.notes.append(f"{rel(CORRECTIONS_TEST)} missing")
        return result

    code, out = run(["uv", "run", "pytest", "-q", str(CORRECTIONS_TEST)])
    result.measured = f"pytest exit {code}"
    if code != 0:
        result.notes.append("\n".join(out.strip().splitlines()[-10:]))
        return result

    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GB.11 — independent review
# --------------------------------------------------------------------------- #


def check_GB_11(thresholds: dict[str, Any]) -> CheckResult:
    """An independent reviewer ran and left no unresolved findings."""
    review_path, all_reviews = newest_review("B")
    result = CheckResult(
        id="GB.11",
        title="Independent reviewer ran with no unresolved findings",
        status="FAIL",
        measured="not measured",
        threshold="the newest reports/review_B*.md declares 0 unresolved findings",
        evidence=[rel(p) for p in all_reviews] or [rel(review_path)],
    )

    if not review_path.is_file():
        result.notes.append(
            f"{rel(review_path)} missing. The reviewer must try to elicit a misleading "
            "score through crafted inputs, not merely re-read the code."
        )
        return result

    matches = re.findall(
        r"^\s*(?:[-*]\s*)?unresolved\s+findings?\s*[:=]\s*(\d+)\s*$",
        review_path.read_text(encoding="utf-8"),
        re.IGNORECASE | re.MULTILINE,
    )
    if not matches:
        result.notes.append("the review must state 'unresolved findings: <n>'")
        return result

    total = max(int(m) for m in matches)
    result.measured = f"{review_path.name} declares {total} unresolved finding(s)"
    if total:
        result.notes.append("unresolved findings count as FAIL (prompt rule 3.4.7)")
        return result

    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GB.12 — disclaimer (owner decision, 2026-10-30)
# --------------------------------------------------------------------------- #


def check_GB_12(thresholds: dict[str, Any]) -> CheckResult:
    """Every API and MCP response carries the limitation of its own score.

    The owner chose to expose the score on 2026-10-30 while blinded adjudication put
    agreement at about 60%. That is the owner's call, but a number whose accuracy is
    not established must travel with that fact. Prompt Section 8 already requires a
    disclaimer; this check makes the specific content testable.
    """
    result = CheckResult(
        id="GB.12",
        title="Every response states that the score is not independently validated",
        status="FAIL",
        measured="not measured",
        threshold=f"every sampled response contains {DISCLAIMER_MARKER!r}",
        evidence=[rel(DISCLAIMER_LOG)],
    )

    code, out = _verify_api()
    if code != 0:
        result.notes.append(f"the API evidence producer failed (exit {code}):\n{_tail(out)}")
        return result

    data = read_json(DISCLAIMER_LOG)
    if data is None:
        result.notes.append(f"{rel(DISCLAIMER_LOG)} was not written")
        return result
    if not _freshness(data, result):
        return result

    checked = int(data.get("responses_checked", 0))
    missing = data.get("missing", [])
    labels = " ".join(data.get("labels", []))
    result.measured = f"{checked} response classes checked, {len(missing)} missing the marker"

    # The old sweep sampled four 200s. A response class it never requested could not
    # fail. These are the classes the review found without the marker.
    required = ("404", "405", "422", "429", "500", "304")
    absent = [marker for marker in required if marker not in labels]
    if absent:
        result.notes.append(f"the sweep did not cover response class(es): {absent}")
        return result
    if checked == 0:
        result.notes.append("the sweep checked nothing")
        return result
    if missing:
        result.notes.append(f"responses without the disclaimer: {missing[:5]}")
        return result

    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GB.13 — identity binding (owner decision, 2026-10-30)
# --------------------------------------------------------------------------- #


REQUIRED_BINDING_CASES = (
    "valid_binding_accepted",
    "replay_rejected",
    "caller_constructed_challenge_rejected",
    "wrong_domain_rejected",
    "one_key_rejected",
    "unsigned_revocation_rejected",
    "rotation_accepted",
    "signed_revocation_accepted",
    "lifecycle_recorded",
    "expired_rejected",
)


def check_GB_13(thresholds: dict[str, Any]) -> CheckResult:
    """Cross-rail identity binding works end to end, and rejects the attack cases.

    ADR 0003 records that attribution can never come from routing: an onion-routed node
    sees amounts, not parties. So the promised "trust level when you pay through us"
    depends entirely on this binding existing and being sound.

    The review (F5) found the old check ran only unit tests over stateless primitives —
    it could not tell whether anything issued a challenge or remembered one. The check
    now also runs `scripts/binding_e2e.py`, which drives the real app over loopback and
    requires every case the prompt names to be true.
    """
    result = CheckResult(
        id="GB.13",
        title=(
            "Identity binding accepts valid proofs and rejects replay, expiry, wrong "
            "domain, one key and unsigned revocation; rotation works"
        ),
        status="FAIL",
        measured="not measured",
        threshold=(
            "unit tests exit 0; binding_e2e.py exit 0 and every required case true"
        ),
        evidence=[rel(BINDING_TEST), rel(BINDING_E2E_SCRIPT), rel(BINDING_E2E)],
    )

    if not BINDING_TEST.is_file() or not BINDING_E2E_SCRIPT.is_file():
        result.notes.append(
            f"{rel(BINDING_TEST)} or {rel(BINDING_E2E_SCRIPT)} missing — task C6 is incomplete"
        )
        return result

    code, out = run(["uv", "run", "pytest", "-q", str(BINDING_TEST)])
    result.measured = f"pytest exit {code}"
    if code != 0:
        result.notes.append("\n".join(out.strip().splitlines()[-12:]))
        return result

    code, out = _producer("binding_e2e", ["uv", "run", "python", "scripts/binding_e2e.py"], 300)
    if code != 0:
        result.notes.append(f"binding_e2e.py failed (exit {code}):\n{_tail(out)}")
        return result

    data = read_json(BINDING_E2E)
    if data is None:
        result.notes.append(f"{rel(BINDING_E2E)} was not written by the producer")
        return result

    cases = data.get("cases", {})
    missing = [name for name in REQUIRED_BINDING_CASES if name not in cases]
    failed = [name for name in REQUIRED_BINDING_CASES if cases.get(name) is not True]
    passed = sum(1 for name in REQUIRED_BINDING_CASES if cases.get(name))
    result.measured = (
        f"{len(cases)} e2e case(s), {passed}/{len(REQUIRED_BINDING_CASES)} required true"
    )
    if missing or failed:
        result.notes.append(f"missing: {missing}; failed: {failed}")
        return result

    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GB.14 — provenance (review F12)
# --------------------------------------------------------------------------- #


def check_GB_14(thresholds: dict[str, Any]) -> CheckResult:
    """The code measured is committed, and the evidence names this database.

    The review found the committed report claiming a commit that did not contain the
    measured files, a "data snapshot" hash of report artifacts rather than data, and
    no staleness check on any artifact. This check makes each of those a failure.
    """
    result = CheckResult(
        id="GB.14",
        title="Gate provenance: source tree committed and evidence produced from this database",
        status="FAIL",
        measured="not measured",
        threshold=(
            "no uncommitted source changes; every artifact's commit and db_sha256 "
            "match HEAD and data/repindex.db"
        ),
        evidence=[rel(DATA_DB)]
        + [
            rel(p)
            for p in (
                FUZZ_JSON,
                WRITE_PROBE,
                DETERMINISM_JSON,
                MCP_TEST_JSON,
                LOADTEST_JSON,
                DISCLAIMER_LOG,
                BINDING_E2E,
                PUBLIC_ANALYSIS,
                PUBLIC_ANALYSIS_PROV,
            )
        ],
    )

    dirty = git_dirty_paths(SOURCE_PATHS)
    if dirty:
        result.measured = f"{len(dirty)} uncommitted source path(s)"
        result.notes.append(
            "the gate measured a working tree that is not what a commit records: "
            + "; ".join(dirty[:5])
        )
        return result

    current_db = _db_hash()
    if current_db is None:
        # N2: distinguish a present database whose WAL blocks its identity from a
        # missing one, with the repair for each.
        if DATA_DB.is_file():
            result.notes.append(
                f"{rel(DATA_DB)} has uncheckpointed WAL frames; stop writers and "
                "checkpoint the database before attesting artefacts (review M2)"
            )
        else:
            result.notes.append(f"{rel(DATA_DB)} is missing")
        return result

    commit = git_commit()
    stale: list[str] = []
    for path in (
        FUZZ_JSON,
        WRITE_PROBE,
        DETERMINISM_JSON,
        MCP_TEST_JSON,
        LOADTEST_JSON,
        DISCLAIMER_LOG,
        BINDING_E2E,
        # `PUBLIC_ANALYSIS_PROV` is deliberately absent here: GB.16 runs later and
        # regenerates it, and GB.16 checks its freshness itself. Adding it to this list
        # would fail on the previous run's provenance every time.
    ):
        data = read_json(path)
        if data is None:
            stale.append(f"{rel(path)} missing")
            continue
        if data.get("commit") != commit:
            stale.append(f"{rel(path)} from commit {str(data.get('commit'))[:12]}…")
        if data.get("db_sha256") != current_db:
            stale.append(f"{rel(path)} from another database")
    if stale:
        result.measured = f"{len(stale)} stale artifact(s)"
        result.notes.extend(stale[:8])
        return result

    result.measured = f"clean tree at {commit[:12]}…; {rel(DATA_DB)}={current_db[:16]}…"
    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GB.15 — packaging (prompt task B6; review F8)
# --------------------------------------------------------------------------- #


def check_GB_15(thresholds: dict[str, Any]) -> CheckResult:
    """A local deployment exists and its compose file parses."""
    result = CheckResult(
        id="GB.15",
        title="Packaging: Dockerfile, compose file and local deployment guide exist",
        status="FAIL",
        measured="not measured",
        threshold="all three files exist; docker-compose.yml parses with an api service",
        evidence=[rel(p) for p in (DOCKERFILE, COMPOSE_FILE, DEPLOY_DOC)],
    )

    missing = [rel(p) for p in (DOCKERFILE, COMPOSE_FILE, DEPLOY_DOC) if not p.is_file()]
    if missing:
        result.notes.append(f"missing: {', '.join(missing)} (Phase B task B6)")
        return result

    try:
        compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError) as exc:
        result.notes.append(f"docker-compose.yml does not parse: {exc}")
        return result

    services = compose.get("services", {}) if isinstance(compose, dict) else {}
    if "api" not in services:
        result.notes.append("docker-compose.yml has no `api` service")
        return result

    docker = shutil.which("docker")
    if docker:
        code, out = run([docker, "compose", "config", "-q"], timeout=120)
        if code != 0:
            result.notes.append(f"docker compose config failed: {_tail(out, 5)}")
            return result
        result.measured = "three files present; docker compose config exit 0"
    else:
        result.measured = "three files present; compose parsed (docker CLI unavailable)"
        result.notes.append(
            "the docker CLI is not installed, so the compose file was parsed but not "
            "resolved. Install Docker or record the deployment check separately."
        )

    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GB.16 — public analysis (prompt task B4; review F8)
# --------------------------------------------------------------------------- #


def check_GB_16(thresholds: dict[str, Any]) -> CheckResult:
    """A public analysis exists, cites its charts, and does not publish addresses."""
    result = CheckResult(
        id="GB.16",
        title="Public analysis: report and static charts exist, with no addresses published",
        status="FAIL",
        measured="not measured",
        threshold=(
            "producer exit 0 and fresh; reports/phase_B_public_analysis.md present, at "
            "least two charts under reports/analysis/, no 0x address in the report"
        ),
        evidence=[rel(PUBLIC_ANALYSIS), rel(PUBLIC_ANALYSIS_PROV), rel(ANALYSIS_DIR)],
    )

    if not PUBLIC_ANALYSIS.is_file():
        result.notes.append(f"{rel(PUBLIC_ANALYSIS)} missing (Phase B task B4)")
        return result

    code, out = _producer(
        "public_analysis", ["uv", "run", "python", "scripts/public_analysis.py"], 600
    )
    if code != 0:
        result.notes.append(f"the analysis producer failed (exit {code}):\n{_tail(out)}")
        return result
    if not _freshness(read_json(PUBLIC_ANALYSIS_PROV), result):
        return result

    charts = (
        sorted(ANALYSIS_DIR.glob("*.png")) + sorted(ANALYSIS_DIR.glob("*.svg"))
        if ANALYSIS_DIR.is_dir()
        else []
    )
    if len(charts) < 2:
        result.notes.append(
            f"{len(charts)} chart(s) under {rel(ANALYSIS_DIR)}; at least two are required"
        )
        return result

    text = PUBLIC_ANALYSIS.read_text(encoding="utf-8")
    referenced = [chart for chart in charts if chart.name in text]
    if not referenced:
        result.notes.append("the report does not reference any of its charts")
        return result
    if re.search(r"\b0x[0-9a-fA-F]{40}\b", text):
        result.notes.append(
            "the public report contains a full address; published analysis must not "
            "single out an identifiable seller"
        )
        return result

    result.measured = f"{len(charts)} charts, {len(referenced)} referenced, no addresses"
    result.status = "PASS"
    return result


# --------------------------------------------------------------------------- #
# GB.17 — usage metrics (prompt task B7; review F8)
# --------------------------------------------------------------------------- #


def check_GB_17(thresholds: dict[str, Any]) -> CheckResult:
    """`make metrics` measures unique external callers, because the market gate uses it."""
    result = CheckResult(
        id="GB.17",
        title="Usage metrics: unique external callers are measurable with `make metrics`",
        status="FAIL",
        measured="not measured",
        threshold=(
            "Makefile metrics target is implemented; the script runs and reports "
            "unique_external_callers over the configured window"
        ),
        evidence=[rel(METRICS_SCRIPT), rel(METRICS_JSON)],
    )

    if not METRICS_SCRIPT.is_file():
        result.notes.append(f"{rel(METRICS_SCRIPT)} missing (Phase B task B7)")
        return result
    if not MAKEFILE.is_file():
        result.notes.append("Makefile missing")
        return result
    makefile = MAKEFILE.read_text(encoding="utf-8")
    if re.search(r"not implemented yet \(Phase B, task B7\)", makefile):
        result.notes.append("`make metrics` is still the unimplemented stub")
        return result

    code, out = _producer(
        "metrics", ["uv", "run", "python", "scripts/metrics.py"], timeout=600
    )
    if code != 0:
        result.notes.append(f"the metrics script failed (exit {code}):\n{_tail(out)}")
        return result

    data = read_json(METRICS_JSON)
    if data is None:
        result.notes.append(f"{rel(METRICS_JSON)} was not written")
        return result
    if "unique_external_callers" not in data:
        result.notes.append("metrics output does not report unique_external_callers")
        return result
    if int(data.get("window_days", 0)) < int(
        thresholds.get("phaseB", {}).get("market_window_days", 30)
    ):
        result.notes.append("metrics window is shorter than the market-gate window")
        return result

    result.measured = (
        f"{data['unique_external_callers']} unique external caller(s) over "
        f"{data['window_days']} days"
    )
    result.status = "PASS"
    return result


CHECKS = (
    check_GB_1,
    check_GB_2,
    check_GB_3,
    check_GB_4,
    check_GB_5,
    check_GB_6,
    check_GB_7,
    check_GB_8,
    check_GB_9,
    check_GB_10,
    check_GB_11,
    check_GB_12,
    check_GB_13,
    check_GB_14,
    check_GB_15,
    check_GB_16,
    check_GB_17,
)


if __name__ == "__main__":
    sys.exit(
        main_wrapper(
            "B",
            list(CHECKS),
            [
                OPENAPI_JSON,
                COVERAGE_JSON,
                LOADTEST_JSON,
                FUZZ_JSON,
                WRITE_PROBE,
                DETERMINISM_JSON,
                MCP_TEST_JSON,
                DISCLAIMER_LOG,
                BINDING_E2E,
                PUBLIC_ANALYSIS,
                PUBLIC_ANALYSIS_PROV,
                DATA_DB,
                DATA_WAL,
            ],
            source_paths=SOURCE_PATHS,
            extra={"data_db_sha256": _db_hash()},
        )
    )
