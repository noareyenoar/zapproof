# ADR 0002 — Node infrastructure

- **Status:** Accepted. **No production node has been started**; a disposable regtest
  PoC ran on 2026-11-16 and passed 8/8 criteria. **Superseded in part by
  [ADR 0003](0003-ln-trust-provider.md) (owner decision, 2026-10-30).** The amendment
  permits being *one hop among many* in Lightning payments; the ban on custody and on
  a mandatory position in anyone's payment path stands. The boundaries table below is
  updated to match, and the rest of this ADR is unchanged.
- **Amended 2026-11-16:** the owner approved the on-chain fee budget and authorised a
  proof-of-concept LND node on the current Windows 10 host. The PoC is signet/regtest
  only; mainnet keys and funds stay off that host. The PoC passed 8/8 criteria on
  2026-11-16. After reviewing the evidence, the owner **declined to fund a mainnet
  channel** for now; the project stays on regtest/signet and L402. See
  `the owner decision record` Q2 and `the LN node security packet`.
- **Date:** 2026-10-17
- **Deciders:** owner (scope and budget, 2026-10-16; PoC and fee budget, 2026-11-16)
  and engineering agent
- **Related:** `the engineering protocol` Sections 1A.13 and 2, `config/thresholds.yaml`
  `node_ops`, [ADR 0004](0004-agent-payments-long-term.md)

---

## Context

The owner authorised the project to run its own Bitcoin and Lightning nodes
(prompt Section 1A.13). That authorisation reversed an earlier hard non-goal, so the
boundaries need to be written down rather than inferred.

The owner also chose the **Neutrino** backend rather than a full Bitcoin node, and set
`channel_capital_sats_max: 1000000`.

## What the nodes are for

1. **Verifying facts without trusting a third party** — the "don't trust, verify"
   posture that the rest of this project is built on.
2. **Accepting payment for the project's own L402 endpoint**, when one exists. That
   makes the project an L402 operator in its own right, which is also how the Phase C
   gate's "3 operators install the reporter" requirement gets its first guaranteed
   install and end-to-end test.
3. **Testing the receipt reporter and the receipt flow** before asking anyone else to
   install anything.

## What the nodes are NOT for, and cannot do

**A Lightning node does not expose other people's transactions.** This is the finding
that matters most, because the owner's original reason for wanting a node was to
"trace the history of agents and addresses on the network".

Lightning is onion-routed by design. A node observes only the HTLCs that traverse its
own channels. It cannot see who paid whom elsewhere, at what amount, or how often.
This is not a hardware limit that a stronger machine overcomes; it is the property the
protocol was built to have.

| Data the product needs | Where it actually comes from | Does our node help? |
|---|---|---|
| x402 settlements on Base | Base JSON-RPC (`eth_getLogs`) | No — unrelated |
| Nostr attestations and zap receipts | Nostr relays (websocket) | No — unrelated |
| L402 receipts from sellers | Sellers POST them to us (Phase C) | Only as a verifier of our own preimages |
| Bitcoin address history | A Bitcoin node **plus an indexer** (electrs/Esplora) | Yes, but Neutrino does not provide it |
| Agent history on Lightning | — | **No. Only our own traffic.** |

So: the agent-history dataset comes from **receipts sellers submit** and **Nostr
attestations**, not from node operation. This ADR records that so the mistake is not
repeated.

## Hard boundaries

| Permitted | Forbidden |
|---|---|
| Accepting payment for the project's own service | Holding, escrowing or controlling a customer's money |
| Holding **our own** funds in channels | Acting as a custodial intermediary, a sequential proxy, or the only route between two parties |
| Routing enabled, so the node is a well-connected participant — including carrying third-party payments as **one hop among many** (ADR 0003) | Being a *mandatory* position in anyone's payment path; a payment must reroute automatically when the node is unavailable |
| Using our own receipts to **test the system** | Letting node state, peers, routes or routing revenue influence any score, label or confidence value |
| — | Attribution from the fact that a payment passed through the node. Any claim that an agent used this project must rest on an identity the agent presented (a bound key, a macaroon), never on routing (ADR 0003) |
| Using our own receipts to **test the system** | Using our own receipts as **evidence about ourselves** |

The last row is a self-dealing hole that would be easy to fall into: a project that
generates receipts and then scores itself on them is marking its own homework.

## Budget, as recorded in `config/thresholds.yaml`

| Setting | Value | Status |
|---|---|---|
| `bitcoin_backend` | `neutrino` | owner decision |
| `channel_capital_sats_max` | 1,000,000 | **owner decision** |
| `onchain_fee_budget_sats_month` | 50,000 | **owner decision 2026-11-16** (was an agent proposal; `the owner decision record` Q2) |
| `min_free_disk_gb` | 50 | agent proposal (Q3) |
| `uptime_target_pct` | 99 | agent proposal |

**Why Neutrino:** an unpruned Bitcoin full node needs roughly 700 GB–1 TB and this
machine has 437 GB free on the data volume — and the owner declined to buy storage.
A pruned node would fit but cannot serve address history, which was the point. Neutrino
runs the Lightning node without the full chain. Revising this later means revisiting
the disk budget, not just a config value.

## Separation from the scoring pipeline

`src/repindex/common/rpc.py` contains no signing code and no key handling. The scoring
pipeline must never read node state. The single permitted read is a *verification
oracle* for one specific payment — for example confirming that a preimage hashes to a
stated payment hash — and that read must be recorded in the evidence for that payment,
not folded into a score.

## Why no production node has been started

Task 0.8 requires the thresholds to be set and this ADR to exist first, and both are
now true. On 2026-11-16 the owner approved the on-chain fee budget of 50,000
sats/month and authorised a proof-of-concept node on the current Windows 10 host.
That authorisation is deliberately narrow:

- the PoC runs on **signet or regtest only**, with no mainnet keys and no mainnet
  funds on the Windows host;
- the Windows host is **not** approved for a mainnet node — the production host
  remains a supported Linux machine (see `LN_NODE_SECURITY_REPORT.md` §5.4);
- opening a **mainnet** channel requires an explicit owner go-ahead. The PoC passed,
  and on 2026-11-16 the owner reviewed the evidence and declined to fund a mainnet
  channel for now; no mainnet key or channel exists.

The proof-of-concept runbook is
`the LN node security packetPOC_PLAN_WINDOWS.md`. The PoC was executed on
2026-11-16 and passed all eight criteria on regtest with Bitcoin Core `v32.1.0` and LND
`v0.21.8-beta`; evidence is in
`the LN node security packetEVIDENCE_POC_2026-11-16.md`. No mainnet key,
mainnet node, or real sat was created. Mainnet channel funding was **declined** on
2026-11-16; the 1,000,000-sat cap remains the ceiling if the decision is reopened.

## Consequences

- The Lightning node, when started, will not produce the dataset the owner originally
  hoped for. That dataset comes from Phase C's receipt ingestion.
- Running a node makes the project both a scorer and a participant. That is a
  conflict of interest in appearance if not in fact, and it is disclosed publicly in
  Phase D.
- Routing revenue is not a business line. Measured industry figures put a
  roughly $50,000 node at **-5 to +15 USD per month net**. Routing stays enabled for
  node health, nothing more.
