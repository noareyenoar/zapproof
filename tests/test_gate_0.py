"""Tests for gates/gate_0.py.

The point of these tests is not coverage. It is to prove the gate checks can FAIL.

A gate whose checks always pass is worse than no gate at all, because it launders
whatever the project happened to build into a green light. G0.3 in particular is
the project's first kill criterion, so most of this file is about making sure it
says no when it should.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import gate_0

THRESHOLDS: dict[str, Any] = {
    "global": {"max_monthly_cost_usd": 0},
    "phase0": {
        "decoded_sample_min": 1000,
        "onchain_recheck_sample": 50,
        "crosscheck_window_hours": 1,
        "crosscheck_max_relative_diff": 0.02,
        "unresolved_token_share_max": 0.02,
        "protocol_evidence_txs_min": 20,
    },
}

WINDOW = {
    "starts_at": "2026-10-15T00:00:00Z",
    "ends_at": "2026-10-15T01:00:00Z",
    "settlement_count": 100,
    "total_token_amount": 5_000_000,
}


def make_source(name: str = "jsonrpc", **overrides: Any) -> dict[str, Any]:
    source: dict[str, Any] = {
        "name": name,
        "usable": True,
        "independent": True,
        "auth_required": "none",
        "free_tier_notes": "no key needed, documented public limits",
        "rate_limit": "10 requests/second",
        "terms": "public read-only use, no restrictions found on this use",
        "projected_monthly_cost_usd": 0,
        "window": dict(WINDOW),
    }
    source.update(overrides)
    return source


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


# --------------------------------------------------------------------------- #
# G0.3 — the kill criterion
# --------------------------------------------------------------------------- #


def test_g0_3_fails_when_only_one_source_works(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probe = tmp_path / "source_probe.json"
    write_json(
        probe, {"sources": [make_source("jsonrpc"), make_source("substreams", usable=False)]}
    )
    monkeypatch.setattr(gate_0, "SOURCE_PROBE", probe)

    result = gate_0.check_g0_3(THRESHOLDS)

    assert result.status == "FAIL"
    assert "FEWER THAN TWO USABLE SOURCES" in " ".join(result.notes)
    assert "kill criterion" in " ".join(result.notes)


def test_g0_3_fails_when_probe_is_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(gate_0, "SOURCE_PROBE", tmp_path / "nope.json")

    result = gate_0.check_g0_3(THRESHOLDS)

    assert result.status == "FAIL"
    assert result.measured == "not measured"


def test_g0_3_fails_on_self_comparison(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Two entries with the same name are one source counted twice."""
    probe = tmp_path / "source_probe.json"
    write_json(probe, {"sources": [make_source("jsonrpc"), make_source("jsonrpc")]})
    monkeypatch.setattr(gate_0, "SOURCE_PROBE", probe)

    result = gate_0.check_g0_3(THRESHOLDS)

    assert result.status == "FAIL"
    assert "self-comparison" in " ".join(result.notes)


def test_g0_3_fails_when_sources_share_an_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two views over one upstream dataset are not independent evidence."""
    probe = tmp_path / "source_probe.json"
    write_json(
        probe,
        {
            "sources": [
                make_source("substreams", origin="sourcify-index"),
                make_source("bitquery", origin="sourcify-index"),
            ]
        },
    )
    monkeypatch.setattr(gate_0, "SOURCE_PROBE", probe)

    result = gate_0.check_g0_3(THRESHOLDS)

    assert result.status == "FAIL"
    assert "same origin" in " ".join(result.notes)


def test_g0_3_fails_when_counts_disagree_beyond_tolerance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probe = tmp_path / "source_probe.json"
    write_json(
        probe,
        {
            "sources": [
                make_source("jsonrpc"),
                make_source("bitquery", window={**WINDOW, "settlement_count": 80}),
            ]
        },
    )
    monkeypatch.setattr(gate_0, "SOURCE_PROBE", probe)

    result = gate_0.check_g0_3(THRESHOLDS)

    assert result.status == "FAIL"
    assert "exceeds" in " ".join(result.notes)


def test_g0_3_fails_when_amounts_disagree_beyond_tolerance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probe = tmp_path / "source_probe.json"
    write_json(
        probe,
        {
            "sources": [
                make_source("jsonrpc"),
                make_source("bitquery", window={**WINDOW, "total_token_amount": 1_000_000}),
            ]
        },
    )
    monkeypatch.setattr(gate_0, "SOURCE_PROBE", probe)

    result = gate_0.check_g0_3(THRESHOLDS)

    assert result.status == "FAIL"


def test_g0_3_fails_when_sources_measured_different_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probe = tmp_path / "source_probe.json"
    write_json(
        probe,
        {
            "sources": [
                make_source("jsonrpc"),
                make_source(
                    "bitquery",
                    window={**WINDOW, "starts_at": "2026-10-14T00:00:00Z"},
                ),
            ]
        },
    )
    monkeypatch.setattr(gate_0, "SOURCE_PROBE", probe)

    result = gate_0.check_g0_3(THRESHOLDS)

    assert result.status == "FAIL"
    assert "same window" in " ".join(result.notes)


def test_g0_3_fails_when_independence_is_not_asserted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probe = tmp_path / "source_probe.json"
    write_json(
        probe,
        {"sources": [make_source("jsonrpc"), make_source("bitquery", independent=False)]},
    )
    monkeypatch.setattr(gate_0, "SOURCE_PROBE", probe)

    result = gate_0.check_g0_3(THRESHOLDS)

    assert result.status == "FAIL"
    assert "independent" in " ".join(result.notes)


def test_g0_3_fails_when_a_source_does_not_document_its_limits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probe = tmp_path / "source_probe.json"
    write_json(
        probe,
        {"sources": [make_source("jsonrpc"), make_source("bitquery", rate_limit="")]},
    )
    monkeypatch.setattr(gate_0, "SOURCE_PROBE", probe)

    result = gate_0.check_g0_3(THRESHOLDS)

    assert result.status == "FAIL"


def test_g0_3_passes_with_two_agreeing_independent_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probe = tmp_path / "source_probe.json"
    write_json(
        probe,
        {
            "sources": [
                make_source("jsonrpc"),
                make_source("bitquery", window={**WINDOW, "settlement_count": 99}),
            ]
        },
    )
    monkeypatch.setattr(gate_0, "SOURCE_PROBE", probe)

    result = gate_0.check_g0_3(THRESHOLDS)

    assert result.status == "PASS", result.notes


# --------------------------------------------------------------------------- #
# G0.4 — token coverage
# --------------------------------------------------------------------------- #


def make_census(
    tokens: list[dict[str, Any]], mechanism_share: float | None = 0.0
) -> dict[str, Any]:
    """Build a census payload in the shape the gate reads.

    `mechanism_share` defaults to 0.0 (measurable and clean). Pass None to model a
    census that carries no mechanism measurement at all.
    """
    payload: dict[str, Any] = {"tokens": tokens}
    if mechanism_share is not None:
        payload["mechanism_paths"] = {
            "eip3009_authorization_used": 1000,
            "permit2_proxy_events": 3,
            "unresolved_mechanism_share": mechanism_share,
        }
    return payload


def test_g0_4_fails_when_unresolved_share_exceeds_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    census = tmp_path / "token_census.json"
    write_json(
        census,
        make_census(
            [
                {"address": "0xusdc", "resolved": True, "settlement_count": 900},
                {
                    "address": "0xmystery",
                    "resolved": False,
                    "settlement_count": 100,
                    "sample_tx_hashes": ["0x" + "ab" * 32],
                },
            ]
        ),
    )
    monkeypatch.setattr(gate_0, "TOKEN_CENSUS", census)

    result = gate_0.check_g0_4(THRESHOLDS)

    assert result.status == "FAIL"
    assert "above the 2% cap" in " ".join(result.notes)


def test_g0_4_fails_when_an_unresolved_token_has_no_samples(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    census = tmp_path / "token_census.json"
    write_json(
        census,
        make_census(
            [
                {"address": "0xusdc", "resolved": True, "settlement_count": 1000},
                {"address": "0xmystery", "resolved": False, "settlement_count": 1},
            ]
        ),
    )
    monkeypatch.setattr(gate_0, "TOKEN_CENSUS", census)

    result = gate_0.check_g0_4(THRESHOLDS)

    assert result.status == "FAIL"
    assert "sample transaction hashes" in " ".join(result.notes)


def test_g0_4_passes_at_the_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    census = tmp_path / "token_census.json"
    write_json(
        census,
        make_census(
            [
                {"address": "0xusdc", "resolved": True, "settlement_count": 980},
                {
                    "address": "0xmystery",
                    "resolved": False,
                    "settlement_count": 20,
                    "sample_tx_hashes": ["0x" + "cd" * 32],
                },
            ]
        ),
    )
    monkeypatch.setattr(gate_0, "TOKEN_CENSUS", census)

    result = gate_0.check_g0_4(THRESHOLDS)

    assert result.status == "PASS", result.notes


def test_g0_4_fails_when_the_mechanism_half_was_never_measured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cap is about an unresolved token OR mechanism. A census that only counts
    what it already decodes can never report an unresolved mechanism, so it must not
    be allowed to pass the combined check on the token half alone."""
    census = tmp_path / "token_census.json"
    write_json(
        census,
        make_census(
            [
                {"address": "0xusdc", "resolved": True, "settlement_count": 1000},
                {
                    "address": "0xmystery",
                    "resolved": False,
                    "settlement_count": 1,
                    "sample_tx_hashes": ["0x" + "ef" * 32],
                },
            ],
            mechanism_share=None,
        ),
    )
    monkeypatch.setattr(gate_0, "TOKEN_CENSUS", census)

    result = gate_0.check_g0_4(THRESHOLDS)

    assert result.status == "FAIL"
    assert "mechanism share NOT MEASURED" in result.measured


def test_g0_4_fails_when_the_mechanism_share_alone_exceeds_the_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A clean token census must not carry a dirty mechanism census through."""
    census = tmp_path / "token_census.json"
    write_json(
        census,
        make_census(
            [{"address": "0xusdc", "resolved": True, "settlement_count": 1000}],
            mechanism_share=0.05,
        ),
    )
    monkeypatch.setattr(gate_0, "TOKEN_CENSUS", census)

    result = gate_0.check_g0_4(THRESHOLDS)

    assert result.status == "FAIL"
    assert "mechanism" in result.measured


# --------------------------------------------------------------------------- #
# G0.5 — cost
# --------------------------------------------------------------------------- #


def test_g0_5_is_needs_human_when_any_source_costs_money(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probe = tmp_path / "source_probe.json"
    projection = tmp_path / "backfill_projection.md"
    projection.write_text("# projection\n", encoding="utf-8")
    write_json(
        probe,
        {
            "sources": [
                make_source("jsonrpc"),
                make_source("bitquery", projected_monthly_cost_usd=19),
            ]
        },
    )
    monkeypatch.setattr(gate_0, "SOURCE_PROBE", probe)
    monkeypatch.setattr(gate_0, "BACKFILL_PROJECTION", projection)

    result = gate_0.check_g0_5(THRESHOLDS)

    assert result.status == "NEEDS_HUMAN"
    assert "EXCEEDS BUDGET" in " ".join(result.notes)


def test_g0_5_fails_when_cost_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probe = tmp_path / "source_probe.json"
    projection = tmp_path / "backfill_projection.md"
    projection.write_text("# projection\n", encoding="utf-8")
    source = make_source("jsonrpc")
    del source["projected_monthly_cost_usd"]
    write_json(probe, {"sources": [source]})
    monkeypatch.setattr(gate_0, "SOURCE_PROBE", probe)
    monkeypatch.setattr(gate_0, "BACKFILL_PROJECTION", projection)

    result = gate_0.check_g0_5(THRESHOLDS)

    assert result.status == "FAIL"


# --------------------------------------------------------------------------- #
# G0.6 — evidence citations
# --------------------------------------------------------------------------- #


def test_g0_6_fails_below_the_transaction_minimum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    notes = tmp_path / "protocol_notes.md"
    notes.write_text("\n".join(f"0x{i:064x}" for i in range(5)), encoding="utf-8")
    adr = tmp_path / "0001-data-source.md"
    adr.write_text("# adr\n", encoding="utf-8")
    monkeypatch.setattr(gate_0, "PROTOCOL_NOTES", notes)
    monkeypatch.setattr(gate_0, "ADR_0001", adr)

    result = gate_0.check_g0_6(THRESHOLDS)

    assert result.status == "FAIL"
    assert "5 < 20" in " ".join(result.notes)


def test_g0_6_counts_distinct_hashes_not_repeats(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Repeating one hash twenty times must not satisfy the minimum."""
    notes = tmp_path / "protocol_notes.md"
    notes.write_text("0x" + "ab" * 32 + "\n" * 40, encoding="utf-8")
    adr = tmp_path / "0001-data-source.md"
    adr.write_text("# adr\n", encoding="utf-8")
    monkeypatch.setattr(gate_0, "PROTOCOL_NOTES", notes)
    monkeypatch.setattr(gate_0, "ADR_0001", adr)

    result = gate_0.check_g0_6(THRESHOLDS)

    assert result.status == "FAIL"
    assert "1 distinct" in result.measured


# --------------------------------------------------------------------------- #
# G0.7 — independent review
# --------------------------------------------------------------------------- #


def test_g0_7_fails_when_the_review_never_ran(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(gate_0, "REVIEWS_DIR", tmp_path)
    monkeypatch.setattr(gate_0, "REVIEW_0", tmp_path / "review_0.md")

    result = gate_0.check_g0_7(THRESHOLDS)

    assert result.status == "FAIL"
    assert "has not run" in " ".join(result.notes)


def test_g0_7_fails_when_findings_are_unresolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    review = tmp_path / "review_0.md"
    review.write_text("## Findings\n\nunresolved findings: 2\n", encoding="utf-8")
    monkeypatch.setattr(gate_0, "REVIEWS_DIR", tmp_path)
    monkeypatch.setattr(gate_0, "REVIEW_0", review)

    result = gate_0.check_g0_7(THRESHOLDS)

    assert result.status == "FAIL"
    assert "declare 2" in result.measured or "2 unresolved" in result.measured


def test_g0_7_fails_when_the_review_does_not_declare_a_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    review = tmp_path / "review_0.md"
    review.write_text("Everything looked fine, I guess.\n", encoding="utf-8")
    monkeypatch.setattr(gate_0, "REVIEWS_DIR", tmp_path)
    monkeypatch.setattr(gate_0, "REVIEW_0", review)

    result = gate_0.check_g0_7(THRESHOLDS)

    assert result.status == "FAIL"


def test_g0_7_passes_with_zero_unresolved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    review = tmp_path / "review_0.md"
    review.write_text("## Findings\n\nunresolved findings: 0\n", encoding="utf-8")
    monkeypatch.setattr(gate_0, "REVIEWS_DIR", tmp_path)
    monkeypatch.setattr(gate_0, "REVIEW_0", review)

    result = gate_0.check_g0_7(THRESHOLDS)

    assert result.status == "PASS", result.notes


# --------------------------------------------------------------------------- #
# the gate as a whole
# --------------------------------------------------------------------------- #


def test_gate_exits_non_zero_when_any_check_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gate that reports FAIL must also fail at the process level, or CI would
    treat it as a green light."""
    monkeypatch.setattr(gate_0, "GATE_MD", tmp_path / "gate_0.md")
    monkeypatch.setattr(gate_0, "GATE_JSON", tmp_path / "gate_0.json")
    monkeypatch.setattr(gate_0, "REVIEWS_DIR", tmp_path)
    for name in (
        "SOURCE_PROBE",
        "TOKEN_CENSUS",
        "BACKFILL_PROJECTION",
        "PROTOCOL_NOTES",
        "REVIEW_0",
    ):
        monkeypatch.setattr(gate_0, name, tmp_path / "missing.json")
    monkeypatch.setattr(gate_0, "ADR_0001", tmp_path / "missing_adr.md")
    monkeypatch.setattr(gate_0, "SCRATCH_DB", tmp_path / "missing.db")
    monkeypatch.setattr(gate_0, "ENV_REPORT", tmp_path / "missing_env.md")

    assert gate_0.main() == 1

    payload = json.loads((tmp_path / "gate_0.json").read_text(encoding="utf-8"))
    assert payload["summary"]["PASS"] == 0
    assert payload["checks"]
    for check in payload["checks"]:
        assert check["status"] in {"PASS", "FAIL", "WARN", "NEEDS_HUMAN"}


def test_latest_review_orders_by_number_not_lexicographically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """J3: `review_B10.md` sorted before `review_B9.md`, so G0.7 read the wrong file."""
    for name in ("review_B9.md", "review_B10.md"):
        (tmp_path / name).write_text("unresolved findings: 1\n", encoding="utf-8")
    monkeypatch.setattr(gate_0, "REVIEWS_DIR", tmp_path)

    newest, found = gate_0.latest_review()

    assert newest.name == "review_B10.md"
    assert [path.name for path in found] == ["review_B9.md", "review_B10.md"]


def test_gate_records_commit_and_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(gate_0, "GATE_MD", tmp_path / "gate_0.md")
    monkeypatch.setattr(gate_0, "GATE_JSON", tmp_path / "gate_0.json")
    # I4: G0.1 runs scripts/env_check.py when the report exists, which rewrites the
    # tracked reports/env.md. A missing path makes the check take its "not recorded"
    # branch instead, so the test does not modify the working tree.
    monkeypatch.setattr(gate_0, "ENV_REPORT", tmp_path / "missing_env.md")

    gate_0.main()

    payload = json.loads((tmp_path / "gate_0.json").read_text(encoding="utf-8"))
    assert payload["commit"]
    assert payload["data_snapshot_hash"]
    assert len(payload["data_snapshot_hash"]) == 64
