# The paid x402 endpoint (Base USDC)

Status: **implemented and tested, not enabled.** It switches on with environment
variables and one image change, once the owner supplies a `payTo` address and CDP
credentials. Nothing here holds a key or a wallet.

## What it is

`GET /v1/paid/score/{identifier}` — the same answer as the free `/v1/score`, but it
asks for a USDC payment on Base through x402 `exact` before answering. The free route
is unchanged.

Why charge for the same data: the x402 catalogs only index resources that answer 402.
Bazaar, x402scan and x402-list are distribution. The price is a demand filter, not a
business model — at $0.005 a lookup, revenue needs ~300k paid lookups/month to matter.

**Price is $0.005, not $0.001.** CDP charges $0.001 per on-chain settlement after the
free 1,000/month (as of 2026-11-25), so a $0.001 price would hand every later payment
to the facilitator.

## Files

| File | Role |
|---|---|
| `src/repindex/api/paid.py` | Config validation, middleware, paid route, cache suppression |
| `src/repindex/api/app.py` | Installs the route only when `ZAPPROOF_X402_ENABLED=1` |
| `tests/test_paid_endpoint.py` | 402 shape + Bazaar declaration + real-app boot test |
| `pyproject.toml` | Optional extra `paid` (x402 2.29.0, cdp-sdk 1.53.0) |

Run the paid tests with the extra installed:

```bash
uv run --extra paid pytest -q tests/test_paid_endpoint.py
```

## Environment

| Variable | Meaning |
|---|---|
| `ZAPPROOF_X402_ENABLED` | `1` installs the route; unset leaves the app exactly as before |
| `ZAPPROOF_X402_PAY_TO` | Public, **checksummed** EVM address the owner controls |
| `ZAPPROOF_X402_NETWORK` | `eip155:8453` (mainnet, default) or `eip155:84532` (rehearsal) |
| `ZAPPROOF_X402_PRICE` | Default `$0.005`; must look like `$0.005` |
| `ZAPPROOF_X402_FACILITATOR` | Optional URL override; mainnet defaults to CDP |
| `ZAPPROOF_X402_ICON_URL` | Optional catalog icon (defaults to the public repo logo) |
| `CDP_API_KEY_ID`, `CDP_API_KEY_SECRET` | Required for mainnet unless a facilitator is named |

The app refuses to start instead of starting half-configured: a missing or
non-checksummed `payTo`, an unknown network, a malformed price, or mainnet without CDP
keys all raise `PaidConfigError` at import.

## Rehearsal on Base Sepolia (no money, no signup)

1. Run locally: `ZAPPROOF_X402_ENABLED=1 ZAPPROOF_X402_PAY_TO=<checksummed> ZAPPROOF_X402_NETWORK=eip155:84532 uv run uvicorn repindex.hosted:app --port 8000`
2. `curl -i localhost:8000/v1/paid/score/0x0000000000000000000000000000000000000000`
   → `402`, a `PAYMENT-REQUIRED` header, `amount: "5000"`, testnet USDC
   `0x036CbD53842c5426634e7929541eC2318f3dCF7e`, and `extensions.bazaar`.
3. To see a settlement, pay it with a funded Sepolia payer (Circle faucet); the payer
   needs USDC only — the facilitator pays gas.

## Enabling on Base mainnet

1. **Owner:** create a CDP account, an API key (`CDP_API_KEY_ID` / `CDP_API_KEY_SECRET`),
   and send the public `payTo` address. No wallet key is stored on the server.
2. Store the secrets:
   ```bash
   printf '%s' "$CDP_KEY_ID" | gcloud secrets create cdp-x402-key-id --data-file=-
   printf '%s' "$CDP_KEY_SECRET" | gcloud secrets create cdp-x402-key-secret --data-file=-
   ```
3. Image changes (`deploy/cloudrun/Dockerfile`):
   - `uv sync --frozen --no-dev` → `uv sync --frozen --no-dev --extra paid` (both lines)
   - CMD: add `--proxy-headers --forwarded-allow-ips=*` so the 402's `resource.url`
     is `https://…`; CDP's preflight rejects an `http` resource URL.
4. `deploy/cloudrun/deploy.sh`: raise `--timeout 60` to `--timeout 300` (the
   facilitator client allows 90 s; a slow settle must not die at the edge) and add:
   ```
   @ZAPPROOF_X402_ENABLED=1@ZAPPROOF_X402_PAY_TO=<address>
   ```
   plus `--set-secrets=CDP_API_KEY_ID=cdp-x402-key-id:latest,CDP_API_KEY_SECRET=cdp-x402-key-secret:latest`.
5. Deploy. `curl -i https://…/v1/paid/score/0x0000000000000000000000000000000000000000`
   must return `402` with a base64 `PAYMENT-REQUIRED` header.
6. Validate the declaration with CDP, free and without a key:
   ```bash
   curl -X POST https://api.cdp.coinbase.com/platform/v2/x402/validate \
     -H 'Content-Type: application/json' \
     -d '{"resource":"https://<host>/v1/paid/score/0x0000000000000000000000000000000000000000","method":"GET"}'
   ```

## Making one real settlement

The test payer wallet (`scripts/payer_wallet.py status`) already holds Base USDC.
`scripts/preflight_payer.py --url <paid URL> --pay --confirm-pay` signs one EIP-3009
authorization; `--max-usdc 0.01` caps it. The facilitator submits it and pays gas.
Do this once — it is the settlement Bazaar needs — and check the `EXTENSION-RESPONSES`
header on the settle receipt for `bazaar: success|processing|rejected`.

## Registering the endpoint

| Catalog | How | Needs a settlement first? |
|---|---|---|
| x402 Bazaar (CDP) | automatic after the first settled payment through CDP; up to 15 min, sometimes hours | **yes** |
| agentic.market | sourced from Bazaar | yes, indirectly |
| x402scan | `POST https://www.x402scan.com/api/x402/registry/register {"url": "…"}`; first call returns 402 with `sign-in-with-x`; sign EIP-4361 with an unfunded identity wallet and retry with `SIGN-IN-WITH-X` | no |
| x402-list.com | `POST https://x402-list.com/api/v1/submit` with url, email, service name, description; they probe the 402 | no |

x402-list charges $1 USDC for `web.app`/`firebaseapp.com` hosts (its published list,
2026-11-25). Submit the `run.app` origin (or a custom domain), not `zapproof.web.app`.

## Limits

- The x402 SDK is Alpha (2.29.0). Both dependencies are pinned; review before bumping.
- Bazaar indexing is best-effort and listings decay without settlements.
- The paid answer is the same as the free one. Until a paid-only extra exists, the
  price buys the caller nothing but the x402 flow itself; say so if asked.
- No refunds and no disputes: a settled payment that returns an error is still settled.
- Revenue has tax and reporting consequences.
