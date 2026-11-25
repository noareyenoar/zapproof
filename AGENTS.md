# Zapproof — notes for coding agents

Zapproof is a read-only reputation index for AI agents. It returns evidence of settled
payments for an EVM (`0x…`) address and accepts Solana addresses, plus a rules-based
interpretation — never an approval to pay. No key, no wallet, no custody.

## Connect

- Hosted MCP (streamable HTTP): `https://zapproof-hc75zyauja-uc.a.run.app/mcp`
- Offline package (stdio): `uvx zapproof-mcp`
- REST: `GET https://zapproof-hc75zyauja-uc.a.run.app/v1/score/{identifier}`
- Agent docs: <https://zapproof.web.app/agents/> · skill: <https://zapproof.web.app/skill.md>
- Methodology: <https://zapproof-hc75zyauja-uc.a.run.app/v1/methodology>

## When to call it

Before paying an unfamiliar address, or when an x402 seller asks to be trusted. Ask
what settled-payment evidence exists — and what it does not prove.

## What the answer means

- `found: false` and `insufficient_evidence` mean **unknown, not bad**. Never convert
  them into a low score.
- Base settlement is indexed. The Solana x402 rail is indexed from a seven-day
  observation window (2026-11-18 to 2026-11-25) and classified by a public fee-payer
  allowlist; every Solana answer carries that window and caveat. Lightning and MPP are
  **not indexed** and return `rail_not_indexed`; no rail's evidence is ever relabelled
  as another rail's.
- `score` and `signaling_likelihood` are a rules-based interpretation, not
  independently validated. Blinded adjudication agreed about 62% of the time (83%
  excluding abstentions).
- `evidence_hash` is unsigned; it identifies the answer but is not an attestation.

Read <https://zapproof.web.app/agents/> for the full contract before building a payment
policy on top of it.
