# ADR 0003 — Lightning trust provider architecture

- **Status:** Accepted (owner decision, 2026-10-30)
- **Deciders:** owner
- **Related:** prompt Section 1A.13, Section 2, ADR 0002,
  [ADR 0004](0004-agent-payments-long-term.md); `docs/METHODOLOGY.md`

---

## Context

The owner has decided that Zapproof will be a **Lightning trust provider**, in two
senses at once:

1. **As payee** — agents pay Zapproof through L402 for the score service.
2. **As one hop** — payments between other parties may route through Zapproof's node,
   and an agent that pays through Zapproof additionally receives a trust level.

The owner states that the legal position for this is acceptable in their jurisdiction.
That is recorded as the owner's assertion, not as a legal opinion; Phase D's
`D_LEGAL_SIGNOFF` still applies in full.

This ADR exists because the second half rests on a technical assumption that does not
hold, and the plan depends on recognising it now rather than discovering it after
months of building.

---

## The constraint that shapes everything

**Lightning is onion-routed.** A node — including a well-connected hub — observes only
the HTLCs that traverse its own channels.

| A routing node sees | A routing node does not see |
|---|---|
| that a payment passed through | who the ultimate payer is |
| amount, fee, timestamp | who the ultimate payee is |
| the adjacent hop on each side | whether the payment was commerce or self-dealing |
| fees earned | any relationship between the two parties |

So routing produces **traffic statistics**, not **counterparty evidence**. "This
payment went through our node" cannot support a claim that "this agent paid that
seller", because the node genuinely does not know.

**Consequence:** the trust level in sense (2) cannot come from routing. It can only
come from the agent **presenting an identity** at the moment of payment — a macaroon
carrying a bound key, a signed challenge, or an SDK that attaches a signature.

This is why Phase C's task C6 (cross-rail identity binding) is pulled forward into
Phase B. Without it, being a hub yields a payment count and nothing about reputation.

---

## Decision

### 1. Zapproof operates a Lightning node as one hop, not as a required intermediary

The node is permitted to route. It is a fail-open position by construction: a payment
that would have routed through it reroutes around an unavailable node, so no third
party's payment depends on Zapproof being up. That property is what makes routing
compatible with the project's long-standing rule against being a mandatory step in
someone else's payment.

### 2. Trust level is issued only against a presented identity

An agent obtains a trust level by **binding a key** (EVM address to Lightning node
pubkey or Nostr key) through the challenge-response flow specified in C6, and then
paying. The level reflects:

- the bound identity's payment evidence on the indexed rails, and
- the age and consistency of the binding itself.

**Never** the fact that a payment traversed the node.

### 3. Routing revenue never touches scoring

No channel, peer, route, fee or routing volume may influence any score, label or
confidence value. This repeats prompt Section 2 because the temptation is structural:
the project would earn money from parties it also rates.

The scoring pipeline may read node state for exactly one purpose — verifying a
specific payment a user claims to have made — and that read must be recorded as
evidence for that claim, never folded into a reputation signal.

### 4. Conflict of interest is disclosed, not denied

Zapproof would (a) rate agents and sellers, (b) earn routing fees from traffic between
them, and (c) sell a service to the same parties. Those three roles create a real
incentive to prefer traffic that routes through the project.

Handling:

- The scoring pipeline and the node are **logically separate**; nothing in the scoring
  path can read routing state.
- Routing statistics are **never an input** to any published number.
- The conflict is **stated publicly** in Phase D, in the same place the methodology is
  published.
- The independent reviewer at Gate C is asked explicitly to look for leakage from node
  operations into scores.

### 5. Privacy boundaries

- No customer personal data is stored.
- Payment preimages are used only to verify a claimed payment, and are not published.
- The node does not attempt to deanonymise counterparties and does not correlate
  payments across channels to infer relationships.

---

## What this decision does **not** claim

- It does not claim that routing detects Sybil behaviour. It does not.
- It does not claim that trust levels are a measure of honesty. They summarise a
  payment record attached to a bound key.
- It does not claim the legal position is settled. It records the owner's decision and
  defers the legal review to the gate that exists for it.

---

## Consequences

**Positive**

- A natural insertion point for identity: an agent already paying has a reason to bind
  a key.
- A small, honest revenue stream that does not depend on the score being right.
- Credibility in the Bitcoin developer community that a pure indexer would not have.

**Negative**

- Operating a node costs capital, liquidity and uptime that produce almost no income —
  measured industry figures put a ~$50k node at −$5 to +$15 per month.
- The appearance of a conflict of interest is unavoidable; only disclosure and
  separation reduce it.
- The "trust level when you pay through us" feature depends entirely on C6 landing.
  If binding proves unpopular, the routing half of this ADR delivers no reputation
  data at all.

## What would overturn this decision

1. A binding design that agents refuse to adopt, leaving routing with no attribution.
2. Legal review finding that holding routing position plus publishing scores creates an
   unacceptable exposure.
3. Evidence that routing traffic correlates with, and thereby leaks, information about
   parties who did not consent to be indexed.
