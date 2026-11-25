# x402 guard — check Zapproof before a payment is signed

An integration example: it registers one x402 **before-payment hook**, looks up the
address the agent is about to pay, logs what the index knows, and lets the payment
proceed. It is not an SDK, not a product feature, and not a gate.

**What it does not do:** it never aborts a payment, never applies a score threshold,
never sends a payer address, never retries, and never writes to the index. Zapproof is
read-only and non-custodial; this file is twelve lines of policy-free plumbing in each
language.

## The hook, verified

Checked against the x402 source on **2026-11-24**:

| Runtime | Registration | Context you read |
|---|---|---|
| TypeScript `@x402/core` | `client.onBeforePaymentCreation(hook)` | `context.selectedRequirements.payTo`, `.network`, `.amount`, `.scheme` |
| Python `x402` | `client.on_before_payment_creation(hook)` | `context.selected_requirements.pay_to`, `.network`, `.scheme` |

The TS hook may return `{ abort: true, reason }` or nothing. The Python hook may return
an abort result or `None`. This example always returns "nothing" / `None`. The hook runs
after scheme, network, and spend-control filtering and **before** the payment payload is
created and signed.

These hooks are **not default-on**: they exist only where a developer registers one, and
legacy x402 v1 clients do not have them. If your installed SDK version names the context
fields differently, read them from its types — do not trust this file's dates.

## Files

| File | Purpose |
|---|---|
| `zapproof_guard.py` | Python reference: `check()`, `attach_guard()`, `format_evidence()` |
| `x402_guard.ts` | TypeScript reference with the same behaviour and no dependencies |
| `test_zapproof_guard.py` | Python tests (stdlib only) |
| `x402_guard.test.ts` | TypeScript tests (`node:test`, no dependencies) |

## Use it — Python

```python
import asyncio

from x402 import x402Client  # your installed client; see the x402 docs
from zapproof_guard import attach_guard, format_evidence

client = x402Client()
attach_guard(client, on_evidence=lambda answer: print(format_evidence(answer)))

# ... then pay as usual. The hook runs before each payment is signed.
```

The lookup is a single `GET {base}/v1/score/{payTo}` with a **1.5 s socket timeout**.
Any failure — timeout, DNS, TLS, 4xx/5xx, malformed JSON, an unknown status — returns
`None` and the payment continues. The hook is an observer, not a dependency.

## Use it — TypeScript

```ts
import { x402Client } from "@x402/core/client";
import { attachZapproofGuard, formatEvidence } from "./x402_guard.ts";

const client = new x402Client();
attachZapproofGuard(client, {
  onEvidence: (answer) => console.error(formatEvidence(answer)),
});
```

`AbortController` enforces the same 1.5 s timeout. Any failure returns `null` and the
hook resolves without aborting. The file declares a small structural type for the client
method it needs, so it compiles without depending on `@x402/core` in this example tree.

## What the answer means

| Answer | Meaning | What this example does |
|---|---|---|
| `status: "scored"` | Enough settled Base payments to compute a score. | Logs it. The score is one input, not an approval. |
| `status: "insufficient_evidence"` | Some record, below the scoring floor. `score` is null. | Logs it. Null is not zero. |
| `found: false` (`status: "unknown"` here) | No seller-side record in this index. | Logs it. Unknown is not bad. |
| Anything else | No usable evidence. | Logs nothing; the payment continues. |

The hosted index answers for **Base mainnet only** (`eip155:8453`). Other networks are
skipped before any request is made. Lightning is not indexed at all; the API returns
`rail_not_indexed` there and this hook never calls it.

The score is a rules-based interpretation that has not been independently validated
(blinded adjudication: 61.5%, 40/65; 83.3% excluding abstentions — the adjudicators were
language models). Treat it as one signal among several. Every answer carries
`methodology_version` and `as_of_block`; neither makes the snapshot current.

## Privacy

The request contains the payee address and nothing else: no payer address, no wallet
signature, no EIP-3009 authorization, no amount, no resource URL or query, no body, no
cookie. The public endpoint sees the source IP of any HTTP call; self-host the API
(`--base-url`) if that matters to you.

The index holds no account and writes nothing. The caller keeps the answer, or does not.

## Tests

Python (stdlib only):

```bash
python -m unittest discover -s examples/x402_guard -p "test_*.py" -v
```

TypeScript (Node 24; built-in type stripping, no `npm install`):

```bash
cd examples/x402_guard && npm test
```

Both suites cover: parsing each status, fail-open on timeout / 5xx / malformed body /
unknown status, no request for a non-address, a request that carries only `payTo`, and a
hook that returns "continue" in every case — including when the index is down and when
the logging callback throws.

## Limits worth repeating

- This does not make Zapproof part of x402. It is a file a developer chooses to copy.
- A scored address can still fail to deliver; a settlement proves payment, not service.
- An unscored address may be new, or may sell elsewhere. Absence of evidence is not
  evidence of absence.
- Do not put a score cutoff in the hook and call it our recommendation. If you want a
  gate, write your own policy, keep a human in the loop, and say that it is yours.
