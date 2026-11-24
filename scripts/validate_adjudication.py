"""Score the blinded adjudication (prompt task A6, replacement spot-check).

Reads the verdicts two independent adjudicators produced from
`reports/adjudication/packets.json`, and computes every number gate check GA.13
needs:

  1. agreement between each adjudicator and the classifier
  2. agreement between the two adjudicators — if this is low the rubric is ambiguous
  3. agreement against a deliberately broken classifier — the negative control
  4. the abstention rate
  5. the external anchor: any flagged seller that is an advertised x402 service

Writes `reports/adjudication/result.json`.

Run:  uv run python scripts/validate_adjudication.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DIR = REPO_ROOT / "reports" / "adjudication"
THRESHOLDS = REPO_ROOT / "config" / "thresholds.yaml"
OUT = DIR / "result.json"

VALID_ANSWERS = {
    "requires_independent_customers",
    "could_be_self_dealing",
    "cannot_tell",
}

DISCOVERY_URL = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"
BASE_NETWORK = "eip155:8453"


def load_verdicts(name: str) -> dict[str, str] | None:
    path = DIR / name
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): str(v).strip().lower() for k, v in data.items()}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verdict_files_identical(path1: Path, path2: Path) -> bool:
    """True when two verdict files are byte-identical.

    Identical files cannot serve as two independent raters. On 2026-11-16 the
    committed `verdicts_1.json` and `verdicts_2.json` had the same SHA-256, so the
    recorded 100% inter-rater agreement was a file compared with itself. Two separate
    deterministic runs can produce the same bytes as well; that measures determinism,
    not rubric clarity, and is equally unusable as an inter-rater check. The gate must
    fail on both, not report them as agreement.
    """
    return path1.read_bytes() == path2.read_bytes()


def advertised_addresses() -> set[str]:
    """Base payTo addresses published in Coinbase's discovery catalogue.

    External ground truth: these are services that advertise themselves publicly.
    Flagging one is a false positive measured against data the classifier never saw.
    A fetch failure returns an empty set and is recorded as UNVERIFIED, not as zero
    false positives.
    """
    try:
        response = httpx.get(DISCOVERY_URL, timeout=60)
        response.raise_for_status()
        items = response.json().get("items", [])
    except (httpx.HTTPError, ValueError):
        return set()

    found: set[str] = set()
    for item in items:
        for accept in item.get("accepts") or []:
            if accept.get("network") == BASE_NETWORK and accept.get("payTo"):
                found.add(str(accept["payTo"]).lower())
    return found


def agreement(verdicts: dict[str, str], truth: dict[str, dict[str, Any]]) -> tuple[float, int, int]:
    """(rate, agree_count, considered_count). `cannot_tell` counts as disagreement."""
    considered = 0
    agreed = 0
    for seller, label in truth.items():
        answer = verdicts.get(seller)
        if answer not in VALID_ANSWERS:
            continue
        considered += 1
        if answer == label["bucket"]:
            agreed += 1
    return (agreed / considered if considered else 0.0), agreed, considered


def main() -> int:
    thresholds = yaml.safe_load(THRESHOLDS.read_text(encoding="utf-8")) or {}
    phase_a = thresholds.get("phaseA", {})
    min_agreement = float(phase_a.get("spot_check_agreement_min", 0.70))
    min_flagged = int(phase_a.get("min_flagged_in_sample", 10))
    inter_rater_min = float(phase_a.get("inter_rater_min", 0.70))
    negative_max = float(phase_a.get("negative_control_max", 0.40))
    max_abstention = float(phase_a.get("max_abstention", 0.30))

    truth = json.loads((DIR / "truth.json").read_text(encoding="utf-8"))
    truth_broken = json.loads((DIR / "truth_broken.json").read_text(encoding="utf-8"))

    verdicts = [load_verdicts(f"verdicts_{n}.json") for n in (1, 2)]
    missing = [n for n, v in zip((1, 2), verdicts, strict=True) if v is None]
    if missing:
        print(
            f"missing verdicts for adjudicator(s) {missing}. Write "
            "reports/adjudication/verdicts_1.json and verdicts_2.json, each a JSON object "
            "mapping seller to one of: " + ", ".join(sorted(VALID_ANSWERS))
        )
        return 1

    v1, v2 = verdicts
    assert v1 is not None and v2 is not None

    v1_path = DIR / "verdicts_1.json"
    v2_path = DIR / "verdicts_2.json"
    identical = verdict_files_identical(v1_path, v2_path)
    hashes = {v1_path.name: sha256_file(v1_path), v2_path.name: sha256_file(v2_path)}

    rate1, ok1, n1 = agreement(v1, truth)
    rate2, ok2, n2 = agreement(v2, truth)
    broken_rate1, _, _ = agreement(v1, truth_broken)
    broken_rate2, _, _ = agreement(v2, truth_broken)

    # Adjudicator-vs-adjudicator on the sellers both answered.
    both = [s for s in truth if v1.get(s) in VALID_ANSWERS and v2.get(s) in VALID_ANSWERS]
    inter = sum(1 for s in both if v1[s] == v2[s]) / len(both) if both else 0.0
    # A file compared with itself is not an independent rater. Force the check to
    # fail even if every answer matches, and record why.
    inter_value = 0.0 if identical else round(inter, 4)
    inter_pass = (not identical) and inter >= inter_rater_min

    adjudicators = [v1, v2]
    abstention = (
        sum(1 for v in adjudicators for s in truth if v.get(s) == "cannot_tell") / (2 * len(truth))
        if truth
        else 0.0
    )

    flagged_sellers = [s for s, t in truth.items() if t["bucket"] == "could_be_self_dealing"]
    advertised = advertised_addresses()
    anchor_ok = bool(advertised)
    false_positives = [s for s in flagged_sellers if s.lower() in advertised]

    best_rate = max(rate1, rate2)
    worst_broken = min(broken_rate1, broken_rate2)
    note = (
        "The adjudicators are language models, not humans (owner decision, 2026-10-28). "
        "They share training data and priors with the author of the classifier, so their "
        "independence is weaker than a human's. Blinding, the prior-committed rubric, the "
        "inter-rater check and the negative control are what stand in for human scrutiny."
    )
    if identical:
        note += (
            " WARNING: verdicts_1.json and verdicts_2.json are byte-identical "
            f"(sha256 {hashes['verdicts_1.json']}). Identical files cannot serve as two "
            "independent raters — one run copied twice, or two deterministic runs with no "
            "measurable disagreement. The inter-rater check is forced to fail."
        )

    checks = {
        "discriminative": {
            "value": len(flagged_sellers),
            "threshold": min_flagged,
            "pass": len(flagged_sellers) >= min_flagged,
        },
        "agreement_adjudicator_1": {
            "value": round(rate1, 4),
            "threshold": min_agreement,
            "pass": rate1 >= min_agreement,
        },
        "agreement_adjudicator_2": {
            "value": round(rate2, 4),
            "threshold": min_agreement,
            "pass": rate2 >= min_agreement,
        },
        "inter_rater": {
            "value": inter_value,
            "threshold": inter_rater_min,
            "pass": inter_pass,
            "verdict_files_identical": identical,
            "note": (
                "the two verdict files are byte-identical; identical output cannot serve "
                "as two independent raters (one run copied twice, or two deterministic "
                "runs with no measurable disagreement)"
                if identical
                else "the two verdict files are distinct"
            ),
        },
        "negative_control": {
            "value": round(worst_broken, 4),
            "threshold": negative_max,
            "pass": worst_broken <= negative_max,
        },
        "external_anchor": {
            "value": len(false_positives),
            "threshold": 0,
            "pass": anchor_ok and not false_positives,
            "verified": anchor_ok,
        },
        "abstention": {
            "value": round(abstention, 4),
            "threshold": max_abstention,
            "pass": abstention <= max_abstention,
        },
    }

    payload = {
        "sample_size": len(truth),
        "flagged_sellers": len(flagged_sellers),
        "agreement": {
            "adjudicator_1": {"rate": round(rate1, 4), "agreed": ok1, "considered": n1},
            "adjudicator_2": {"rate": round(rate2, 4), "agreed": ok2, "considered": n2},
            "inter_rater": inter_value,
            "inter_rater_raw": round(inter, 4),
        },
        "verdict_files_identical": identical,
        "verdict_file_sha256": hashes,
        "negative_control": {
            "adjudicator_1_vs_broken": round(broken_rate1, 4),
            "adjudicator_2_vs_broken": round(broken_rate2, 4),
        },
        "advertised_addresses_checked": len(advertised),
        "false_positives_against_catalogue": false_positives,
        "abstention_rate": round(abstention, 4),
        "checks": checks,
        "all_pass": all(c["pass"] for c in checks.values()),
        "best_agreement": round(best_rate, 4),
        "note": note,
    }

    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"sample                : {len(truth)} sellers, {len(flagged_sellers)} flagged")
    print(f"agreement adj1 / adj2 : {rate1:.1%} / {rate2:.1%}  (need >= {min_agreement:.0%})")
    print(
        f"inter-rater           : {inter_value:.1%}  (need >= {inter_rater_min:.0%}; "
        f"raw {inter:.1%})"
    )
    if identical:
        print("  WARNING: verdicts_1.json and verdicts_2.json are byte-identical")
        print("           (one copy, or deterministic runs); inter-rater forced to 0")
    print(f"negative control      : {worst_broken:.1%}  (need <= {negative_max:.0%})")
    print(f"abstention            : {abstention:.1%}  (need <= {max_abstention:.0%})")
    print(
        f"external anchor       : {len(false_positives)} false positives "
        f"of {len(advertised)} advertised addresses"
    )
    print(f"ALL PASS              : {payload['all_pass']}")
    print(f"wrote {OUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
