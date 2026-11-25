"""Response models for the public API.

The shape separates three things that are easy to blur and must not be:

1. **Evidence** — facts read from the database. These are exact: a settlement either
   exists on chain or it does not.
2. **Interpretation** — the classifier's opinion about that evidence. Measured against
   blinded adjudication at 61.5% agreement on 2026-11-16, which is better than chance
   and worse than a verdict.
3. **Disclaimer** — the statement that the interpretation has not been validated.

A caller that treats (2) as if it were (1) will make a bad decision, and the response
makes that hard to do by accident.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from repindex import adjudication

# Built from `repindex.adjudication`, never typed by hand: the report is the source of
# truth and tests/test_adjudication_facts.py fails if the two disagree.
DISCLAIMER_TEXT = adjudication.disclaimer_text()

# Gate check GB.12 greps for this exact phrase in every response.
DISCLAIMER_MARKER = "not independently validated"


class Window(BaseModel):
    start_block: int
    end_block: int
    start_time: int
    end_time: int


class Evidence(BaseModel):
    """What the database can state as fact about this counterparty."""

    settlements: int = Field(description="Number of settled payments observed.")
    counterparties: int = Field(description="Distinct addresses that paid this seller.")
    active_days: int = Field(description="Distinct calendar days with at least one payment.")
    window: Window
    as_of_block: int


class RuleContribution(BaseModel):
    rule: str
    fired: bool
    contribution: float
    detail: str


class Interpretation(BaseModel):
    """The classifier's reading. Opinion, not fact."""

    signaling_likelihood: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "0 to 1. Higher means more signaling-like. Null when there is not enough "
            "evidence to judge — null is not zero and is not a clean bill of health."
        ),
    )
    judged: bool = Field(
        description="False when the evidence was below the floor for a judgement."
    )
    rules: list[RuleContribution] = Field(
        description="Every rule, whether it fired and why. Nothing hidden in a model."
    )
    methodology_version: str


class LabelProvenance(BaseModel):
    label_type: str = Field(description="e.g. operator_group")
    label_value: str = Field(description="e.g. cluster:61")
    entity_id: int | None = Field(
        default=None,
        description="The operator group id, when the label names one; use /v1/entity/{id}.",
    )
    method: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str


SCHEMA_VERSION = "zapproof.score.v1"


class ScoreResponse(BaseModel):
    """A found counterparty. `score` is null exactly when status is insufficient_evidence."""

    found: Literal[True] = True
    identifier: str
    schema_version: str = Field(
        default=SCHEMA_VERSION,
        description="Version of this response shape, so a stored answer stays parseable.",
    )
    evidence_hash: str | None = Field(
        default=None,
        description=(
            "SHA-256 over the canonical response body (excluding this field, the "
            "permalink and the disclaimer). Store it with the answer; re-fetching and "
            "comparing tells you whether the evidence changed. Unsigned: it is a "
            "fingerprint, not an attestation."
        ),
    )
    permalink: str | None = Field(
        default=None,
        description=(
            "Stable URL for this lookup. The content may change; the hash says what "
            "it was."
        ),
    )
    not_an_endorsement: bool = Field(
        default=True,
        description="Always true. This response is evidence, never an approval to pay.",
    )
    rail: Literal["base", "solana"] = "base"
    coverage: str | None = Field(
        default=None,
        description=(
            "How this rail's evidence was observed. Null for Base, whose snapshot is "
            "described by the methodology. A Solana answer states its window and "
            "classifier here."
        ),
    )
    status: Literal["scored", "insufficient_evidence"]
    score: float | None = Field(
        default=None,
        ge=0.0,
        le=100.0,
        description="Null when evidence is below the minimum. Never zero for unknown.",
    )
    confidence_band: Literal["low", "medium", "high"]
    evidence: Evidence
    interpretation: Interpretation
    label_provenance: list[LabelProvenance] = Field(default_factory=list)
    methodology_version: str
    disclaimer: str = DISCLAIMER_TEXT


class NotFoundResponse(BaseModel):
    """No *seller-side* score. Explicitly not a score of zero.

    `role_observed` distinguishes an address the index has never seen from one it
    knows well as a payer but has never seen sell (F13). Both are `found: false`
    because the product scores sellers, but only the first is "unknown".
    """

    found: Literal[False] = False
    identifier: str
    schema_version: str = SCHEMA_VERSION
    evidence_hash: str | None = None
    permalink: str | None = None
    not_an_endorsement: bool = True
    rail: Literal["base", "solana"] = "base"
    coverage: str | None = Field(
        default=None,
        description="How this rail's evidence was observed, when the rail is not Base.",
    )
    reason: str = "no evidence for this identifier in the index"
    note: str = "This is not a low score. An address with no activity is unknown, not bad."
    role_observed: Literal["payer", "relayer", "payer_and_relayer"] | None = None
    payer_settlements: int | None = Field(
        default=None,
        description="Settlements in which this address paid. Present when the address is known.",
    )
    seller_settlements: int | None = Field(
        default=None,
        description=(
            "Settlements in which this address sold. Zero when only the payer role "
            "is known."
        ),
    )
    disclaimer: str = DISCLAIMER_TEXT


class EntityResponse(BaseModel):
    group_id: int
    members: list[str]
    group_size: int
    confidence: float
    method: str
    evidence: str
    methodology_version: str
    disclaimer: str = DISCLAIMER_TEXT


class MethodologyResponse(BaseModel):
    version: str
    summary: str
    signals: list[str]
    not_signals: list[str]
    agreement_with_blinded_readers: float
    # F11: the headline number's material caveats. Published as fields rather than
    # prose so a caller reading only the JSON still sees them.
    adjudication_caveats: list[str]
    limitation: str
    disclaimer: str = DISCLAIMER_TEXT


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    as_of_block: int
    sellers_indexed: int
    methodology_version: str
    # Present even here: gate check GB.12 requires the limitation on every response,
    # and an exception for the health endpoint would be the first crack in it.
    disclaimer: str = DISCLAIMER_TEXT
