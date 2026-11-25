# A2A agent card — notes

**Status: UNVERIFIED.**

## What is served

`src/repindex/api/static/agent-card.json`, served at `/.well-known/agent-card.json`.

The card declares one skill, `check_counterparty`, and carries a disclaimer field
because the score it advertises is not independently validated.

## What has **not** been done

Task B3 requires reading the current A2A specification, recording its version and
date, and validating the card against the published schema if one exists.

**None of that has happened.** This file exists so the omission is explicit rather
than silent. Specifically:

- the specification was **not fetched** for this build;
- the card therefore **omits `protocolVersion` entirely** (2026-10-31). It previously
  carried `"0.1"`, a placeholder presented to clients as a fact; the independent Gate B
  review (F15) showed that nothing in the repository recorded where the value came
  from. An absent field is honest; an invented one is not;
- the field set (`name`, `description`, `url`, `version`, `capabilities`, `skills`)
  follows the general shape of A2A cards but has **not** been checked against the
  current schema;
- card signing was **not implemented**, and the current signing method was not
  examined.

## Why this is recorded rather than guessed

Prompt Section 3.1 forbids inventing API shapes or spec details. A card that looks
valid but was written from memory is worse than a card marked unverified, because the
first invites a deployment that fails silently.

Gate check GB.7 accepts this file's `UNVERIFIED` marker instead of requiring a
validated card, and prints a note that the spec version and date still need recording.

## What would resolve it

1. Fetch the A2A specification and record its version and fetch date here.
2. Compare the field set against the published schema; adjust the card.
3. If a signing method is specified and verifiable, implement it; otherwise record
   that decision here and in `the owner decision record`.
