"""The demo policy must not turn "no evidence" into "bad", or a score into approval."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parent.parent


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "examples" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # `@dataclass` looks its module up by name, so it must be registered first.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_scored_seller_pays_the_requested_amount() -> None:
    payer = _load("payer_agent")

    decision = payer.decide({"found": True, "status": "scored", "score": 80}, 25.0)

    assert decision.action == "pay"
    assert decision.cap_usdc == 25.0
    assert "not an approval" in decision.reason


def test_unknown_is_capped_not_refused() -> None:
    payer = _load("payer_agent")

    decision = payer.decide({"found": False, "status": "unknown", "score": None}, 25.0)

    assert decision.action == "pay_capped"
    assert decision.cap_usdc == payer.UNKNOWN_CAP_USDC
    assert "Unknown is not bad" in decision.reason


def test_insufficient_evidence_is_capped() -> None:
    payer = _load("payer_agent")

    decision = payer.decide({"found": True, "status": "insufficient_evidence"}, 25.0)

    assert decision.action == "pay_capped"
    assert decision.cap_usdc == payer.UNKNOWN_CAP_USDC


def test_lightning_stops_because_the_rail_is_not_covered() -> None:
    payer = _load("payer_agent")

    decision = payer.decide({"status": "rail_not_indexed", "found": False}, 25.0)

    assert decision.action == "stop"
    assert "not covered" in decision.reason


def test_an_index_error_stops_rather_than_guesses() -> None:
    payer = _load("payer_agent")

    decision = payer.decide({"error": "index_unavailable"}, 25.0)

    assert decision.action == "stop"


def test_seller_record_renders_unknown_as_not_zero() -> None:
    seller = _load("seller_record")

    text = seller.format_record(
        {"identifier": "0xabc", "found": False, "status": "unknown", "score": None}
    )

    assert "not zero" in text
    assert "61.5%" in text


def test_seller_record_renders_evidence() -> None:
    seller = _load("seller_record")

    text = seller.format_record(
        {
            "identifier": "0xabc",
            "found": True,
            "status": "scored",
            "score": 80,
            "as_of": 1000,
            "source": "snapshot",
            "evidence_summary": {
                "settlements": 25,
                "counterparties": 7,
                "active_days": 4,
            },
        }
    )

    assert "Settlements:     25" in text
    assert "snapshot" in text
    assert "evidence, not a verdict" in text
