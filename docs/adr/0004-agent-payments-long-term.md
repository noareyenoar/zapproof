# ADR 0004 — Long-term direction: sats payments between agents and humans

- **Status:** Accepted as a direction (owner decision, 2026-11-16). The phases below
  are sequencing, not approved spending; each phase needs its own gate and, where it
  spends money, an explicit owner decision.
- **Deciders:** owner
- **Related:** [ADR 0002](0002-node-infrastructure.md),
  [ADR 0003](0003-ln-trust-provider.md), `docs/METHODOLOGY.md`,
  `config/thresholds.yaml` `node_ops`

---

## Context

The owner has stated the long-term goal directly: Zapproof should work toward being a
**pillar of payments between agents and humans when the payment is made in sats**.

That is broader than the current node decisions. ADR 0002 authorises a node for
verification, for accepting payment for Zapproof's own L402 service, and for testing.
ADR 0003 permits being one hop among many and defines how trust may be issued. Neither
ADR says what Zapproof is trying to become over several years. This ADR records that
direction so later node, product and partnership decisions can be judged against it.

The word **pillar** is used in the sense of dependable public infrastructure, not in
the sense of a mandatory intermediary or a custodian. The distinction matters because
the project's existing boundaries are what make the goal compatible with its ethics.

---

## Decision

Zapproof will build toward a **non-custodial sats payment layer for agent-to-human and
human-to-agent commerce**, in phases:

### Phase 0 — Proof of concept on the current host (now)

- LND on the current Windows 10 machine, **signet or regtest only**.
- No mainnet keys, no mainnet funds, no third-party channels.
- Prove the operational basics: install, wallet creation, chain sync, invoice and
  payment, backup and restore, monitoring, clean shutdown.
- **Exit gate:** every Phase 0 bullet has a recorded drill result — including SCB
  backup and restore, monitoring, and clean shutdown — and the owner reviews that
  evidence and gives an explicit go-ahead before any mainnet channel is funded. A
  passing install-and-pay run alone does not close Phase 0.

**Status 2026-11-16:** the core PoC passed 8/8 criteria on regtest — install, wallet
creation, chain sync, channel, payment, and restart — with Bitcoin Core `v32.1.0` and
LND `v0.21.8-beta`. Evidence:
`the LN node security packetEVIDENCE_POC_2026-11-16.md`. The Phase 0 drills
that followed also passed on a second disposable lab: seed-backup round-trip, SCB
restore (peer-initiated force-close, 990,629 sats swept back), a local force-close and
CSV sweep (393,601 sats), clean shutdown and restart, and a health-check script for
sync, channel, pending channels, balance floor, and disk. **Not covered:** watchtower
operation, SCB recovery with the peer permanently offline, agent-side prompt injection,
and production uptime. The owner reviewed the evidence on 2026-11-16 and **declined
mainnet funding**; the project remains in Phase 0 (regtest/signet, L402), and no
mainnet key or channel exists. A future go-ahead would need demand evidence plus the
untested controls above closed in the lab.

### Phase 1 — L402 payee in production

- LND + Neutrino on a supported Linux host; Tor for P2P; RPC/REST on localhost.
- Invoice-only macaroon for the L402 service; no admin macaroon in any web process.
- 1,000,000-sat hot-balance cap (ADR 0002), watchtower, tested SCB restore, 72-hour
  patch SLA for High advisories.
- Prove that an agent can pay for a Zapproof score through L402 end to end.
- **Exit gate:** a real external payment settles, and a restore drill passes.

### Phase 2 — One hop among many, with identity binding

- Routing enabled as a **fail-open** participant: a payment reroutes automatically if
  Zapproof is unavailable. Never a mandatory position in anyone's path.
- Trust levels are issued only against a presented identity (ADR 0003, Phase C task
  C6). Routing statistics never enter a score.
- **Exit gate:** a bound identity completes a payment and receives a trust level that
  is reproducible from the binding plus indexed payment evidence.

### Phase 3 — Agent payment sidecar

- A policy-enforcing payer sidecar for agents: budget, allowlist, BOLT11 decode,
  amount and payee verification, fee cap, and no credential in the LLM context.
- Support both directions: agents paying humans or merchants, and humans paying agents
  for work, all denominated in sats.
- **Exit gate:** prompt-injection tests fail closed; a capped autonomous payment
  succeeds without exposing a macaroon.

### Phase 4 — Implementation diversity and resilience

- Evaluate CLN + VLS and pruned Bitcoin Core as the validating-signer path; evaluate
  LDK only for embedded use cases where Zapproof controls the node code.
- Publish reliability metrics and a security-disclosure process.
- **Exit gate:** a second implementation can be operated without weakening the
  non-custody or identity rules.

### Phase 5 — Ecosystem role

- Publish the payment sidecar, receipt format and integration guidance as reusable
  public infrastructure.
- Measure success by dependability, neutrality and adoption by independent operators,
  not by routing revenue or market share.
- **Exit gate:** at least three independent operators run an integration (Phase C's
  existing market requirement is the first milestone).

The phase numbers are a sequence, not a promise of dates. A later phase does not
start merely because an earlier one shipped; it starts when its own gate is met.

---

## What "pillar" means here — and what it does not

| It means | It does **not** mean |
|---|---|
| High uptime and predictable settlement | Holding or escrowing anyone's money |
| Enough liquidity to complete ordinary payments | Being the only route between two parties |
| Standards compliance: BOLT11, BOLT12 where applicable, L402 | A proprietary payment rail or a new token |
| Fail-open routing and automatic rerouting | A mandatory chokepoint or gatekeeper |
| Published reliability and security metrics | Deanonymising counterparties |
| Open integration for independent operators | Routing volume used as a reputation signal |

The conflict-of-interest disclosure in ADR 0003 still applies in full: Zapproof would
rate agents, earn fees from payments, and sell a service to the same parties. The
answer remains separation of the scoring pipeline from node state, public disclosure,
and an independent reviewer at Gate C.

---

## Constraints inherited from earlier decisions

1. **No custody.** Zapproof never holds, escrows or controls a customer's money
   (ADR 0002).
2. **Own funds only.** Channels are funded with Zapproof's own capital, capped at
   1,000,000 sats until the owner changes the cap.
3. **No mandatory position.** Routing must remain fail-open; a payment must reroute
   automatically when Zapproof is unavailable (ADR 0002, ADR 0003).
4. **Trust requires a presented identity.** Routing cannot produce trust evidence
   (ADR 0003).
5. **Routing revenue never touches scoring** (ADR 0003).
6. **No mainnet on the Windows 10 host.** The PoC is signet/regtest only; production
   runs on a supported Linux host.
7. **Legal review still applies.** Phase D's `D_LEGAL_SIGNOFF` is unchanged; this ADR
   records a direction, not a legal opinion.

---

## How progress is measured

These are candidate metrics for phase gates, not published claims:

| Area | Metric |
|---|---|
| Reliability | Payment success rate; uptime; force-close rate |
| Recovery | SCB restore drill pass; time to restore |
| Security | Patch latency for High advisories; credential-scope violations (target: 0) |
| Agent safety | Prompt-injection tests failed closed; payments outside policy (target: 0) |
| Adoption | Independent L402 payers; bound identities; external operators |
| Liquidity | Inbound/outbound capacity; failed-payment reasons |

**Routing revenue is deliberately not a success metric.** ADR 0003 already records
that a ~$50k node earns roughly −$5 to +$15 per month net. The goal is usefulness and
dependability, not fee income.

---

## Risks

| Risk | Why it matters | Mitigation |
|---|---|---|
| Liquidity and channel management | A node that cannot route is not a pillar | Start small (1M sats), measure failure reasons, add liquidity only against evidence |
| Regulatory perimeter | "Payments between agents and humans" can look like money transmission | Phase D legal sign-off; no custody; no pooled funds; document the boundary |
| Hot-wallet and credential risk | A compromised node loses the capped balance | Small cap, scoped macaroons, localhost-only RPC, watchtower, 72-hour patching |
| AI-agent risk | An agent with a credential can be prompt-injected | Sidecar, budget, allowlist, BOLT11 amount binding, no credential in the LLM |
| Demand risk | Agents may not pay in sats at the scale hoped | Phase 1 exit gate requires a real external payment before expanding |
| Competition | Other L402/LN providers may move faster | Compete on neutrality, evidence and integration, not on lock-in |
| Protocol change | BOLT12, splicing, TRUC anchors change the ground | Track advisories and protocol drafts; keep the node upgradeable |

---

## What would overturn this decision

1. A legal opinion that operating a payment position plus publishing scores creates an
   unacceptable exposure.
2. The PoC failing in a way that cannot be fixed within the 1M-sat loss cap.
3. No external demand after Phase 1 and Phase 2 gates.
4. A security incident that exceeds the cap or destroys confidence in the recovery
   story.
5. Evidence that routing centrality would make Zapproof a de facto mandatory
   intermediary, contrary to ADR 0002.

---

## Consequences

**Positive**

- Connects the node work to a product direction instead of leaving it as an
  infrastructure side quest.
- Gives later decisions a test: does this make sats payments between agents and humans
  more dependable without creating custody or a chokepoint?
- Speaks directly to the Bitcoin/Lightning audience Zapproof wants to serve.

**Negative**

- Scope creep is a real risk; a payments business is larger than an indexer.
- Liquidity and uptime are ongoing operational costs that produce little direct
  revenue.
- The appearance of conflict of interest grows with every payment role; disclosure and
  separation must grow with it.
- "Pillar" can become an excuse for centralisation. The fail-open and non-custody
  rules are the guardrails, and they must be tested, not assumed.
