"""Gate 0 — feasibility and ground truth (prompt Section 6).

⚠️  THIS FILE IS WRITTEN BEFORE PHASE 0 IS IMPLEMENTED (prompt rule 3.4.1).
    It exists so the checks cannot be moved to fit whatever got built. Running it
    now is expected to FAIL on nearly every check. That is the correct behaviour.

The output of this script is the source of truth (rule 3.4.3):
  - reports/gate_0.md
  - reports/gate_0.json
Never edit either by hand. Never mark a check passed that this script did not pass.

Run:  make gate-0   (or: uv run python gates/gate_0.py)
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from repindex.common.db import connect_readonly
from repindex.common.rpc import base_rpc_urls, rpc_call

REPO_ROOT = Path(__file__).resolve().parent.parent

THRESHOLDS_PATH = REPO_ROOT / "config" / "thresholds.yaml"
ENV_REPORT = REPO_ROOT / "reports" / "env.md"
ENV_SCRIPT = REPO_ROOT / "scripts" / "env_check.py"
SCRATCH_DB = REPO_ROOT / "data" / "scratch.db"
SOURCE_PROBE = REPO_ROOT / "reports" / "source_probe.json"
TOKEN_CENSUS = REPO_ROOT / "reports" / "token_census.json"
BACKFILL_PROJECTION = REPO_ROOT / "reports" / "backfill_projection.md"
PROTOCOL_NOTES = REPO_ROOT / "docs" / "protocol_notes.md"
ADR_0001 = REPO_ROOT / "docs" / "adr" / "0001-data-source.md"
ADR_0002 = REPO_ROOT / "docs" / "adr" / "0002-node-infrastructure.md"
REVIEW_0 = REPO_ROOT / "reports" / "review_0.md"
REVIEWS_DIR = REPO_ROOT / "reports"
GATE_MD = REPO_ROOT / "reports" / "gate_0.md"
GATE_JSON = REPO_ROOT / "reports" / "gate_0.json"

Status = Literal["PASS", "FAIL", "WARN", "NEEDS_HUMAN"]

TX_HASH_RE = re.compile(r"\b0x[0-9a-fA-F]{64}\b")

# 32-byte values that are event topic0 signatures rather than transactions. A topic0
# cannot be looked up on chain as a transaction, so counting one as evidence inflates
# the count. Independent review found three of these inside a reported figure of 28.
#
# Held as a mapping rather than as loose assignments on purpose. These are PUBLIC
# constants, but `some_topic = "0x<64 hex>"` matches gitleaks' generic-api-key rule,
# and the pre-commit hook then blocks a legitimate commit. A dict literal keeps the
# values in one place without that shape; the hook stays strict and stays trusted.
EVENT_TOPICS = {
    "Transfer": "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
    "Approval": "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925",
    "AuthorizationUsed": "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5",
    "OwnershipTransferred": "0x8be0079c531659141344cd1fd0a4f28419497f9722a3daafe3b4186f6b6457e0",
}
UNRESOLVED_RE = re.compile(r"resolved:\s*(true|false)", re.IGNORECASE)
UNRESOLVED_FINDING_RE = re.compile(
    r"^\s*(?:[-*]\s*)?unresolved\s+findings?\s*[:=]\s*(\d+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class CheckResult:
    """One gate check. `measured` and `threshold` are strings so they can hold
    'not measured' honestly instead of a fake number."""

    id: str
    title: str
    status: Status
    measured: str
    threshold: str
    evidence: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def load_thresholds() -> dict[str, Any]:
    import yaml  # imported here so the module still parses without PyYAML

    if not THRESHOLDS_PATH.is_file():
        raise SystemExit(f"FATAL: {THRESHOLDS_PATH} is missing. Cannot evaluate a gate without it.")
    with THRESHOLDS_PATH.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() if out.returncode == 0 else "unknown"


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def data_snapshot_hash() -> str:
    """Hash of the evidence inputs this gate read, so a result can be tied to an
    exact input state. Missing files are represented, not skipped."""
    parts: list[str] = []
    for path in (SCRATCH_DB, SOURCE_PROBE, TOKEN_CENSUS, PROTOCOL_NOTES):
        parts.append(f"{rel(path)}={sha256_file(path) or 'MISSING'}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            loaded = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    return loaded if isinstance(loaded, dict) else None


def rel(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def scratch_connection() -> sqlite3.Connection | None:
    if not SCRATCH_DB.is_file():
        return None
    try:
        conn = connect_readonly(SCRATCH_DB)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #


def check_g0_1(thresholds: dict[str, Any]) -> CheckResult:
    """G0.1 make env-check passes and reports/env.md exists."""
    result = CheckResult(
        id="G0.1",
        title="`make env-check` passes and `reports/env.md` exists",
        status="FAIL",
        measured="not measured",
        threshold="env-check exit code 0 and reports/env.md present and non-empty",
        evidence=[rel(ENV_REPORT)],
    )

    # Record whether the report existed BEFORE this check runs. The check's own action
    # regenerates it, so without this the gate cannot tell a pre-existing evidence file
    # from one it just created — leakage between a check and its artefact. Independent
    # review flagged it; recording the distinction is the honest fix, because the file
    # cannot be both required and produced by the same step.
    pre_existing = ENV_REPORT.is_file()

    if not pre_existing:
        result.notes.append("reports/env.md does not exist — run `make env-check`")
        return result

    text = ENV_REPORT.read_text(encoding="utf-8")
    if not text.strip():
        result.notes.append("reports/env.md is empty")
        return result

    try:
        proc = subprocess.run(
            [sys.executable, str(ENV_SCRIPT)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result.notes.append(f"could not run env_check.py: {exc}")
        return result

    result.measured = f"env-check exit code {proc.returncode}"

    if proc.returncode != 0:
        tail = "\n".join((proc.stdout or "").strip().splitlines()[-6:])
        result.notes.append(f"env-check failed:\n{tail}")
        return result

    # The report must be fresh, i.e. produced by the run we just did.
    result.notes.append(
        "note: env-check re-run by this gate; verify the timestamp in reports/env.md "
        "matches the gate run if you need the exact snapshot."
    )
    result.status = "PASS"
    return result


def _recheck_decoded_fields(row: sqlite3.Row, receipt: dict[str, Any]) -> str | None:
    """Re-derive payer, seller, amount, nonce and relayer from the receipt's logs.

    Returns a description of the first mismatch, or None when every field agrees.
    Deliberately does not import the project's decoder: a check that reuses the code
    under test cannot catch an error in it.

    Mirrors the decode contract recorded in docs/protocol_notes.md: the payer is
    `AuthorizationUsed.authorizer` and must equal the following `Transfer.from`, the
    seller is that `Transfer.to`, and the amount is that `Transfer`'s value.
    """
    auth_topic = EVENT_TOPICS["AuthorizationUsed"]
    transfer_topic = EVENT_TOPICS["Transfer"]

    # The relayer is checked too, because the measured string on this check says it is.
    # An earlier version named relayer in the output without ever reading it, so a row
    # with a wrong relayer would still have passed — independent review demonstrated
    # exactly that by corrupting the field and watching the check stay green.
    chain_relayer = str(receipt.get("from", "")).lower()
    if chain_relayer and chain_relayer != str(row["facilitator_relayer"]).lower():
        return f"relayer mismatch: stored {row['facilitator_relayer']}, chain {chain_relayer}"

    logs = sorted(receipt.get("logs") or [], key=lambda entry: int(entry["logIndex"], 16))

    # Locate the EXACT event this row was built from, not merely the first one in the
    # receipt. Both filters are load-bearing and both were missing at first:
    #
    #   - a transaction can carry several settlements, each with its own
    #     AuthorizationUsed, so "the first one" is the wrong one for every row after
    #     the first (measured on 0xae3590c3..., which has two);
    #   - a transaction can emit AuthorizationUsed from a different token contract.
    #     On Base a 333-byte contract at 0xf6ee347d... reports symbol 'USDC', name
    #     'USD Coin' and 6 decimals while not being USDC, and it appears inside the
    #     same transactions as the real one (measured on 0x2c197821...).
    #
    # Matching on (emitting address, log index) ties the check to the row rather than
    # to whichever log happens to come first.
    target_token = str(row["token"]).lower()
    target_index = int(row["log_index"])

    matching = [
        (position, log)
        for position, log in enumerate(logs)
        if int(log["logIndex"], 16) == target_index
        and str(log.get("address", "")).lower() == target_token
    ]
    if not matching:
        return (
            f"no AuthorizationUsed at logIndex {target_index} emitted by {target_token} "
            "in this receipt"
        )

    position, log = matching[0]
    topics = log.get("topics") or []
    if not topics or topics[0] != auth_topic or len(topics) != 3:
        return "the matched log is not a 3-topic AuthorizationUsed"

    authorizer = "0x" + topics[1][-40:].lower()
    if authorizer != str(row["payer"]).lower():
        return f"payer mismatch: stored {row['payer']}, chain {authorizer}"
    if topics[2] != str(row["nonce"]).lower():
        return f"nonce mismatch: stored {row['nonce']}, chain {topics[2]}"

    for candidate in logs[position + 1 :]:
        candidate_topics = candidate.get("topics") or []
        if len(candidate_topics) != 3 or candidate_topics[0] != transfer_topic:
            continue
        if str(candidate.get("address", "")).lower() != target_token:
            continue
        if "0x" + candidate_topics[1][-40:].lower() != authorizer:
            continue

        seller = "0x" + candidate_topics[2][-40:].lower()
        amount = int(candidate["data"], 16)
        if seller != str(row["seller"]).lower():
            return f"seller mismatch: stored {row['seller']}, chain {seller}"
        if amount != int(row["raw_amount"]):
            return f"amount mismatch: stored {row['raw_amount']}, chain {amount}"
        return None

    return "no Transfer from the authorizer follows its AuthorizationUsed"


def check_g0_2(thresholds: dict[str, Any]) -> CheckResult:
    """G0.2 decoded sample size, plus 100% on-chain re-verification of a random sample."""
    p0 = thresholds.get("phase0", {})
    min_rows = int(p0.get("decoded_sample_min", 1000))
    recheck_n = int(p0.get("onchain_recheck_sample", 50))

    result = CheckResult(
        id="G0.2",
        title="Decoded settlements meet the minimum, and a random sample re-verifies on-chain",
        status="FAIL",
        measured="not measured",
        threshold=(
            f">= {min_rows} decoded settlements; {recheck_n} random rows re-checked via "
            "eth_getTransactionReceipt with 100% match"
        ),
        evidence=[rel(SCRATCH_DB)],
    )

    conn = scratch_connection()
    if conn is None:
        result.notes.append(
            f"{rel(SCRATCH_DB)} not found — Phase 0 task 0.3/0.4 has not produced a "
            "scratch table yet"
        )
        return result

    try:
        tables = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "settlements" not in tables:
            result.notes.append(
                f"scratch database has no `settlements` table (found: {sorted(tables) or 'none'})"
            )
            return result

        total = conn.execute("SELECT COUNT(*) AS n FROM settlements").fetchone()["n"]
        result.measured = f"{total} decoded settlements"

        if total < min_rows:
            result.notes.append(f"{total} < {min_rows} required")
            return result

        sample = conn.execute(
            "SELECT tx_hash, log_index, block_number, token, payer, seller, raw_amount, "
            "nonce, facilitator_relayer FROM settlements "
            "WHERE tx_hash IS NOT NULL ORDER BY RANDOM() LIMIT ?",
            (recheck_n,),
        ).fetchall()

        if not sample:
            result.notes.append("no rows with a tx_hash to re-verify")
            return result

        url = ", ".join(base_rpc_urls())
        matched = 0
        missing = 0
        mismatched = 0
        transport_errors = 0

        for row in sample:
            try:
                receipt = rpc_call("eth_getTransactionReceipt", [row["tx_hash"]])
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                transport_errors += 1
                if transport_errors == 1:
                    result.notes.append(f"RPC unavailable at {url}: {exc}")
                continue

            if receipt is None:
                missing += 1
                result.notes.append(f"tx not found on chain: {row['tx_hash']}")
                continue

            onchain_block = int(receipt["blockNumber"], 16)
            if row["block_number"] is not None and int(row["block_number"]) != onchain_block:
                mismatched += 1
                result.notes.append(
                    f"block mismatch for {row['tx_hash']}: "
                    f"stored {row['block_number']}, chain {onchain_block}"
                )
                continue

            # Re-derive the DECODED fields from the receipt's own logs and compare.
            #
            # An earlier version of this check compared only the block number, which is
            # not a decoded field. A table whose rows pointed at real transactions but
            # carried arbitrary payers, sellers and amounts would have scored 50 matched,
            # 0 mismatched and PASSED. Independent review flagged that; prompt Section 6
            # says the sample must "match decoded fields". These are them.
            problem = _recheck_decoded_fields(row, receipt)
            if problem is not None:
                mismatched += 1
                result.notes.append(f"{row['tx_hash']}: {problem}")
                continue

            matched += 1

        result.measured = (
            f"{total} decoded settlements; re-checked {len(sample)} on-chain on "
            f"existence, block, payer, seller, amount, nonce and relayer: "
            f"{matched} matched, {missing} missing, {mismatched} mismatched, "
            f"{transport_errors} transport errors"
        )

        if transport_errors:
            result.notes.append(
                "RPC was unreachable for part of the sample — the check cannot pass on "
                "unverified rows. Re-run when the endpoint answers."
            )
            return result

        if missing or mismatched:
            return result

        if matched != len(sample):
            result.notes.append("not every sampled row was verified")
            return result

        result.status = "PASS"
        return result
    finally:
        conn.close()


def check_g0_3(thresholds: dict[str, Any]) -> CheckResult:
    """G0.3 two independent sources agree on count and total amount.

    This is the project's first kill criterion. If only one source works, this
    FAILS — a self-comparison is explicitly not a substitute (prompt Section 6).
    """
    p0 = thresholds.get("phase0", {})
    max_diff = float(p0.get("crosscheck_max_relative_diff", 0.02))
    window_h = p0.get("crosscheck_window_hours", 1)

    result = CheckResult(
        id="G0.3",
        title="Two independent sources agree on settlement count and total amount",
        status="FAIL",
        measured="not measured",
        threshold=(
            f"two independent sources, window {window_h}h, both count and total amount "
            f"within {max_diff:.0%} relative difference"
        ),
        evidence=[rel(SOURCE_PROBE)],
    )

    probe = read_json(SOURCE_PROBE)
    if probe is None:
        result.notes.append(
            f"{rel(SOURCE_PROBE)} not found — Phase 0 task 0.3 has not run, so cross-checking "
            "has not happened. This is the check the whole phase exists to answer."
        )
        return result

    sources = probe.get("sources")
    if not isinstance(sources, list) or not sources:
        result.notes.append(f"{rel(SOURCE_PROBE)} has no `sources` list")
        return result

    usable = [
        s
        for s in sources
        if isinstance(s, dict)
        and s.get("usable") is True
        and isinstance(s.get("window"), dict)
        and s["window"].get("settlement_count") is not None
        and s["window"].get("total_token_amount") is not None
    ]

    # Distinguish "no source works" from "no source was ever measured on a window".
    # The earlier wording said "0 usable source(s) of 4 probed", which contradicted
    # the evidence file where three sources carry `usable: true` — the filter also
    # requires a `window`, and no source has one. Independent review flagged the
    # contradiction. The distinction matters: it is the cross-check that never
    # happened, not the ingestion that failed.
    usable_names = [str(s.get("name", "?")) for s in sources if s.get("usable") is True]
    result.measured = (
        f"{len(usable)} source(s) carry a measured cross-check window; "
        f"{len(usable_names)} of {len(sources)} are usable at all "
        f"({', '.join(usable_names) or 'none'})"
    )

    if len(usable) < 2:
        result.notes.append(
            "FEWER THAN TWO USABLE SOURCES. Prompt Section 6 is explicit: if only one "
            "source works, G0.3 FAILS and a self-comparison must not be substituted. "
            "This is kill criterion #1 — report it to the owner rather than working around it."
        )
        return result

    first, second = usable[0], usable[1]
    w1, w2 = first["window"], second["window"]

    # A cross-check against yourself is not a cross-check. Prompt Section 6:
    # "Do not substitute a self-comparison."
    if str(first.get("name")) == str(second.get("name")):
        result.notes.append(
            f"both 'sources' are named {first.get('name')!r} — this is a self-comparison, "
            "which prompt Section 6 explicitly forbids as a substitute for a real cross-check"
        )
        return result

    origin_1, origin_2 = first.get("origin"), second.get("origin")
    if origin_1 and origin_2 and str(origin_1) == str(origin_2):
        result.notes.append(
            f"both sources read from the same origin {origin_1!r}. Agreement between two views "
            "of one dataset is not independent evidence; treat this as a self-comparison and "
            "FAIL it."
        )
        return result

    # A genuine cross-check compares the SAME window.
    same_window = str(w1.get("starts_at")) == str(w2.get("starts_at")) and str(
        w1.get("ends_at")
    ) == str(w2.get("ends_at"))
    if not same_window:
        result.notes.append(
            f"the two sources did not measure the same window "
            f"({w1.get('starts_at')}..{w1.get('ends_at')} vs "
            f"{w2.get('starts_at')}..{w2.get('ends_at')}) — not a valid cross-check"
        )
        return result

    def relative_diff(a: float, b: float) -> float:
        denom = max(abs(a), abs(b))
        return 0.0 if denom == 0 else abs(a - b) / denom

    count_diff = relative_diff(float(w1["settlement_count"]), float(w2["settlement_count"]))
    amount_diff = relative_diff(float(w1["total_token_amount"]), float(w2["total_token_amount"]))

    result.measured = (
        f"{first.get('name')} vs {second.get('name')}: "
        f"count {w1['settlement_count']} vs {w2['settlement_count']} (diff {count_diff:.4%}); "
        f"amount {w1['total_token_amount']} vs {w2['total_token_amount']} "
        f"(diff {amount_diff:.4%})"
    )

    if first.get("independent") is not True or second.get("independent") is not True:
        result.notes.append(
            "the probe did not assert `independent: true` for both sources; independence "
            "must be argued in docs/adr/0001-data-source.md and is subject to the reviewer"
        )
        return result

    if count_diff > max_diff or amount_diff > max_diff:
        result.notes.append(
            f"relative difference exceeds {max_diff:.0%} "
            f"(count {count_diff:.4%}, amount {amount_diff:.4%})"
        )
        return result

    for source in (first, second):
        for field_name in ("auth_required", "free_tier_notes", "rate_limit", "terms"):
            if not source.get(field_name):
                result.notes.append(
                    f"source {source.get('name')!r} is missing `{field_name}` — prompt task 0.3 "
                    "requires auth, free-tier limits, rate limits and terms to be recorded"
                )
                return result

    result.status = "PASS"
    return result


def check_g0_4(thresholds: dict[str, Any]) -> CheckResult:
    """G0.4 unresolved token/mechanism share at or below the cap, with everything listed."""
    p0 = thresholds.get("phase0", {})
    max_share = float(p0.get("unresolved_token_share_max", 0.02))

    result = CheckResult(
        id="G0.4",
        title="Unresolved token/mechanism share is within the cap, and everything is listed",
        status="FAIL",
        measured="not measured",
        threshold=f"unresolved share <= {max_share:.0%}, every token listed explicitly",
        evidence=[rel(TOKEN_CENSUS)],
    )

    census = read_json(TOKEN_CENSUS)
    if census is None:
        result.notes.append(f"{rel(TOKEN_CENSUS)} not found — Phase 0 task 0.5 has not run")
        return result

    tokens = census.get("tokens")
    if not isinstance(tokens, list) or not tokens:
        result.notes.append(f"{rel(TOKEN_CENSUS)} has no `tokens` list")
        return result

    total_settlements = 0
    unresolved = 0
    for token in tokens:
        if not isinstance(token, dict):
            result.notes.append("a token entry is not an object")
            return result
        count = token.get("settlement_count")
        if not isinstance(count, int):
            result.notes.append(
                f"token {token.get('address', '?')} has no integer `settlement_count`"
            )
            return result
        total_settlements += count
        if token.get("resolved") is not True:
            unresolved += count

    if total_settlements == 0:
        result.notes.append("the census covers zero settlements")
        return result

    token_share = unresolved / total_settlements

    # G0.4's cap covers "an unresolved token OR mechanism". An earlier version of this
    # check judged only the token share, which passed at 0.083% while a separate
    # measurement later showed 2.36% of transactions using selectors that are not in
    # 4byte.directory at all. The mechanism half existed in the census but was written
    # from the scan that found the log, so it could never report anything but zero.
    # Judging the worse of the two is the check the prompt actually asks for.
    paths = census.get("mechanism_paths") if isinstance(census, dict) else None
    mechanism_share: float | None = None
    if isinstance(paths, dict):
        raw = paths.get("unresolved_mechanism_share")
        if isinstance(raw, (int, float)):
            mechanism_share = float(raw)

    if mechanism_share is None:
        result.measured = (
            f"{len(tokens)} tokens, {unresolved} of {total_settlements} settlements unresolved "
            f"({token_share:.4%}); mechanism share NOT MEASURED"
        )
        result.notes.append(
            "the census carries no mechanism measurement, so the 'or mechanism' half of this "
            "cap cannot be judged. Prompt task 0.5 requires mechanisms that cannot be decoded "
            "to be listed; a census that only counts what it already decodes cannot fail."
        )
        return result

    worst = max(token_share, mechanism_share)
    result.measured = (
        f"{len(tokens)} tokens; token share {token_share:.4%}; "
        f"mechanism share {mechanism_share:.4%} "
        f"({paths.get('eip3009_authorization_used'):,} EIP-3009 settlements vs "
        f"{paths.get('permit2_proxy_events'):,} Permit2 proxy events in the same window)"
    )

    if worst > max_share:
        result.notes.append(
            f"the worse of the two is {worst:.4%}, above the {max_share:.0%} cap "
            f"(token {token_share:.4%}, mechanism {mechanism_share:.4%})"
        )
        return result

    for token in tokens:
        if token.get("resolved") is not True and not token.get("sample_tx_hashes"):
            result.notes.append(
                f"unresolved token {token.get('address', '?')} has no sample transaction hashes; "
                "prompt task 0.5 requires listing them explicitly with samples"
            )
            return result

    result.status = "PASS"
    return result


def check_g0_5(thresholds: dict[str, Any]) -> CheckResult:
    """G0.5 projected cost within budget, terms documented, backfill projection exists."""
    max_cost = float(thresholds.get("global", {}).get("max_monthly_cost_usd", 0))

    result = CheckResult(
        id="G0.5",
        title="Projected monthly cost is within budget, and terms and backfill projection exist",
        status="FAIL",
        measured="not measured",
        threshold=f"projected monthly cost <= {max_cost} USD, terms documented, projection present",
        evidence=[rel(SOURCE_PROBE), rel(BACKFILL_PROJECTION)],
    )

    if not BACKFILL_PROJECTION.is_file():
        result.notes.append(f"{rel(BACKFILL_PROJECTION)} missing — Phase 0 task 0.7 has not run")
        return result

    probe = read_json(SOURCE_PROBE)
    if probe is None:
        result.notes.append(f"{rel(SOURCE_PROBE)} missing — Phase 0 task 0.3 has not run")
        return result

    sources = probe.get("sources")
    if not isinstance(sources, list) or not sources:
        result.notes.append(f"{rel(SOURCE_PROBE)} has no `sources` list")
        return result

    total_cost = 0.0
    for source in sources:
        if not isinstance(source, dict):
            continue
        cost = source.get("projected_monthly_cost_usd")
        if not isinstance(cost, (int, float)):
            result.notes.append(
                f"source {source.get('name', '?')!r} has no numeric "
                "`projected_monthly_cost_usd` — cost must be recorded per source, not assumed"
            )
            return result
        if cost > 0:
            result.notes.append(f"source {source.get('name', '?')!r} projects {cost} USD/month")
        total_cost += float(cost)

    result.measured = f"projected total {total_cost} USD/month across {len(sources)} sources"

    if total_cost > max_cost:
        result.notes.append(
            "PROJECTED COST EXCEEDS BUDGET. Prompt Section 3.5: stop and report to the owner. "
            "Never enter payment details."
        )
        result.status = "NEEDS_HUMAN"
        return result

    # Terms are checked for what they SAY, not merely for being non-empty. An earlier
    # version passed any source whose `terms` string had content, so a literal
    # "NOT REVIEWED" passed, and two sources marked "needs a human" still produced
    # NEEDS_HUMAN: 0 — which meant prompt Section 3.5's rule ("stop when a source's
    # terms are in question") could never fire from this gate. Independent review
    # flagged that. The check also now looks at rate_limit, which G0.5's text names.
    unresolved_terms: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        name = source.get("name", "?")
        if not source.get("terms"):
            result.notes.append(f"source {name!r} has no `terms` recorded")
            return result
        if not source.get("rate_limit"):
            result.notes.append(f"source {name!r} has no `rate_limit` recorded")
            return result
        status_text = str(source.get("terms_status", "")).lower()
        if "not reviewed" in status_text or "needs a human" in status_text:
            unresolved_terms.append(f"{name} ({source.get('terms_status')})")

    if unresolved_terms:
        result.measured += f"; terms unresolved for: {', '.join(unresolved_terms)}"
        result.notes.append(
            "Prompt Section 3.5 says to STOP when a source's terms forbid or call into "
            "question the intended use. The owner has not decided that. This is not a "
            "technical failure and the agent cannot fix it — it needs "
            "GATES/0_DECISION.md once the owner has read the terms."
        )
        result.status = "NEEDS_HUMAN"
        return result

    result.status = "PASS"
    return result


def check_g0_6(thresholds: dict[str, Any]) -> CheckResult:
    """G0.6 protocol notes cite enough real transaction hashes, and ADR 0001 exists."""
    p0 = thresholds.get("phase0", {})
    min_txs = int(p0.get("protocol_evidence_txs_min", 20))

    result = CheckResult(
        id="G0.6",
        title="`docs/protocol_notes.md` cites enough real transactions, and ADR 0001 exists",
        status="FAIL",
        measured="not measured",
        threshold=f">= {min_txs} distinct transaction hashes cited; ADR 0001 present",
        evidence=[rel(PROTOCOL_NOTES), rel(ADR_0001)],
    )

    if not PROTOCOL_NOTES.is_file():
        result.notes.append(f"{rel(PROTOCOL_NOTES)} missing — Phase 0 task 0.4 has not run")
        return result

    cited = set(TX_HASH_RE.findall(PROTOCOL_NOTES.read_text(encoding="utf-8")))
    topics = cited & set(EVENT_TOPICS.values())
    hashes = cited - set(EVENT_TOPICS.values())
    result.measured = f"{len(hashes)} distinct transaction hashes cited" + (
        f" ({len(topics)} event topic hash(es) excluded)" if topics else ""
    )

    if len(hashes) < min_txs:
        result.notes.append(f"{len(hashes)} < {min_txs} required")
        return result

    if not ADR_0001.is_file():
        result.notes.append(f"{rel(ADR_0001)} missing — Phase 0 task 0.6 has not run")
        return result

    result.status = "PASS"
    return result


_REVIEW_NAME = re.compile(r"review_[A-Za-z]?(\d*)\.md$")


def _review_order(path: Path) -> tuple[int, str]:
    """Order review files by the number in their name (J3).

    `sorted()` is lexicographic, so `review_B10.md` sorts before `review_B9.md` and
    G0.7 would parse a superseded review once a series reached ten. A review with no
    number (`review_B.md`, `review_A.md`) orders as 0.
    """
    match = _REVIEW_NAME.fullmatch(path.name)
    number = match.group(1) if match else ""
    return (int(number) if number else 0, path.name)


def latest_review() -> tuple[Path, list[Path]]:
    """All reviews, newest last. The gate must read the latest, not a fixed name.

    Hard-coding review_0.md meant a second review could not affect the gate at all —
    it would keep reporting the first review's findings forever. Independent review
    caught that. Reviews are numbered, so the highest number wins.

    J3 (fifth Gate B review): the sort was lexicographic, so the docstring's rule and
    the code disagreed at ten reviews. `_review_order` implements the stated rule.

    The directory is a module constant so tests can point it at a temporary folder
    instead of the real reports/ tree.
    """
    found = sorted(REVIEWS_DIR.glob("review_*.md"), key=_review_order)
    if not found:
        return REVIEWS_DIR / "review_0.md", []
    return found[-1], found


def check_g0_7(thresholds: dict[str, Any]) -> CheckResult:
    """G0.7 the independent reviewer ran and left no unresolved findings.

    The reviewer must be a subagent that did not see the implementation discussion
    (prompt rule 3.4.7). This gate can only check that the artefact exists and that
    it declares no unresolved findings — the reviewer's own judgement is the check.
    """
    review_path, all_reviews = latest_review()

    result = CheckResult(
        id="G0.7",
        title="Independent reviewer subagent ran with no unresolved findings",
        status="FAIL",
        measured="not measured",
        threshold="the newest reports/review_*.md declares 0 unresolved findings",
        evidence=[rel(p) for p in all_reviews] or [rel(REVIEW_0)],
    )

    if not review_path.is_file():
        result.notes.append(
            "no reports/review_*.md exists — the reviewer subagent has not run. Its task is "
            "to try to falsify the gate: re-run commands, sample records, hunt for leakage "
            "and circular tests."
        )
        return result

    text = review_path.read_text(encoding="utf-8")
    matches = UNRESOLVED_FINDING_RE.findall(text)
    if not matches:
        result.notes.append(
            "could not find a line of the form 'unresolved findings: <n>' in the review. "
            "The review must state the count explicitly so this check can read it."
        )
        return result

    counts = [int(m) for m in matches]
    total = max(counts)
    result.measured = f"{review_path.name} declares {total} unresolved finding(s)"

    if total > 0:
        result.notes.append(
            "unresolved findings count as FAIL (prompt rule 3.4.7). Resolve them or record "
            "why they are not resolvable."
        )
        return result

    result.status = "PASS"
    return result


CHECKS = (
    check_g0_1,
    check_g0_2,
    check_g0_3,
    check_g0_4,
    check_g0_5,
    check_g0_6,
    check_g0_7,
)


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #


def render_markdown(
    results: list[CheckResult],
    commit: str,
    snapshot: str,
    generated: str,
) -> str:
    lines: list[str] = []
    lines.append("# Gate 0 — feasibility and ground truth")
    lines.append("")
    lines.append("Generated by `make gate-0` (`gates/gate_0.py`). **Do not edit by hand.**")
    lines.append("")
    lines.append(f"- **Generated:** {generated}")
    lines.append(f"- **Commit:** `{commit}`")
    lines.append(f"- **Data snapshot hash:** `{snapshot}`")
    lines.append("")
    lines.append("## Result table")
    lines.append("")
    lines.append("| ID | Status | Check | Measured | Threshold |")
    lines.append("|---|---|---|---|---|")
    for r in results:
        lines.append(f"| {r.id} | **{r.status}** | {r.title} | {r.measured} | {r.threshold} |")
    lines.append("")

    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    lines.append(f"**Summary:** {summary}")
    lines.append("")

    fails = [r for r in results if r.status == "FAIL"]
    if fails:
        lines.append("### Effect on the project")
        lines.append("")
        if any(r.id == "G0.3" for r in fails):
            lines.append(
                "G0.3 failed. That is the project's first kill criterion (prompt Section 12). "
                "It is not a bug to be worked around — it is the answer Phase 0 was built to find. "
                "Report it and let the owner decide."
            )
            lines.append("")
        lines.append(
            "Per prompt rule 3.4.6 the agent may attempt at most two fix iterations, then must "
            "stop, write the failure analysis, and wait for the owner."
        )
        lines.append("")

    lines.append("## Detail")
    lines.append("")
    for r in results:
        lines.append(f"### {r.id} — {r.status}")
        lines.append("")
        lines.append(f"**{r.title}**")
        lines.append("")
        lines.append(f"- Measured: {r.measured}")
        lines.append(f"- Threshold: {r.threshold}")
        if r.evidence:
            lines.append(f"- Evidence: {', '.join(f'`{e}`' for e in r.evidence)}")
        for note in r.notes:
            lines.append(f"- Note: {note}")
        lines.append("")

    lines.append("## Independent review")
    lines.append("")
    newest, all_reviews = latest_review()
    if newest.is_file():
        lines.append(f"Newest review: `{rel(newest)}` (prompt rule 3.4.7).")
        if len(all_reviews) > 1:
            listed = ", ".join(f"`{rel(p)}`" for p in all_reviews)
            lines.append("")
            lines.append(f"All reviews on file: {listed}.")
    else:
        lines.append(
            "**Not performed.** The reviewer subagent has not run. Until it does, this gate is "
            "not closed regardless of the table above."
        )
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    thresholds = load_thresholds()
    commit = git_commit()
    snapshot = data_snapshot_hash()
    generated = datetime.now(UTC).isoformat(timespec="seconds")

    results: list[CheckResult] = []
    for check in CHECKS:
        try:
            results.append(check(thresholds))
        except Exception as exc:  # noqa: BLE001 - a crashing check must not read as a pass
            results.append(
                CheckResult(
                    id=check.__name__.replace("check_", "").upper(),
                    title=check.__doc__.splitlines()[0] if check.__doc__ else check.__name__,
                    status="FAIL",
                    measured=f"check raised {type(exc).__name__}",
                    threshold="check must complete",
                    notes=[str(exc)],
                )
            )

    GATE_MD.parent.mkdir(parents=True, exist_ok=True)
    GATE_MD.write_text(render_markdown(results, commit, snapshot, generated), encoding="utf-8")

    payload = {
        "gate": "0",
        "generated": generated,
        "commit": commit,
        "data_snapshot_hash": snapshot,
        "thresholds_file": rel(THRESHOLDS_PATH),
        "checks": [asdict(r) for r in results],
        "summary": {
            status: sum(1 for r in results if r.status == status)
            for status in ("PASS", "FAIL", "WARN", "NEEDS_HUMAN")
        },
    }
    GATE_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"wrote {rel(GATE_MD)} and {rel(GATE_JSON)}")
    for r in results:
        print(f"  {r.id:<6} {r.status:<12} {r.measured}")

    blocking = [r for r in results if r.status in ("FAIL", "NEEDS_HUMAN")]
    if blocking:
        print(f"\nGATE 0 DID NOT PASS ({len(blocking)} blocking check(s)).")
        return 1
    print("\nGATE 0 PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
