"""The public analysis counts classifier rules, not corrections (H3).

The third independent review found that a `dispute_override` contribution was counted
as a ninth classifier rule and that an override-only seller was reported as having
"triggered at least one rule" — inflating the public rule-prevalence table and its
headline while every real rule abstained.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts import public_analysis

from repindex.ingest import schema

SELLER = "0x" + "aa" * 20


def _seed(path: Path, *, fired_rule: str | None = None, override: bool = False) -> None:
    conn = schema.connect(path)
    try:
        conn.execute(
            "INSERT INTO settlements (tx_hash, log_index, block_number, block_hash, "
            "block_time, token, decimals, raw_amount, normalized_amount, payer, seller, "
            "facilitator_relayer, nonce, mechanism) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "0x" + "ab" * 32,
                0,
                53_200_000,
                "0x" + "cd" * 32,
                1_793_000_000,
                "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                6,
                1_000_000,
                "1.0",
                "0x" + "11" * 20,
                SELLER,
                None,
                "0x" + "33" * 32,
                "eip3009_authorization_used",
            ),
        )
        conn.execute(
            "INSERT INTO features (seller, unique_payers, repeat_buyer_rate, "
            "reciprocity_rate, amount_entropy, burstiness, peak_hour_share, "
            "fresh_payer_share, self_funding_flag, relayer_concentration, "
            "settlement_count, active_days, observation_window_hours, methodology_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (SELLER, 1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, None, 1, 1, 0.0, "v1"),
        )
        contributions = [
            {"rule": name, "fired": name == fired_rule, "contribution": 0.0}
            for name in public_analysis.RULES
        ]
        if override:
            contributions.append(
                {
                    "rule": "dispute_override",
                    "fired": True,
                    "contribution": -0.35,
                    "computed_before": 0.4,
                    "detail": "signaling_likelihood set to 0.05 by an upheld dispute",
                }
            )
        conn.execute(
            "INSERT INTO classifications (seller, signaling_likelihood, contributions, "
            "methodology_version) VALUES (?,?,?,?)",
            (SELLER, 0.05 if override else 0.0, json.dumps(contributions), "v1"),
        )
        conn.execute(
            "INSERT INTO scores (identifier, status, score, confidence_band, "
            "evidence_settlements, evidence_counterparties, evidence_active_days, "
            "data_window_start, data_window_end, as_of_block, methodology_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                SELLER,
                "scored",
                3.11,
                "low",
                1,
                1,
                1,
                1_793_000_000,
                1_793_000_000,
                53_200_000,
                "v1",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _collect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fired_rule: str | None = None,
    override: bool = False,
) -> dict:
    db = tmp_path / "public.db"
    _seed(db, fired_rule=fired_rule, override=override)
    monkeypatch.setattr(public_analysis, "DB", db)
    return public_analysis.collect()


def test_an_override_only_seller_did_not_trigger_a_classifier_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _collect(tmp_path, monkeypatch, override=True)

    assert data["flagged"] == 0, "an override is not a classifier rule firing"
    assert dict(data["rule_counts"]) == dict.fromkeys(public_analysis.RULES, 0)
    assert data["overrides"] == 1

    report = public_analysis.render(data, commit="0" * 40, fingerprint="0" * 16)

    assert "dispute_override" not in report, "a correction must not become a public rule row"
    assert "carry an upheld classification dispute" in report


def test_a_real_rule_firing_still_counts_alongside_an_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _collect(tmp_path, monkeypatch, fired_rule="self_loop", override=True)

    assert data["flagged"] == 1
    assert data["rule_counts"]["self_loop"] == 1
    assert data["overrides"] == 1
    assert sum(data["rule_counts"].values()) == 1


def test_an_unoverridden_seller_reports_no_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counter starts at zero for every rule, and no other key can appear."""
    data = _collect(tmp_path, monkeypatch)

    assert dict(data["rule_counts"]) == dict.fromkeys(public_analysis.RULES, 0)
    assert data["overrides"] == 0
