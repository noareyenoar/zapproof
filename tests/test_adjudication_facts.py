"""The figures every public surface quotes must equal the committed report.

`reports/adjudication/result.json` is produced by `scripts/validate_adjudication.py`
from `truth.json` and the two verdict files. `repindex.adjudication` holds the counts
that the API disclaimer, the `/v1/methodology` caveats, and the MCP tool description
render. This test recomputes the numbers from the raw artifacts, so a stale figure
fails here instead of reaching a caller.

Before this existed, the API published the 2026-10-28 run's 63.1% (41/65) while the
committed report described the 2026-11-16 run's 61.5% (40/65). Gate B's language audit
only greps for "not independently validated", so nothing caught the disagreement.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from repindex import adjudication
from repindex.api import queries
from repindex.api.models import DISCLAIMER_TEXT
from repindex.mcp.server import TOOL_DESCRIPTION

REPO_ROOT = Path(__file__).resolve().parents[1]
ADJ = REPO_ROOT / "reports" / "adjudication"
VALID_ANSWERS = {
    "requires_independent_customers",
    "could_be_self_dealing",
    "cannot_tell",
}


def _load(name: str) -> Any:
    return json.loads((ADJ / name).read_text(encoding="utf-8"))


def _scored(verdicts: dict[str, str], truth: dict[str, dict[str, Any]]) -> tuple[int, int]:
    """(agreed, considered) with `cannot_tell` counting as a disagreement."""
    considered = sum(1 for seller in truth if verdicts.get(seller) in VALID_ANSWERS)
    agreed = sum(1 for seller, row in truth.items() if verdicts.get(seller) == row["bucket"])
    return agreed, considered


def test_counts_match_the_committed_result() -> None:
    result = _load("result.json")
    truth = _load("truth.json")
    v1 = _load("verdicts_1.json")
    v2 = _load("verdicts_2.json")

    assert adjudication.SAMPLE_SIZE == result["sample_size"] == len(truth)
    assert result["flagged_sellers"] == adjudication.FLAGGED_SELLERS

    for verdicts in (v1, v2):
        agreed, considered = _scored(verdicts, truth)
        assert (agreed, considered) == (adjudication.AGREED, adjudication.CONSIDERED)

    reported = result["agreement"]["adjudicator_1"]
    assert reported["agreed"] == adjudication.AGREED
    assert reported["considered"] == adjudication.CONSIDERED
    assert round(adjudication.AGREEMENT_RATE, 4) == reported["rate"]


def test_abstention_and_excluding_rows_match() -> None:
    result = _load("result.json")
    truth = _load("truth.json")
    v1 = _load("verdicts_1.json")
    v2 = _load("verdicts_2.json")

    abstentions = sum(
        1 for verdicts in (v1, v2) for seller in truth if verdicts.get(seller) == "cannot_tell"
    )
    assert abstentions == 2 * adjudication.ABSTENTIONS
    assert round(adjudication.ABSTENTION_RATE, 4) == result["abstention_rate"]

    answered = [s for s in truth if v1.get(s) in VALID_ANSWERS and v1[s] != "cannot_tell"]
    assert len(answered) == adjudication.EXCLUDING_CONSIDERED
    excluded_agreed = sum(1 for s in answered if v1[s] == truth[s]["bucket"])
    assert excluded_agreed == adjudication.AGREED
    assert round(adjudication.EXCLUDING_ABSTENTIONS_RATE, 4) == round(
        excluded_agreed / len(answered), 4
    )


def test_public_strings_use_the_current_run() -> None:
    rate = f"{adjudication.AGREEMENT_RATE:.1%}"
    excluding = f"{adjudication.EXCLUDING_ABSTENTIONS_RATE:.1%}"
    assert rate == "61.5%"
    assert excluding == "83.3%"

    assert rate in adjudication.agreement_caveat()
    assert rate in adjudication.limitation_text()
    assert adjudication.agreement_sentence() in TOOL_DESCRIPTION
    assert adjudication.AGREEMENT_HEADLINE in DISCLAIMER_TEXT
    assert adjudication.EXCLUDING_HEADLINE in DISCLAIMER_TEXT
    assert queries.AGREEMENT_WITH_BLINDED_READERS == adjudication.AGREEMENT_RATE


def test_no_surface_quotes_the_previous_run() -> None:
    """The 2026-10-28 figures must not survive on a public surface."""
    for text in (DISCLAIMER_TEXT, TOOL_DESCRIPTION, adjudication.agreement_caveat()):
        assert "63.1%" not in text
        assert "82.0%" not in text
        assert "41/65" not in text
        assert "41/50" not in text
