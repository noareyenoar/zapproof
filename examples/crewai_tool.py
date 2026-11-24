"""`check_counterparty` as a CrewAI tool.

Install:  pip install crewai zapproof-mcp
"""

from __future__ import annotations

from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from repindex.mcp.server import check_counterparty as _check


class CheckCounterpartyInput(BaseModel):
    """Arguments an agent supplies."""

    identifier: str = Field(
        description="EVM address, did:ethr/did:pkh, or x402 seller wallet to check before payment"
    )
    rail: str = Field(
        default="any",
        description="base, lightning, or any; only Base evidence is indexed today",
    )


class CheckCounterpartyTool(BaseTool):
    """Payment-backed reputation lookup for a counterparty."""

    name: str = "check_counterparty"
    description: str = (
        "Check an EVM address's settled-payment record on Base before paying it. "
        "Returns settlements, distinct payers, active days and a rules-based "
        "interpretation. Unknown is not bad: found:false and insufficient_evidence are "
        "not negative signals, and only Base evidence is indexed."
    )
    args_schema: type[BaseModel] = CheckCounterpartyInput

    def _run(self, identifier: str, rail: str = "any") -> dict[str, Any]:
        if rail not in ("base", "lightning", "any"):
            return {"error": "invalid_rail", "detail": "rail must be one of: base, lightning, any"}
        return _check(identifier, rail)  # type: ignore[arg-type]
