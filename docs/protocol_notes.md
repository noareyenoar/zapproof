# Protocol notes — x402 settlement on Base

**Method:** every fact below was measured from this machine against Base mainnet on
2026-10-16. Nothing is copied from a document, a blog post, or the project's own
Section 9 hints without being re-checked on chain. Where a hint turned out to be
wrong, it says so.

**Reproduce:**

```sh
uv run python scripts/sample_settlements.py --blocks 120
uv run python -c "from repindex.common.rpc import get_code; print(get_code('0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'))"
```

---

## 1. Chain constants

| Item | Measured value | How |
|---|---|---|
| Block time | **2.000 s exactly** | 2,000 blocks spanned 4,000 s of block timestamps |
| Blocks per day | 43,200 | derived |
| Blocks per hour | 1,800 | derived — this is the gate's cross-check window |

Block time being exactly 2.000 s is convenient but not assumed anywhere in code:
windows are expressed in blocks and converted through `SECONDS_PER_BLOCK`.

## 2. Contracts

| Contract | Address | Check |
|---|---|---|
| USDC | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | `symbol()` → `"USDC"`, `decimals()` → `6`, code present |
| Permit2 proxy | `0x402085c248eea27d92e8b30b2c58ed07f9e20001` | code present (2,913 bytes) |
| FacilitatorRegistry | `0x67C75c4FD5BbbF5f6286A1874fe2d7dF0024Ebe8` | code present, `owner()` returns a real address — see the caveat in section 6 |

The USDC address from the project's Section 9 hints is **confirmed**. The Permit2
proxy address is **confirmed to exist**; its importance is a separate matter,
addressed in section 5.

## 3. Events

Observed directly in live USDC logs and resolved against 4byte.directory:

| topic0 | Signature | Observed rate |
|---|---|---|
| `0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef` | `Transfer(address,address,uint256)` | ~105 per block |
| `0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925` | `Approval(address,address,uint256)` | baseline |
| `0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5` | `AuthorizationUsed(address,bytes32)` | ~2–3 per block |

### The constraint that shapes the decoder

`AuthorizationUsed` carries **three topics and empty data**:

```
topics[0] = 0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5
topics[1] = the authorizer (the payer), left-padded
topics[2] = the nonce
data      = 0x          <-- EMPTY. The amount is not here.
```

**The amount must come from the paired `Transfer` in the same transaction.** This is
a property of the token contract, not a decoding preference. Any decoder that tries
to read an amount from `AuthorizationUsed` alone is wrong.

## 4. A second decode path, and what it proves

Because the amount is absent from the event, the settlement is also fully described
by the `transferWithAuthorization` calldata. Decoding both ways over the same
transactions gives a genuine internal cross-check. For one such transaction,
`0x465e38c3c67efd96c598e1fec9937cf6708c627c5b4a081e404e3cc282148f64`:

| Field | From calldata | From events | Agree |
|---|---|---|---|
| payer | `0x6a0c7baf60395a83f2bba2712c4ee5e8d48fc984` | `AuthorizationUsed.authorizer` | yes |
| seller | `0x96368628b679a459fe69815bacca5cb0232ac397` | `Transfer.to` | yes |
| amount | `10000` | `Transfer` value | yes |
| nonce | `0xcef02b51cfe9…2036c2` | `AuthorizationUsed` topic 2 | yes |

Over a 120-block window: **214 settlements decoded from events (all of them), 153
decoded from calldata, and all 153 agreed with the event decode on every field.**
There were zero field-level disagreements. The 61 that calldata could not decode are
explained in the next section — they are not errors.

## 5. Not every settlement is a direct `transferWithAuthorization` call

Measured over the **named window 52,680,019–52,680,138** (120 blocks), containing
214 `AuthorizationUsed` events across 212 distinct transactions. Every figure below
is reproducible from that window alone.

| Selector | Count | Share | Resolved signature |
|---|---|---|---|
| `0xe3ee160e` | 153 | 72.2% | `transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)` |
| `0xcf092995` | 30 | 14.2% | `transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,bytes)` — same method, **`bytes` signature encoding instead of `v,r,s`** |
| `0x1fad948c` | 11 | 5.2% | `handleOps(...)` — **ERC-4337 EntryPoint**, the settlement is nested inside a user operation |
| `0x82ad56cb` | 11 | 5.2% | `aggregate3((address,bool,bytes)[])` — **Multicall3**, nested |
| `0x9aefaff8` | 2 | 0.9% | **not in 4byte.directory — unresolved** |
| `0xcd4ea2bd` | 2 | 0.9% | **not in 4byte.directory — unresolved** |
| `0xb929d7f9` | 1 | 0.5% | `charge((address,address,address,address,uint120,uint48,uint48,uint48,uint16,uint16,address,uint256),uint256,address,bytes,bytes,uint16,address)` |
| `0xcccbb34c` | 1 | 0.5% | `batchTransferWithAuthorization(...)` — a batch settlement path |
| `0xf5f5065b` | 1 | 0.5% | **not in 4byte.directory — unresolved** |

> **Correction, 2026-10-17.** An earlier version of this table reported percentages
> over an unnamed 233-transaction window (70.4% / 18.5% / 5.2% / 4.3% / 0.4% / 0.4%
> / 0.9%) and listed only seven selectors. Independent review could not reproduce it,
> because the window was never stated. Re-measured above over a window this document
> names, which also surfaced three selectors the earlier table omitted. The earlier
> figures were not wrong so much as unreproducible, which for an evidence table is
> the same problem.

**This is the single most important design finding in Phase 0.**

Decoding from calldata requires handling every wrapper — two signature encodings,
ERC-4337, Multicall3, a batch method, and three unresolved selectors. Decoding by
pairing events requires handling none of them: `AuthorizationUsed` + `Transfer`
appear regardless of how the transaction was constructed.

**Decision: the event-pairing decoder is authoritative. Calldata decoding is a
cross-check that applies to the subset where it is possible, and its coverage is
reported rather than assumed.**

Two consequences worth stating plainly:

- The project's Section 9 hint about a **Permit2 `Settled` event being central is
  not supported here.** Direct EIP-3009 authorizations dominate the observed
  population. The Permit2 proxy emitted roughly one log per 1,000 blocks in a
  separate check, which is negligible against ~2–3 authorizations per block.
- **Three selectors — 5 of 212 transactions, 2.36% — are not in 4byte.directory at
  all.** This is above gate check G0.4's 2% cap for unresolved mechanisms. The
  settlements themselves decode fine, because the event decoder does not need the
  selector; what is unresolved is *how the call was constructed*, not *what was
  paid*. Whether that counts against G0.4's cap is a judgement the gate now makes
  explicitly rather than by construction — see `reports/gate_0.md`.

## 6. The FacilitatorRegistry is not a usable allowlist

Prompt Section 9 does not mention this contract; reconnaissance surfaced it as an
on-chain facilitator allowlist and proposed building the facilitator gate on it.
**Measurement does not support that:**

| Question | Measured |
|---|---|
| Deployed when? | Block 44,152,583 = **2026-04-02** |
| Logs in the last 500,000 blocks? | **Zero.** Fully covered, not sampled |
| Logs ever? | Only `OwnershipTransferred` in its deployment window |
| `isFacilitator(relayer)` for known busy relayers? | **false** for all three probed, including one with 248 settlements in a 1,000-block window |

The `x402-base-pulse` package's own documentation independently confirms this,
describing its registry-only v3.0.0 as *"Deprecated: produces empty output in
practice"* because the registry is *"sparsely populated (only Meridian as of
2026-05)"*. The `x402-omnigraph` subgraph gated on this registry alone and is still
marked "Syncing". Two independent lines of evidence agree with the measurement here.

## 7. Relayer concentration

Over the **named window 52,680,019–52,680,138**: 214 settlements, **48 distinct
relayers**, the top five accounting for **26.6%**, and the busiest single relayer
broadcasting 18.

> **Correction, 2026-10-17.** An earlier version reported 106 relayers and a 31.0%
> top-five share over a 1,000-block window that was never named, so it could not be
> reproduced. Re-measured over a named window above. The direction of the finding is
> unchanged: relayers are a small, repeating set, which is what makes
> `transaction.from` usable as the facilitator signal.

This supports the identification model — `transaction.from` is a facilitator's
relayer, not the payer — and it is the feature the signaling classifier will use
later for "concentration in a single facilitator".

## 8. Observed population characteristics

From the named window 52,680,019–52,680,138 (214 settlements):

- **109 distinct payers, 50 distinct sellers, 48 relayers** — a long tail, not a
  handful of participants.

> **Correction, 2026-10-17.** An earlier version reported 61 payers, 33 sellers and
> 28 relayers for this population. Those were the counts over the 153 settlements
> that were *also* calldata-decodable, not over all 214 that were decoded from
> events. Independent review caught the substitution. The full-population counts are
> above, re-derived from chain.
- Amounts observed include `2000`, `5000`, `20000`, `50000`, `5695`, `20920` in
  USDC base units — i.e. 0.002 to 0.05 USDC. The dominant values repeat heavily.
- One payer–seller pair (`0x105538b0…` → `0xa9dd7cc9…`) appears repeatedly at an
  identical amount of `2000` across many consecutive blocks.

That last observation is recorded as an **observation about the payment record**,
not a conclusion about any entity. Whether it indicates machine-rate API metering,
a test harness, or something else is exactly the question the Phase A classifier
exists to examine, and the project does not assert an answer here.

## 9. Evidence transactions

All decoded on 2026-10-16 from blocks 52,680,019–52,680,138. Each row was produced
by `scripts/sample_settlements.py`; amounts are raw USDC base units (6 decimals).

| Transaction | Block | Raw amount |
|---|---|---|
| `0x0fd06a01a9dd82258d5fb0f5489639fb57ebc34b7cf9ce1f1a3a4db57449e8d2` | 52680019 | 2000 |
| `0x3b0356440e9ee176eaa5b365a0f556045c94920309c27e201cdb0845f451b95c` | 52680019 | 5000 |
| `0x43fca7e1c02ff738d0b8ddab9824b3830e21d3189c35e68558910048fbb2f6a4` | 52680019 | 2000 |
| `0x7e4a0bd5d54d5d74240d9249f6d9c74cceaa730f1ace6a66c9af0f8fa1092e73` | 52680019 | 2000 |
| `0x035b411490f840188fcf53dc00b54086d7848b716ab8a86838de2b21b48654fb` | 52680020 | 2000 |
| `0x0fa132fae97c1c1b0097018a733be7cc28750fea0777c29917b15d1c68e0cf09` | 52680020 | 50000 |
| `0x147e3ff74b0287207216572e7b13bdbb8ab6d94d033874bb8bb2b37b1d037bd8` | 52680020 | 2000 |
| `0x1a1bac1bf76376d202a0ec1251645bb9f456b41a846cc1bb28f846ad753bcfb8` | 52680020 | 2000 |
| `0x514a2ead39b9ced6f1c05720dcb5567bc6030bf98357e1a8a138429216f3768f` | 52680020 | 20000 |
| `0xa8b96f6cd3091a1946f2b0c5e121e3d190b61bf1cfcf797d3e2ba50ea23e505c` | 52680020 | 2000 |
| `0xc7b48b422a1ab47ba1880fca085c89fb8502db4a61cc14663fd8aa13048d6a91` | 52680020 | 20920 |
| `0xed15f0a7da318f821c89dd515da0cf9b90f1cd534b24851014a9fcc73ffa2014` | 52680021 | 2000 |
| `0x0640ac0d1cbbca6962ed45b48eefb2dfef676cbaf4e2462f65c951b79d81abe0` | 52680022 | 20000 |
| `0x3645013b71beecd53b720216074f0eabe8d30047278f333af63f90162e0a36c0` | 52680022 | 2000 |
| `0x38576833d267569ae2ffd74900f41787d0736caa30e727c5add5afb590fa4baf` | 52680022 | 5000 |
| `0x91946629144468b5f5c8dd2ec45ead15c6f58791817fb1017c4a9f80565de22b` | 52680022 | 2000 |
| `0x9f3ac6cef4d567d856ca4a55e9714e3cdd3dbff8d9ebe069ea0c314faf655c37` | 52680022 | 2000 |
| `0x33638b4f36acf1bc5b552f3a0e64f215341dd1ea21a77b624748325e88fb56c8` | 52680023 | 20000 |
| `0x5309cc4c116bf103bc8777e799772bb1f69d653b5df5da9f6726259c0f70e90f` | 52680024 | 5000 |
| `0x993a39bf7be2e121358194dfb30feb7bd2b39ba80205ee7307c54e1f63cbdead` | 52680024 | 50000 |
| `0xb3f6d97dea0c02842d22915aee447935d76416e8c1f13a047dbf9d37ca4d796f` | 52680024 | 20000 |
| `0xd9bfd0cc93d22b00f9738fe65d3c7dfc9b57145be8f6bc61deb26e292a8728f7` | 52680024 | 2000 |
| `0xe9d2421d65399cb2d11a7a652ff790c9f3c27df000831f02b16e2ec6ddb212d8` | 52680024 | 5695 |
| `0x49f511b57dfecbf64f5b8b7fa9f9df8e6b66121d5e44ac0015d633dfe4331c40` | 52680025 | 2000 |
| `0x465e38c3c67efd96c598e1fec9937cf6708c627c5b4a081e404e3cc282148f64` | 52679374 | 10000 |

> The last row is the worked example from section 4 and sits outside the block range
> above, which is why its block number does not fit the sequence. An earlier version
> listed it at block 52,677,667; the transaction is in **52,679,374**, verified from
> both the transaction object and the receipt. Corrected 2026-10-17 after independent
> review.

## 10. Token population, measured over seven days

Census over blocks 52,377,000–52,679,399 (302,400 blocks), unfiltered by address so
that "only USDC uses this" is measured rather than assumed. 1,426,155
`AuthorizationUsed` events from 11 contracts.

| Token | Events | Resolved |
|---|---|---|
| USDC `0x833589fC…` | 1,421,707 | yes |
| `0x671be08d…` (no ERC-20 metadata; 9,789 bytes of code) | 1,460 | **no** |
| "USDC" `0xf6ee347d…` — a 333-byte contract reporting `symbol()='USDC'`, `name()='USD Coin'`, `decimals()=6`, and not USDC | 2,616 | yes (by address) |
| zkUSD, PHNM, sUSDp, USDp, CADC, EURC, cbBTC, cbMEGA | ~360 | yes |

Unresolved token share: **0.1024%**. Every token is keyed by address, never by
symbol — the `0xf6ee347d…` entry is why.

Mechanism paths over the same window: **EIP-3009** 1,426,155 settlements, **Permit2
proxy** 62 events that cannot be decoded (topics absent from 4byte.directory, no
`AuthorizationUsed`). The Permit2 events are counted as unresolved rather than
assumed to be nothing.

## 11. What is still unresolved

| Item | Status | What would resolve it |
|---|---|---|
| Selector `0x9aefaff8` (0.9% of settlements) | **unresolved** | find a verified ABI, or trace one transaction's call path |
| Whether the `0xcf092995` `bytes`-encoding variant has the same semantics as the `v,r,s` one | assumed same, not proven | decode both and compare against the event pair for a sample |
| Whether any token other than USDC appears in x402 settlement | **measured** — see §11 | — |
| Exact per-request result cap on the public RPC endpoints | measured at **20,000 results**, plus a 1,000-block range cap | — |
| Whether Base is available on Etherscan's free tier | **no — paid tier only**, measured 2026-10-23 | — |
| Terms of use for the public RPC endpoints | **owner accepted as fair use, 2026-10-23** | formal legal review still due at Phase D |
| Whether an `AuthorizationUsed` event is necessarily an x402 settlement | **still unresolved, and this is the important one** | nothing on chain distinguishes an ordinary EIP-3009 transfer from an x402 settlement; the distinction lives off-chain |

Nothing in this file is a claim about any entity. Section 8 describes patterns in
the payment record; the interpretation of those patterns is Phase A's job, and the
project's language rules apply there in full.
