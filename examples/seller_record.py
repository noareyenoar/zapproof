"""Print a seller's own record as a badge-like summary.

The summary is the evidence, not a seal. It shows the numbers an address can point
at — settlements, distinct payers, active days, as-of block — and says what the
record does not prove.

Run:
    uv run python examples/seller_record.py 0x...
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from repindex.mcp.server import check_counterparty


def format_record(answer: dict[str, Any]) -> str:
    """Human-readable summary. Unknown is rendered as unknown, never as zero."""
    if "error" in answer:
        return f"Index error: {answer['error']} — {answer.get('detail', '')}".strip()

    if answer.get("status") == "rail_not_indexed":
        return (
            "No evidence is indexed for this rail. This is not a low score and not a "
            "statement about the address."
        )

    summary = answer.get("evidence_summary") or {}
    score = answer.get("score") if answer.get("found") else "n/a (not zero)"
    lines = [
        f"Address:         {answer.get('identifier')}",
        f"Status:          {answer.get('status')}",
        f"Settlements:     {summary.get('settlements', 'n/a')}",
        f"Distinct payers: {summary.get('counterparties', 'n/a')}",
        f"Active days:     {summary.get('active_days', 'n/a')}",
        f"Score:           {score}",
        f"As of block:     {answer.get('as_of', 'n/a')}",
        f"Source:          {answer.get('source', 'live index')}",
    ]
    if answer.get("note"):
        lines.append(f"Note:           {answer['note']}")
    lines.append(
        "The record is evidence, not a verdict. The interpretation is not independently "
        "validated (blinded adjudication: 61.5%, 40/65; 83.3% excluding abstentions)."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("identifier")
    parser.add_argument("--json", action="store_true", help="print the raw answer instead")
    args = parser.parse_args(argv)

    answer = check_counterparty(args.identifier, "any")
    print(json.dumps(answer, indent=2, sort_keys=True) if args.json else format_record(answer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
