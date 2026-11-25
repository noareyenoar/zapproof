# Cross-rail evidence — draft specification

**Status:** draft for review, not implemented. Written 2026-11-19, revised 2026-11-23 and
2026-11-25 (Solana evidence class E3, based on a measured sample; still not in the index).
**Scope:** defines what Zapproof would accept as payment evidence from each rail, and
what each class does and does not prove. No ingestion code exists for L402 or MPP.
**Decision rule:** no evidence class is added to the index until a real partner supplies
receipts that can be verified against this document.

This is an internal design draft. It does not claim that any rail is supported. The
public answer for Lightning and MPP remains `rail_not_indexed` until ingestion exists,
is tested, and is measured.

---

## 1. Why this document exists

Zapproof's score is computed from settled payments on Base. The market is moving toward
at least three payment rails:

| Rail | What settles | Current Zapproof status |
|---|---|---|
| Base x402 (EIP-3009 / USDC) | an on-chain ERC-20 transfer on Base | **indexed** |
| Solana x402 (SPL USDC) | an SPL transfer fee-paid by an allowlisted facilitator | **indexed from a seven-day sample** |
| Lightning (L402) | a Lightning payment proved by a preimage and invoice | **not indexed** |
| MPP (Stripe/Tempo draft) | a metered session, settled by an operator | **not indexed** |

Without a written evidence model, three failures become likely:

1. **Rail confusion** — Base activity is presented as if it were Lightning activity.
2. **Self-report inflation** — an operator's own receipt is treated as independent proof.
3. **Identity leakage** — a payment proof that is also a bearer secret is stored or logged.

The model below exists to make each of those failures impossible to ship by accident.

---

## 2. Principles

1. **Settlement before assertion.** Only evidence that value moved, or a cryptographic
   proof that a specific invoice was paid, counts. A seller's claim about itself does not.
2. **Name the evidence class.** Every record carries `rail` and `evidence_class`. A score
   is computed per class and per identifier; classes are never silently merged.
3. **Self-reported is not proof.** Receipts signed by the payee are evidence of the
   payee's claim, not of independent demand. They may be stored, but they carry a lower
   confidence label and never alone produce a `scored` result.
4. **Unknown is not bad.** Absence of evidence yields `found: false`,
   `insufficient_evidence`, or `rail_not_indexed` — never a zero score.
5. **Rail labels are explicit.** Base evidence is never relabelled as Lightning, and
   Lightning evidence is never relabelled as Base. A mixed-rail answer is not an
   aggregate without a verified identity binding.
6. **Attribution never comes from routing.** A node sees only the HTLCs through its own
   channels. Payment passing through an operator says nothing about who paid whom.
7. **Proof secrets are not stored.** A Lightning preimage is a bearer secret. Verify it,
   hash it, discard it. Never write it to logs, telemetry, or the evidence table.
8. **Idempotency is per rail.** Every class has a natural key; re-ingesting the same
   payment must not create a second settlement.
9. **Privacy minimisation.** Store what verification needs and no more. Do not store raw
   payer identifiers when a hash or a derived key is sufficient for the stated purpose.

---

## 3. Evidence classes

### 3.1 E0 — Base x402 settlement (implemented)

**Source.** Base L2 logs for the USDC contract, decoded as EIP-3009
`AuthorizationUsed` plus the corresponding `Transfer`, inside the finality window.

**Fields.**

| Field | Meaning |
|---|---|
| `chain_id` | `8453` for Base mainnet |
| `token` | USDC contract address |
| `tx_hash`, `log_index` | location of the settlement on chain |
| `block_number`, `block_time` | when it settled |
| `from`, `to` | payer and payee addresses |
| `value` | atomic units (6 decimals for USDC) |
| `authorization_nonce` | EIP-3009 nonce, for replay detection |

**Proves.** A transfer of a specific amount from `from` to `to` was authorised and
settled on Base at a specific block. The payer controlled the key that signed the
authorisation at that moment (subject to the token contract's rules).

**Does not prove.** That the payer is independent of the payee; that a service was
delivered; that the payee is the operator of the endpoint the agent intended to pay
(the x402 request/response envelope is off-chain); that two addresses belong to
different entities; that the transfer is commerce rather than a transfer between
related wallets.

**Confidence.** On-chain and reproducible from a node or an archive provider. Counts
are cross-checked on three windows against a second Base RPC provider (Gate A GA.3),
which verifies decoding and counting but not chain completeness; every published
number still says it is one decoder's output.

**Replay key.** `(chain_id, tx_hash, log_index)`.

**Status.** Implemented and used for the current score.

---

### 3.2 E1 — Lightning L402 receipt (not implemented)

**Artifacts.**

| Artifact | What it contains | Who can verify it |
|---|---|---|
| BOLT11 invoice | payment hash, amount, payee node pubkey, expiry, description | anyone holding the invoice |
| Macaroon | service identifier, payment hash, caveats, expiry | the service, and anyone with the root key |
| Preimage | 32 bytes whose SHA-256 equals the payment hash | anyone holding the invoice |
| Payer signature (optional) | payer key signature over the invoice or payment hash | anyone with the payer pubkey |
| Settlement metadata (optional) | node-reported settled time and amount | the payee node, or a third party it trusts |

**What a receipt must contain to be accepted.** `rail="lightning"`, `evidence_class="l402_receipt"`,
the invoice (or its hash and payee pubkey), the preimage presented at verification time,
the amount actually paid, and the payee node pubkey. The verifier MUST check:

1. `SHA256(preimage) == payment_hash` from the invoice.
2. The invoice's payee node pubkey matches the claimed payee.
3. The paid amount matches the invoice amount (no partial-payment assumption).
4. The invoice has not expired at the claimed settlement time.
5. The receipt has not been seen before: key `(rail, payment_hash)`.
6. The preimage is discarded after verification; only its hash and the verification
   result are retained.

**Proves.** The presenter knew the preimage for that invoice, so the invoice was paid
(or the preimage was shared by someone who paid it). It binds an amount and a payee
node pubkey to a payment hash.

**Does not prove.**

- That the service was delivered. L402 gates access on payment; it does not attest to
  the response.
- That the payer is independent of the payee. A payee can pay its own invoice.
- That the payer is the agent that will consume the service. Without a payer signature,
  the preimage is a bearer secret and can be transferred.
- That the node pubkey maps to a legal entity, a domain, or a Base address. A node
  operator can run many pubkeys, and one entity can run many nodes.
- That the amount has a stable fiat value.

**Self-reported part.** The payee's server reports that it issued and honoured the
receipt. The cryptographic check proves payment of the invoice, not delivery of the
service or the identity of the payer.

**Confidence.** Strong for "this invoice was paid"; weak for "this seller has
independent customers"; silent on service quality.

**Privacy.** The preimage is a bearer secret. The invoice and payment hash can link a
payer's purchases across services if reused. Store the payment hash only if needed for
dispute handling, and disclose the retention period.

**Status.** Not implemented. L402 has multiple implementations (Aperture, lightmeter,
others); a version field is required before ingestion. No partner has supplied a real
receipt yet.

---

### 3.3 E2 — MPP session (not implemented; watch only)

**Source.** The Machine Payments Protocol (MPP) draft (`draft-ryan-httpauth-payment`,
Stripe/Tempo lineage; `https://mpp.dev/overview`, checked 2026-11-24). A session has an
identifier, a meter, unit prices, and a settlement reference.

**Fields (proposed).** `operator_id`, `session_id`, `meter_version`, `units`,
`unit_price`, `currency`, `settlement_ref`, `receipt_signature`, `settled_at`.

**Proves.** The operator attests that a session consumed a number of units and that a
settlement reference exists. If the settlement reference is independently resolvable at
the payment provider, it may prove that funds moved.

**Does not prove.** That the operator's meter is honest; that the payer is independent;
that the service was delivered; that the session identifiers are not shared or resold.
MPP is an IETF draft, so field names and settlement semantics may change.

**Confidence.** Operator-attested, not trustless. Treat as `self_reported` until an
independent settlement reference is verified with the payment provider.

**Replay key.** `(operator_id, session_id, settlement_ref)`.

**Status.** Watch only. Do not implement in the 30-day plan. Add a class only after the
draft stabilises and a partner supplies verifiable receipts.

---

### 3.4 E3 — Solana x402 settlement (indexed 2026-11-25 from a seven-day sample)

**Why this class needs a different key.** Solana has no protocol-level event that says
"x402". The `exact` scheme is a client-signed SPL `TransferChecked` submitted by a
facilitator that pays the network fee. A memo may be present, but anyone can write the
same bytes, so a memo is not evidence. The only usable discriminator measured so far is
**the fee payer**: a transfer counts as x402 evidence only when the transaction's fee
payer is a known x402 facilitator address.

**Source.** `getSignaturesForAddress` on each allowlisted facilitator, then
`getTransaction` (`jsonParsed`, `maxSupportedTransactionVersion: 0`). The payer and payee
are derived from `preTokenBalances` / `postTokenBalances` for the USDC mint
(`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`, 6 decimals): a positive delta is the
payee, the largest negative delta is the payer. Balance deltas were chosen over
instruction parsing because they also account for Token-2022 transfer fees, address
lookup tables and inner instructions.

**Fields.** `signature`, `account_index`, `slot`, `block_time`, `mint`, `decimals`,
`raw_amount`, `payer`, `payee`, `fee_payer`, `facilitator` (name from the public list).

**Proves.** That a USDC transfer settled in a transaction fee-paid by a known x402
facilitator. It is a strong indicator, not a protocol fact: the facilitator could be
used for a non-x402 transfer, and the list can be incomplete.

**Does not prove.** Payer independence, delivery, or that the transaction is commerce.
The allowlist is a classifier with a publication date, not a consensus rule.

**Replay key.** `(signature, account_index)`.

**Status.** Indexed from a seven-day observation window (2026-11-18 to 2026-11-25) and
served by the API and MCP from `data/solana.db`, a separate database that is never
merged with the Base snapshot. The measured window holds 40,238 settlements from 133
sellers and 186 payers; 13 sellers have enough evidence to score and 120 answer
`insufficient_evidence`. Every Solana answer carries the window and the allowlist date.
The facilitator list (`tmp/solana/allowlist.json`, 12 addresses, fetched 2026-11-25) is
versioned by date and must be refreshed before each build; the bank of evidence does
not grow on its own.

---

## 4. Identifier model

| Identifier | Form | Current behaviour | Required behaviour |
|---|---|---|---|
| EVM address | `0x` + 40 hex | scored from Base evidence | unchanged |
| EVM DID | `did:ethr:…`, `did:pkh:…` | resolved to an address | unchanged |
| Lightning node pubkey | 33-byte compressed pubkey | `rail_not_indexed`, no score (implemented 2026-11-25) | unchanged |
| Lightning address | `user@domain` | `rail_not_indexed`, no score; no LNURL resolution (implemented 2026-11-25) | unchanged |
| MPP operator | `mpp:<id>` | `rail_not_indexed`, no score (implemented 2026-11-25) | unchanged |
| Solana address | base58, 32 bytes | answered from the separate Solana sample database (implemented 2026-11-25); `rail_not_indexed` when the index is absent | a Solana sample score with its window; never Base evidence |

**No cross-rail aggregation without a verified binding.** If an entity presents both a
Base address and a Lightning node pubkey, the two records stay separate until an
identity binding proves control of both keys. Even then, the answer should show each
rail's evidence separately; a single blended score hides which rail the evidence came
from.

---

## 5. Ingestion contract (future)

Any future ingestion path MUST:

1. Verify the evidence according to its class before writing.
2. Write an append-only record with `rail`, `evidence_class`, `confidence`, and the
   natural key.
3. Enforce the natural key with a unique constraint, so replay is unrepresentable.
4. Store the verification result, not the bearer secret.
5. Record the verifier version and the source (node, provider, partner).
6. Refuse a record whose rail label does not match its verifier.
7. Never join records across rails by address, name, or timing alone.
8. Expose the limitation in the API answer: a score built from one rail says so.

A schema sketch:

```text
evidence(
  rail              TEXT NOT NULL,          -- 'base' | 'lightning' | 'mpp'
  evidence_class    TEXT NOT NULL,          -- 'eip3009_settlement' | 'l402_receipt' | 'mpp_session'
  natural_key       TEXT NOT NULL,          -- canonical per class
  payer_ref         TEXT,                   -- address, pubkey, or null
  payee_ref         TEXT NOT NULL,
  amount_atomic     INTEGER NOT NULL,
  asset             TEXT NOT NULL,
  settled_at        INTEGER NOT NULL,
  confidence        TEXT NOT NULL,          -- 'onchain' | 'cryptographic' | 'operator_attested'
  verifier_version  TEXT NOT NULL,
  source            TEXT NOT NULL,
  UNIQUE (rail, evidence_class, natural_key)
);
```

The current Base schema is separate and predates this sketch; a future migration would
map it into E0 without changing the score semantics.

---

## 6. Open questions

1. **Payer proof.** Can a payer prove it paid an invoice without revealing the preimage
   to Zapproof? A signature over the invoice plus a payer pubkey is the candidate, but
   it proves control of a key, not independence from the payee.
2. **Delivery proof.** What would prove that a paid service was delivered? A payee-signed
   response hash plus a payer acknowledgement is still self-reported. Without a trusted
   third party, delivery may be unprovable from payment data alone.
3. **MPP settlement.** Can a settlement reference be verified with Stripe/Tempo without
   the operator's cooperation? If not, MPP stays operator-attested.
4. **Retention.** How long should payment hashes be kept for dispute handling, and what
   is the legal basis in the owner's jurisdiction?
5. **Identity binding.** What proof is sufficient to link a Base address to a Lightning
   node pubkey — a signed message from both keys, a DID document, or an on-chain
   attestation? The current binding app covers EVM and Nostr, not Lightning.
6. **Cross-rail Sybil.** A fresh key on each rail is cheap. Cross-rail evidence does not
   solve the `fan_in_fresh` limit; it may make it worse by adding more identities.

---

## 7. Non-goals

- Building an L402 payee or payer implementation.
- Running a Lightning node, channels, or a watchtower.
- Custodying funds or holding invoices on behalf of either party.
- Inferring attribution from routing.
- Scoring a rail before a verifier exists and is tested against real receipts.
- Publishing a cross-rail score that blends classes without showing the split.

---

## 8. What changes when this is implemented

Until then, the public contract stays:

- `rail="lightning"` → `status: "rail_not_indexed"`, no score.
- A Lightning identifier (33-byte node pubkey or `user@domain`) → `200` with
  `status: "rail_not_indexed"`, not `invalid_identifier`. Implemented 2026-11-25;
  no LNURL/DNS resolution happens on lookup.
- `mpp:<id>` → `200` with `status: "rail_not_indexed"`, no score. Implemented
  2026-11-25; no claim of MPP support.
- A Solana address is **not classified yet** and returns `422`. No Solana answer is
  served by the API; the sample is a report, not an endpoint.
- Every answer keeps "not independently validated" and the 62%/83% caveat.

The first implementation step is not code. It is a partner who can supply one real
L402 receipt and one real MPP session, so the verifier can be tested against evidence
rather than a specification.
