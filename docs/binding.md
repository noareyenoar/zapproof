# Identity binding

A binding says: *this EVM address and this Nostr public key belong to the same
operator*, proven by one signature from each over a challenge the server issued.

It exists because of ADR 0003. Lightning onion routing means a node sees amounts, not
parties, so "pay through Zapproof and receive a trust level" can never rest on the
payment having passed through our node. It can only rest on the agent presenting an
identity. This document describes that identity claim and its limits.

## What it proves, and what it does not

Proves:

- the holder of the EVM private key and the holder of the Nostr private key both
  agreed to the same challenge, at the same time;
- the challenge came from the service and had not been used before.

Does **not** prove:

- that either key controls the payments the index scores — that link does not exist
  yet, and building it is Phase C work;
- that the operator is a distinct legal person, or that the Nostr key belongs to the
  same human as the EVM key beyond their cooperating at binding time;
- anything about the reputation of the key. The index scores sellers from settled
  payments; a binding is an identity claim, not evidence of conduct.

The score API does not read this database. A binding changes no score today.

## Flow

1. `POST /v1/bindings/challenge` with `{evm_address, nostr_pubkey}`.
   The service generates a 32-byte nonce, stores the challenge, and returns it,
   including `message`: the exact bytes both keys must sign (hex-encoded), so clients
   do not re-implement canonicalisation.
2. Both keys sign `message`:
   - EVM: EIP-191 `personal_sign` over the raw bytes;
   - Nostr: BIP-340 Schnorr over `sha256(message)`.
3. `POST /v1/bindings` with `{nonce, evm_signature, nostr_signature}`.
   The service loads the stored challenge, checks time, checks the address is not
   already bound, verifies both signatures, then consumes the nonce. A failed attempt
   does not burn the challenge.
4. `POST /v1/bindings/rotate` with `{evm_address, new_nostr_pubkey, nostr_signature}`
   signed by the **current** Nostr key.
5. `POST /v1/bindings/revoke` with `{evm_address, by, signature}` where `by` is `evm`
   or `nostr` and the named key signs the revocation message.
6. `GET /v1/bindings/{evm_address}` returns the binding and its event history.

### Message formats

All messages are canonical JSON: sorted keys, no whitespace, UTF-8. The field `domain`
is `zapproof.binding.v0` in every message, and each purpose carries a distinct shape:

| purpose | fields |
| --- | --- |
| challenge | `domain, evm, expires_at, issued_at, nonce, nostr` |
| revocation | `by, created_at, domain, evm, kind: "revocation", nostr` |
| rotation | `domain, evm, kind: "rotation", new_nostr, old_nostr` |

`kind` keeps a signature collected for a challenge from being replayed as a
revocation; the domain keeps a signature collected for another application from being
replayed here.

### Time

Challenges expire after 600 seconds by default. A challenge issued in the future by
more than 120 seconds is refused, which tolerates modest clock skew without allowing
a far-future challenge to sit unused for months.

## Storage

`data/bindings.db`, separate from `data/repindex.db`.

- `challenges` — nonce, claimed keys, issue/expiry/consumption times.
- `bindings` — current binding per EVM address, both signatures, revocation state.
- `binding_events` — append-only `created` / `recreated` / `rotated` / `revoked`
  events.

The store is writable by design. It lives in its own app
(`uvicorn repindex.binding.api:app --port 8001`) and is never imported by the score
API, which remains read-only and is proven so by GB.4.

## Attacks and the answer

| attack | answer |
| --- | --- |
| replay a captured challenge | nonce is single-use in the store (conditional `UPDATE`) |
| fabricate a challenge offline | only stored nonces verify; unknown nonce is `challenge_not_issued` |
| sign for another application | domain separation in every message |
| present one key and claim both | both signatures are required and each is checked |
| use a signature from another key | recovered address / pubkey must match the challenge |
| revoke without holding a key | revocation requires a signature from the side named in `by` |
| revoke twice | second revocation is refused |
| rotate without the current key | rotation is signed by the current Nostr key |
| race two verifications | conditional nonce update; the loser gets `already_bound` |

## Recovery

- Lost Nostr key: revoke with the EVM key, then bind again with a fresh challenge.
  The audit trail keeps the old binding's history.
- Lost EVM key: revoke with the Nostr key. Rebinding needs a new EVM address; there is
  no EVM-key rotation in v0.

## Running it

```bash
uv run uvicorn repindex.binding.api:app --host 127.0.0.1 --port 8001
uv run python scripts/binding_e2e.py     # writes reports/binding_e2e.json
uv run pytest tests/test_binding.py -q
```

`BINDING_DB` overrides the database path. The app is not part of the score API's
deployment and has no TLS, no rate limiting and no authentication of its own; run it
behind a proxy before exposing it.

## Known gaps

- Nothing consults a binding yet. Phase C has to decide how a bound identity affects
  what the score API reports; this version deliberately changes no score.
- Bindings are key-to-key. Lightning addresses (`name@domain`) and DIDs are not bound;
  the EVM address takes the DID forms the score API accepts only after normalisation.
- The binding app has no rate limiting. Its write endpoints are the only place in the
  project that persist caller input, and they are not deployed.
