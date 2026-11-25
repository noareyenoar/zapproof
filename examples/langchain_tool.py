"""`check_counterparty` as a LangChain tool.

This wrapper calls the same function the MCP server exposes, so an agent using
LangChain and an agent using MCP get byte-identical answers.

Install:  pip install langchain-core zapproof-mcp
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from repindex.mcp.server import check_counterparty as _check


@tool
def check_counterparty(identifier: str, rail: str = "any") -> dict[str, Any]:
    """Check an EVM address's settled-payment record on Base before paying it.

    Returns settlements, distinct payers, active days and a rules-based
    interpretation. `found: false` and `insufficient_evidence` are not negative
    signals. Only Base evidence is indexed; rail="lightning" returns
    rail_not_indexed. Use it as one input among several, never as an approval to pay.
    """
    return _check(identifier, rail)  # type: ignore[arg-type]
