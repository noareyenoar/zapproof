"""An agent that checks before it pays.

The decision function is intentionally small and explicit. The failure this example
guards against is an agent reading `found: false` as "bad" and refusing to trade, or
reading a score as "approved" and paying any amount. Neither is what the evidence says.

Run:
    uv run python examples/payer_agent.py --identifier 0x... --amount 25
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# A missing record is a reason to limit exposure, not a reason to refuse. The number
# is an example, not a recommendation.
UNKNOWN_CAP_USDC = 1.0


@dataclass(frozen=True)
class Decision:
    action: str  # "pay" | "pay_capped" | "stop"
    cap_usdc: float | None
    reason: str


def decide(answer: dict[str, Any], amount_usdc: float) -> Decision:
    """Turn one tool answer into a payment policy decision."""
    if answer.get("status") == "rail_not_indexed" or answer.get("rail") == "lightning":
        return Decision("stop", None, "No evidence is indexed for this rail; it is not covered.")

    if "error" in answer:
        return Decision("stop", None, "The index is unavailable or the input is invalid.")

    if answer.get("found") and answer.get("status") == "scored":
        return Decision(
            "pay",
            amount_usdc,
            "Enough evidence to score. The score is one input, not an approval.",
        )

    if answer.get("found") and answer.get("status") == "insufficient_evidence":
        return Decision(
            "pay_capped",
            min(amount_usdc, UNKNOWN_CAP_USDC),
            "Some evidence, too little to score: cap the amount and require human approval.",
        )

    return Decision(
        "pay_capped",
        min(amount_usdc, UNKNOWN_CAP_USDC),
        "Unknown is not bad, and it is not zero: cap the amount and require human approval.",
    )


async def ask(identifier: str, rail: str, command: list[str]) -> dict[str, Any]:
    params = StdioServerParameters(command=command[0], args=command[1:])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool(
            "check_counterparty", {"identifier": identifier, "rail": rail}
        )
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            answer: dict[str, Any] = json.loads(text)
            return answer
    return {"error": "empty_response"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--identifier", required=True, help="EVM address or supported DID")
    parser.add_argument("--amount", type=float, default=25.0, help="USDC the agent is about to pay")
    parser.add_argument("--rail", default="any", choices=["base", "lightning", "any"])
    parser.add_argument(
        "--server",
        default=f"{sys.executable} -m repindex.mcp.server",
        help="command that starts the MCP server over stdio",
    )
    args = parser.parse_args(argv)

    answer = asyncio.run(ask(args.identifier, args.rail, args.server.split()))
    decision = decide(answer, args.amount)

    print(json.dumps({"payload": answer, "decision": decision.__dict__}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
