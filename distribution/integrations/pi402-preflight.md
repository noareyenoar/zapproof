# Preflight pattern: check a counterparty before paying a 402 challenge

**Status:** draft integration note, 2026-11-23. This document describes a pattern; it
does not claim that any project has integrated it. Zapproof has not modified, and will
not modify, another project's repository.

**Audience:** developers of agents that pay 402 endpoints, including L402 sellers built
on Aperture/lightmeter-style stacks and the Pi ecosystem's `pi-402` extension.

---

## 1. The pattern

A 402 endpoint answers with a payment challenge. The recommended order is:

1. Identify the counterparty the agent is about to pay.
2. Ask Zapproof what settled-payment evidence exists for that counterparty.
3. Apply a policy: pay, pay with a cap, or stop.
4. Only then sign and send the payment.

The point is not to approve or block a payment. The point is to make the agent ask a
second question — *what evidence exists, and what does it not prove?* — before it
irreversibly sends money.

```text
agent ──▶ 402 endpoint ──▶ challenge (invoice / x402 accepts)
  │
  ├──▶ Zapproof check (MCP tool or GET /v1/score/{identifier})
  │        │
  │        └── scored | insufficient_evidence | found:false | rail_not_indexed
  │
  └──▶ policy ──▶ pay full | pay capped | stop
```

---

## 2. What Zapproof can answer today

| Seller rail | Identifier the agent has | Today's answer | Useful? |
|---|---|---|---|
| Base x402 | EVM address (`0x…`) | a score, `insufficient_evidence`, or `found:false` | yes |
| Lightning L402 | node pubkey or Lightning address | `invalid_identifier` (P2 will change it to `rail_not_indexed`) | not yet |
| Lightning L402 | no EVM address | cannot check | no — this is a coverage gap, not negative evidence |
| MPP | operator/session id | not accepted | no |

**Consequence for a pi402-style Lightning seller.** Zapproof cannot see Lightning
settlements. If the agent only has the seller's node pubkey or Lightning address, the
honest result is "not indexed", not "good" or "bad". Do not treat the absence of a
Zapproof record as a reason to refuse a seller, and do not treat the presence of a Base
score as evidence about a Lightning endpoint.

A hybrid seller that publishes both a Base address and a Lightning endpoint can be
checked on the Base side only. The answer must say which rail it covers.

---

## 3. Availability (read this before writing code)

- The hosted API, remote MCP, `/skill.md` and `/skill.th.md` are deployed, and
  `scripts/smoke_hosted.py` passes 8/8 (2026-11-23) on
  `https://zapproof-hc75zyauja-uc.a.run.app`, the canonical `status.url`. An unknown
  address returns `found: false` with no score. Cloud Run also prints a second hostname
  (`zapproof-552007190449…`) that serves REST but returns `421` on `/mcp`; call the
  canonical host, and do not ship an integration against a `421` host.
- The offline package is on PyPI (`zapproof-mcp 0.1.2`): `uvx zapproof-mcp` starts the
  stdio server from the bundled seller snapshot; it needs no key or wallet.
- The 1-sat x402 toll is not implemented. If a request returns `200`, do not pay. If a
  future deploy returns `402`, follow `/skill.md`: pay once, retry once, never loop.

---

## 4. MCP tool or plain HTTP

### MCP (stdio, source checkout)

```json
{
  "mcpServers": {
    "zapproof": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/zapproof", "zapproof-mcp"]
    }
  }
}
```

Tool: `check_counterparty(identifier="0x…", rail="any")`.

### HTTP (hosted)

```http
GET https://zapproof-hc75zyauja-uc.a.run.app/v1/score/0x0000000000000000000000000000000000000000
```

Read `found`, `status`, `score`, `role_observed`, `evidence_window`, and the
`disclaimer`. Never read a missing `score` as zero.

### Lightning

Do not send a node pubkey or Lightning address to the API yet. It returns
`invalid_identifier` today. When the P2 change lands, it will return
`rail_not_indexed`; the integration should treat both as "no Lightning evidence".

---

## 5. Policy example

The policy is deliberately small. It encodes the failure modes this project cares
about: refusing an unknown counterparty, or treating a score as an approval.

```python
from dataclasses import dataclass

UNKNOWN_CAP_USDC = 1.0  # example, not a recommendation


@dataclass(frozen=True)
class Decision:
    action: str  # "pay" | "pay_capped" | "stop"
    cap_usdc: float | None
    reason: str


def decide(answer: dict, amount_usdc: float) -> Decision:
    status = answer.get("status")

    if status == "rail_not_indexed" or answer.get("rail") == "lightning":
        return Decision("stop", None,
                        "No evidence is indexed for this rail; this is missing coverage, "
                        "not negative evidence.")

    if "error" in answer:
        return Decision("stop", None, "The index is unavailable or the input is invalid.")

    if answer.get("found") and status == "scored":
        return Decision("pay", amount_usdc,
                        "Enough evidence to score. The score is one input, not an approval.")

    if answer.get("found") and status == "insufficient_evidence":
        return Decision("pay_capped", min(amount_usdc, UNKNOWN_CAP_USDC),
                        "Some evidence, too little to score: cap the amount and require "
                        "human approval.")

    return Decision("pay_capped", min(amount_usdc, UNKNOWN_CAP_USDC),
                    "Unknown is not bad and not zero: cap the amount and require human "
                    "approval.")
```

This is the same decision function as `examples/payer_agent.py`, which runs against the
local index today.

---

## 6. What not to do

- Do not call Zapproof *after* paying. The check exists to inform the decision.
- Do not put Zapproof in the payment path as a required hop. If Zapproof is down, the
  agent's payment must still be able to proceed under its own policy.
- Do not send a Lightning preimage, macaroon, invoice, private key, seed, or x402
  signature to Zapproof. The checker needs an identifier, nothing else.
- Do not retry a 402 more than once. Do not pay twice after a timeout.
- Do not describe a score as "safe", "verified", or a guarantee. The interpretation is
  rules-based and was not independently validated; blinded adjudication agreed about
  62% of the time (83% excluding abstentions), and the adjudicators were language
  models, not humans.
- Do not treat `found: false` or `insufficient_evidence` as a zero score.

---

## 7. What would make this a real integration

1. The hosted score returns `200` and `scripts/smoke_hosted.py` exits zero.
2. A Base x402 seller is checked before a real payment, and the agent's policy is
   exercised on `scored`, `insufficient_evidence`, and `found:false`.
3. For Lightning, the P2 identifier handling returns `rail_not_indexed`; a partner
   supplies a real L402 receipt so the evidence class in `docs/CROSS_RAIL_EVIDENCE.md`
   can be tested.
4. The partner agrees to be named. Until then, this document is a pattern, not a
   customer story.
