---
name: zapproof
description: Check what settled-payment evidence exists for an EVM address on Base or a Solana address before paying it. Use before an x402 payment to an unknown counterparty, or when a seller asks to be trusted. The answer is evidence, not a verdict; unknown is not bad.
---

# Zapproof — check a counterparty before paying

Zapproof is a read-only, non-custodial index of settled Base payments (x402 /
EIP-3009, USDC), plus a separate seven-day Solana x402 observation window. It
answers one question:

> What settled-payment evidence exists for this counterparty, and what does it not
> prove?

It never holds funds, never routes payments, and has no token.

## When to use it

Use it when an agent is about to pay an EVM address or a Solana address it does not
control — especially on a 402 challenge from an unfamiliar seller — or when a seller
asks to be trusted. It is one input, not an approval to pay.

## How to call it

The plugin exposes one MCP tool:

```
check_counterparty(identifier: str, rail: "base" | "solana" | "lightning" | "any" = "any")
```

- `identifier`: a `0x` EVM address, `did:ethr:<address>`, or
  `did:pkh:eip155:8453:<address>` for Base; a base58 Solana address for the Solana
  rail. A DID qualified with another chain is rejected.
- `rail`: use `"base"` (or `"any"`) for Base evidence, `"solana"` for a Solana
  address. The Solana index is a seven-day observation window (2026-11-18 to
  2026-11-25) classified by a public fee-payer allowlist — a sample, not a census;
  every Solana answer states that window. Asking `"solana"` about a Base address
  (or the reverse) returns `rail_mismatch` rather than a guess. `rail: "lightning"`
  returns `status: "rail_not_indexed"` — Lightning is not indexed, and no rail's
  evidence is ever relabelled as another rail's.

The same question is available over HTTP:

```http
GET https://zapproof-hc75zyauja-uc.a.run.app/v1/score/{identifier}
```

## Read the answer correctly

| Field | Meaning |
|---|---|
| `status: "scored"` | Enough evidence to interpret. `score` (0–100), `confidence_band`, `evidence_summary`, `rules_fired` and the disclaimer are present. |
| `status: "insufficient_evidence"` | Some record exists, too thin to score. `score` is `null` **on purpose**. Not bad. |
| `found: false` | No seller-side record in this index. It may be a new seller, a payer, or outside coverage. Not a zero. |
| `status: "rail_not_indexed"` | This rail is not covered. Missing coverage, not negative evidence. |
| `error: "rail_mismatch"` | The identifier's rail and the requested `rail` disagree. Do not guess; re-ask on the right rail. |
| `error: "invalid_identifier"` | Fix the input; do not retry unchanged. |
| `index_unavailable` | The hosted index could not answer. Retry later or use the offline snapshot; **do not** conclude anything about the counterparty. |

`signaling_likelihood` is `null` when the evidence was not judged. Null is not zero.
`found: false` and `insufficient_evidence` must never be rendered as a low score or
used by themselves to refuse a counterparty.

The interpretation is rules-based and **not independently validated**. Blinded
adjudication agreed with it about 62% of the time (83% excluding abstentions). Every
response carries this limitation; keep it attached to the number.

## Using the answer in a payment flow

1. Agent receives a 402 challenge naming a payee.
2. Call `check_counterparty` with the payee address.
3. Decide: pay the full amount, pay a capped amount with operator approval, or stop.
4. Record the `methodology_version`, the evidence window and the reason — not just
   the number.

Cap exposure when evidence is thin, and keep a human in the loop for consequential
payments.

## Offline use

`uvx zapproof-mcp` runs the same tool from a bundled Base seller snapshot with no
wallet, no key and no payment. The snapshot carries no Solana index, so a Solana
address answers `rail_not_indexed` offline. Set `ZAPPROOF_API_URL` to point it at a
hosted index instead.

## Do not

- Do not describe the result as a guarantee, a certification, or proof of honesty.
- Do not claim Zapproof detects Sybil behaviour, fraud, or wash trading.
- Do not treat missing data as a negative finding.
- Do not repeat the queried address or query string in telemetry you keep.
