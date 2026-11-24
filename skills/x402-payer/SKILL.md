---
name: x402-payer
description: Pay an x402 resource safely from a dedicated Base wallet. Use when an agent receives a 402 challenge and must decide whether to pay, sign one EIP-3009 authorization, retry once, and verify settlement. Includes the wallet and policy rules that keep a hot payer from draining itself.
---

# x402 payer — a safe, minimal payment loop

This skill is free to copy. It teaches an agent to pay an x402 resource on Base with
USDC, using a dedicated hot wallet, a hard cap, and one retry. It works without
Zapproof; the optional counterparty check is one input, not a requirement.

## Safety contract (read first)

1. Use a **dedicated** wallet. Never the operator's main wallet, never a wallet that
   holds more than you are willing to lose.
2. Fund it with **USDC on Base only** (`eip155:8453`, token
   `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`). USDC on another chain is a
   different token at that address; do not send it.
3. **No ETH is required** for the `exact` scheme: the facilitator submits the
   transfer. Keep ETH only if you also make direct transfers.
4. Set a per-call cap and a daily cap. Refuse anything above either.
5. Keep an allowlist of payees you have decided to trust, and require a human for a
   new payee above a small amount.
6. The private key never goes in a prompt, a log, a commit, an env var printed by a
   script, or a chat message. Store it in a file readable only by the agent's user.
7. Dry-run first. Sign only after the challenge fields match what you expect.
8. If anything is unclear, **stop and report**. Paying twice is worse than not paying.

## 1. Create the payer wallet

Use an ordinary EOA. EIP-3009 authorizations are signed by the token holder, so a
smart-contract wallet is not a drop-in replacement.

```python
from eth_account import Account
account = Account.create()          # address + key
# save account.key.hex() to a 0600 file outside the repository; print only account.address
```

A ready-made helper lives at `scripts/payer_wallet.py` in this repository:

```bash
uv run python scripts/payer_wallet.py create   # prints the address, never the key
uv run python scripts/payer_wallet.py status   # USDC and ETH balance on Base
```

Fund the printed address with a small amount of USDC on Base (about $1–5 is enough for
hundreds of 0.001 USDC calls). Wait for confirmation, then check `status`.

## 2. Request the resource and read the 402

Send the request with no payment. A paid resource answers `402` with a challenge in one
of two places:

- **x402 v2:** base64-encoded JSON in the `PAYMENT-REQUIRED` response header.
- **x402 v1:** the JSON response body.

Both contain an `accepts` array. Each entry has `scheme`, `network`, `asset`, `amount`
(atomic units, a string), `payTo`, `maxTimeoutSeconds`, and `extra`.

```python
import base64, json

def parse_challenge(response):
    header = response.headers.get("payment-required")
    if header:
        return json.loads(base64.b64decode(header)), 2
    body = response.json()
    return body, body.get("x402Version", 1)
```

## 3. Choose one acceptable entry

Accept an entry only if all of these are true:

- `network == "eip155:8453"` (Base mainnet);
- `asset` is the Base USDC contract above;
- `scheme == "exact"` (skip `upto` unless you understand the ceiling);
- `extra.assetTransferMethod` is `eip3009` or absent (the EVM exact default);
- `extra.paymentFlow` is absent, `authorization`, or another flow you explicitly
  support; skip `upfront` and `escrow` if you do not;
- `int(amount) <= your per-call cap`.

Choose the cheapest acceptable entry. Do not alter any field afterward.

## 4. Check the counterparty before paying (recommended)

Before signing, ask what settled-payment evidence exists for `payTo`:

```http
GET https://zapproof-hc75zyauja-uc.a.run.app/v1/score/{payTo}
```

Read `status`:

| status | Meaning | Reasonable action |
|---|---|---|
| `scored` | Enough settled-payment evidence to interpret | Score is one input; cap still applies |
| `insufficient_evidence` | Some record, too thin to score | Cap the amount; human above the cap |
| `found: false` | No seller-side record indexed | Cap the amount; human above the cap |
| `422` | Invalid identifier | Do not pay; fix the input |
| anything else | The index did not answer | Do not treat it as a score; stop or fall back to your own allowlist |

`insufficient_evidence` and `found: false` are **unknown, not bad**. Never render them as
zero, and never treat a score as an approval to pay.

## 5. Sign one EIP-3009 authorization

Sign exactly the accepted amount and payee, with a fresh random 32-byte nonce and a
short validity window.

```python
import secrets, time
from eth_account import Account
from eth_account.messages import encode_typed_data

TYPES = {
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ]
}
DOMAIN = {
    "name": "USD Coin",
    "version": "2",
    "chainId": 8453,
    "verifyingContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
}

now = int(time.time())
authorization = {
    "from": account.address,
    "to": accept["payTo"],
    "value": str(accept["amount"]),
    "validAfter": str(now - 60),
    "validBefore": str(now + int(accept["maxTimeoutSeconds"])),
    "nonce": "0x" + secrets.token_hex(32),
}
message = {
    "from": authorization["from"],
    "to": authorization["to"],
    "value": int(authorization["value"]),
    "validAfter": int(authorization["validAfter"]),
    "validBefore": int(authorization["validBefore"]),
    "nonce": bytes.fromhex(authorization["nonce"][2:]),
}
signed = Account.sign_typed_data(
    key, domain_data=DOMAIN, message_types=TYPES, message_data=message
)
signature = signed.signature.hex()
if not signature.startswith("0x"):
    signature = "0x" + signature      # eth-account 0.14 / hexbytes 1.x omit the prefix
payload = {
    "x402Version": version,
    "resource": challenge.get("resource", {}),
    "accepted": accept,
    "payload": {
        "signature": signature,
        "authorization": authorization,
    },
    "extensions": challenge.get("extensions", {}),
}
```

**Send the signature 0x-prefixed, 132 characters.** This is the failure that cost us a
real retry: `HexBytes.hex()` in eth-account 0.14 returns 130 hex characters with no
`0x`, and a facilitator rejects that as an invalid signature even though the ECDSA
signature itself is correct. If the seller echoes `invalid signature` or returns `402`
again, check the prefix before you touch the domain.

The domain values (`name`, `version`, `chainId`, `verifyingContract`) must match the
token contract. `USD Coin`/`2` is correct for Base USDC; do not guess for another token.

## 6. Retry once, with the right header

```python
import base64, json
header_value = base64.b64encode(json.dumps(payload).encode()).decode()
if version == 2:
    headers["PAYMENT-SIGNATURE"] = header_value
else:
    headers["X-PAYMENT"] = header_value
retry = client.request(method, url, headers=headers, json=body)
```

- Retry the **same** request exactly once.
- On `200`, read the result and the `PAYMENT-RESPONSE` header (settlement receipt).
- On another `402`, a `5xx`, or a timeout: **stop**. Do not sign a second time.
- Record the transaction hash from the receipt. If there is no hash but the resource
  was served, report that gap; do not claim settlement you cannot show.

## 7. Failure table

| Symptom | Likely cause | Action |
|---|---|---|
| `402` again after payment | Wrong amount/payee, expired authorization, nonce reuse | Stop; compare signed fields to the challenge |
| `insufficient_funds` | No USDC on Base at the payer address | Fund; do not retry blindly |
| `invalid signature` | Wrong EIP-712 domain, changed accepted entry, or signature missing `0x` | Re-sign from the original challenge; send 0x-prefixed 132-char hex |
| `5xx` | Seller or facilitator failure | Stop; retry the whole flow later, not the payment |
| Timeout after sending | Unknown settlement state | Check the receipt/transaction before any retry |

## 8. What this loop does not prove

- That the seller is honest, competent, or will deliver.
- That the price is fair.
- That a `scored` result is accurate. It is a rules-based interpretation of observed
  payments, not an audit.
- That a payment settled just because the resource answered. Verify the receipt.

## 9. Reference implementation

`scripts/preflight_payer.py` implements this loop end to end: challenge parsing, entry
selection, the optional Zapproof check, a pay / pay_capped / stop policy, and signing
behind `--pay --confirm-pay`. The default is a dry run. Read it before you copy it, and
replace the policy numbers with your own.
