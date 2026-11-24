---
name: zapproof-counterparty-check
description: Check an EVM address's observed settled-payment record before paying it, and complete the x402 1-sat payment if the hosted index answers 402.
---

# Zapproof counterparty check

Use this before sending money to an EVM address you do not control, or when a seller
asks to be trusted. The answer is observed payment evidence, not a verdict.

Base URL: `__API_URL__`

## 1. Ask for the score

```http
GET __API_URL__/v1/score/{address}
```

The address may be a 0x EVM address or a `did:ethr` / `did:pkh` identifier.

Read the response:

- `found: true` and `status: "scored"`: `score` and `signaling_likelihood` are
  present, with the evidence window and rule contributions.
- `status: "insufficient_evidence"`: the record is too thin to interpret. The score
  is `null` on purpose.
- `found: false`: nothing seller-side is indexed for this address. It may still be a
  known payer; read `role_observed` and `payer_settlements` when present.
- `422`: not a valid identifier. Do not retry without fixing the input.
- `429`: back off. Do not loop.

**`found: false` and `insufficient_evidence` are unknown, not bad.** Never treat
either as a zero score or as a reason to refuse a counterparty by itself.

## 2. If the response is 402

The endpoint may ask for 1 sat per score request through x402. While the toll is
being brought online it answers `200` without payment; if you receive `200`, use the
score and do not pay. A 402 carries a payment challenge in one of two protocol
versions:

- **x402 v2**: the challenge is the base64-encoded JSON in the `PAYMENT-REQUIRED`
  response header. Send payment in the `PAYMENT-SIGNATURE` request header.
- **x402 v1**: the challenge is the 402 response body. Send payment in the `X-PAYMENT`
  request header.

In both versions the challenge has an `accepts` array. Each entry names a `scheme`,
`network`, `asset`, `amount` (atomic units, a string), and `payTo`.

1. Choose one entry you can actually pay. Prefer the cheapest. On Base the entry is
   usually `scheme: "exact"`, `network: "eip155:8453"`, USDC
   `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`, `amount: "1000"` — 0.001 USDC,
   about 1 sat at the static USDC price. Parity with sats drifts; the `amount` in the
   challenge is the price, not this sentence.
2. Sign an EIP-3009 `TransferWithAuthorization` for exactly that `amount` and `payTo`,
   inside the challenge's validity window and with a fresh random nonce. Do not alter
   any field.
3. Build the x402 payment payload (`x402Version`, the accepted entry, and the
   `payload` with `signature` and `authorization`), base64-encode the JSON, and retry
   the **same** `GET` once with the version's payment header.
4. On `200`, use the score. On another `402` or a `5xx`, stop: do not pay twice.

If you cannot sign (no wallet, no funds, a policy cap below the price), stop and tell
your operator. The offline package `uvx zapproof-mcp` answers the same question from
a bundled seller snapshot with no wallet, no key and no payment.

## 3. Rules for using the answer

- Cite the record: `methodology_version`, the evidence window, and the disclaimer
  returned with every response.
- The score is a rules-based interpretation of observed payments; it is not
  independently validated. Blinded adjudication agreed with it about 62% of the time
  (83% excluding abstentions).
- Lightning is not indexed. `rail: "lightning"` answers `rail_not_indexed`; that is
  missing coverage, not negative evidence.
- Do not describe the result as a guarantee or as proof of honesty, and do not repeat
  the query string or address in telemetry you keep.
