"""The blinded-adjudication figures, in one place.

Canonical artifact: ``reports/adjudication/result.json``, produced by
``scripts/validate_adjudication.py`` from the verdict files and ``truth.json``.
``reports/adjudication/run_metadata.json`` records which run that is.

Every public surface that quotes the agreement rate — the API disclaimer, the
``/v1/methodology`` caveats, the MCP tool description, the A2A card — builds its
sentence from this module. ``tests/test_adjudication_facts.py`` recomputes the
numbers from the raw artifacts, so a stale figure fails the suite instead of
shipping to a caller.

Counts, not rounded rates, are the stored facts: a percentage recomputed from
counts cannot drift by a rounding step when the wording changes.
"""

from __future__ import annotations

ADJUDICATION_DATE = "2026-11-16"
SAMPLE_SIZE = 65
FLAGGED_SELLERS = 28

# Adjudicator-vs-classifier, counting `cannot_tell` as disagreement (the conservative
# reading, and the one published).
AGREED = 40
CONSIDERED = 65
ABSTENTIONS = 17

# The same run, abstentions excluded.
EXCLUDING_CONSIDERED = 48

AGREEMENT_RATE = AGREED / CONSIDERED
ABSTENTION_RATE = ABSTENTIONS / CONSIDERED
EXCLUDING_ABSTENTIONS_RATE = AGREED / EXCLUDING_CONSIDERED

# Headline wording used in prose: rounded to a whole percent, the house style these
# surfaces have used since the first adjudication run.
AGREEMENT_HEADLINE = "about 62%"
EXCLUDING_HEADLINE = "83%"


def disclaimer_text() -> str:
    """The one paragraph every API response and MCP answer carries."""
    return (
        "This score is not independently validated. It summarises observed payment "
        "evidence and a rules-based interpretation of it; blinded adjudication agreed "
        f"with the interpretation {AGREEMENT_HEADLINE} of the time, and "
        f"{EXCLUDING_HEADLINE} when the adjudicators' abstentions are excluded. The "
        "adjudicators were language models, not humans. It is not a statement about "
        "any person's or company's honesty, and 'insufficient_evidence' is not a low "
        "score. Unknown is not bad."
    )


def agreement_caveat() -> str:
    """The detailed arithmetic, for `/v1/methodology`."""
    return (
        f"{AGREEMENT_RATE:.1%} counts `cannot_tell` answers as disagreements: "
        f"{AGREED} agreements out of {CONSIDERED}, with {ABSTENTIONS} abstentions "
        f"({ABSTENTION_RATE:.1%}). Excluding the abstentions the figure is "
        f"{EXCLUDING_ABSTENTIONS_RATE:.1%} ({AGREED}/{EXCLUDING_CONSIDERED}); the "
        "lower number is published because it is conservative."
    )


def limitation_text() -> str:
    """One sentence, for the methodology response's `limitation` field."""
    return (
        "The interpretation is not yet reliable: it agreed with blinded adjudication "
        f"{AGREEMENT_RATE:.1%} of the time, or {EXCLUDING_ABSTENTIONS_RATE:.1%} "
        "excluding abstentions, and the adjudicators were language models rather "
        "than humans."
    )


def agreement_sentence() -> str:
    """Compact form for the MCP tool description, where context is paid for."""
    return (
        f"Blinded adjudication agreed with it {AGREEMENT_HEADLINE} of the time "
        f'({AGREED}/{CONSIDERED}, counting {ABSTENTIONS} "cannot tell" answers as '
        f"disagreements; {EXCLUDING_HEADLINE} excluding them)."
    )
