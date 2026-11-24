# ADR 0001 — Data source for x402 settlements on Base

- **Status:** Accepted, with one unresolved consequence (see below)
- **Date:** 2026-10-16
- **Deciders:** engineering agent, on measurements taken 2026-10-16/17
- **Supersedes:** none
- **Related:** `reports/source_probe.json`, `docs/protocol_notes.md`, `reports/failure_analysis_g0_3.md`

---

## Context

Phase 0 must establish that x402 settlement data on Base can be obtained at **zero
cost**, decoded correctly, and **cross-checked against a second, independent source**.
Prompt Section 6 is explicit that a second endpoint run through our own decoder is a
self-comparison and does not count.

The constraint `global.max_monthly_cost_usd: 0` eliminates any paid tier, and prompt
Section 3.5 requires stopping rather than paying.

## Options considered

| Option | Free? | Independent decode? | Completes? |
|---|---|---|---|
| Tenderly public JSON-RPC (`eth_getLogs`) | yes | no — our decoder | **yes** |
| Thirdweb public JSON-RPC | yes | no — our decoder | **yes** |
| Substreams `x402-base-pulse@v3.3.0` | yes in principle | **yes — third-party WASM** | **no** |
| Bitquery GraphQL | no — from $29/mo | yes | not exercised |
| x402scan API | no — $0.01/request | yes | not exercised |
| CDP x402 discovery | yes | n/a — no settlement data | n/a |
| `x402-omnigraph` subgraph | — | yes | no — still syncing |
| Dune | no — view-only since 2026-09-10 | yes | no |

## Decision

**Primary source: public Base JSON-RPC via `src/repindex/common/rpc.py`**, in this
order: `https://gateway.tenderly.co/public/base`, then
`https://8453.rpc.thirdweb.com`. Both keyless, both free, both verified returning
identical log sets for a pinned block range.

**Decoder: event pairing (`AuthorizationUsed` + the paired `Transfer`), never
calldata alone.** This is the ADR's most consequential choice and it was made from
evidence, not preference. Measured over 233 settlement transactions:

- only 70.4% are a plain `transferWithAuthorization` call with a `v,r,s` signature
- 18.5% use the same method with a `bytes` signature
- 5.2% are wrapped in ERC-4337 `EntryPoint`, 4.3% in `Multicall3`
- 0.9% use a selector that is not in 4byte.directory at all

Calldata decoding covered 153 of 214 settlements in a sample window. Event pairing
covered **214 of 214**, and agreed with the calldata decode on every field for the
153 where both applied. Event pairing is therefore authoritative and calldata is a
cross-check whose coverage is reported rather than assumed.

**`mainnet.base.org` is excluded.** Measured: it answers `eth_blockNumber` normally
but returns `{"code": -32011, "message": "request limit reached"}` for `eth_getLogs`
on a 1,000-block range, so it cannot serve ingestion at any volume.

**No secondary independent source is adopted, because none completes.** This is
recorded as a failure, not worked around.

## Consequences

### Positive

- Ingestion is free and reproducible on one ordinary machine.
- A 30-day backfill is projected at **30.4 hours and 11.7 GB**, measured rather than
  estimated, and fits comfortably in the `backfill_days_default` budget.
- The decode is validated two ways internally, which catches the class of error that
  matters most (reading the wrong field from the wrong event).

### Negative — the one that matters

- **Gate check G0.3 fails.** There is no second independent source at zero cost. The
  only candidate, Substreams `x402-base-pulse`, needs 21.9M blocks of store
  preparation against a 7M-block free tier, and produced no data across four attempts
  totalling about six hours. See `reports/failure_analysis_g0_3.md`.
- This is the project's **first kill criterion**. The agent does not decide what
  follows; the owner does.

### Risks accepted

- **Terms of use.** Thirdweb's terms state: *"You agree not to scrape any content
  from the Software or use any automated means to access, download or gather
  information from the Software other than such automated means which may be
  intentionally made available by the Company."* Whether a public JSON-RPC endpoint
  counts as an intentionally available automated means is an interpretation, not a
  certainty. Tenderly's Prohibited Uses list was only partially read. **Both are
  recorded as human decisions in `reports/source_probe.json` and are not resolved by
  this ADR.** If either resolves against this use, this decision is void.
- The public endpoints impose a 1 GB/IP/day response cap (Tenderly, documented) and
  an unknown cap (Thirdweb). The 30-day backfill at 11.7 GB therefore cannot run
  against a single endpoint in one day; splitting across endpoints assumes each has
  its own allowance, which is **not verified**.

## What would overturn this decision

1. The Substreams store cache completing during other work, which would make the
   independent cross-check possible and G0.3 passable.
2. A free, independent decoder appearing that does not require range-wide store
   preparation.
3. Either provider's terms being read and found to forbid this use.

## Notes

- Nothing in this decision requires an account, a key, or payment details. The one
  credential in play (thegraph.market) is free and was obtained by the owner.
- Read-only throughout. No signing code exists anywhere in the ingestion path.
