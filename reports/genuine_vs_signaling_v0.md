# Genuine commerce versus signaling-like activity — v1

**0.59% of settlements in this window belong to scored sellers with a signaling_likelihood of 0.5 or above.**

The evidence minimum excludes **95** sellers whose stored classification is at or above the 0.5 cutoff (**0.77%** of settlements in this window). No likelihood is published for them — `/v1/score` returns `insufficient_evidence` with a null likelihood and `judged: false` — so they are reported separately here and not counted in the headline.

## Data window

- Blocks **52,893,065–53,201,719**
- Spans **171.5 hours**
- **2,389,053** settlements

## Coverage

**Unsupported-share census (Phase 0, an earlier window: blocks 52,677,290–52,979,689).** This database stores only tokens the decoder supports, so an unsupported share cannot be measured over the window above. The seven-day census measured **0.1015%** of settlements in an unsupported token and **0.00431%** unresolved Permit2 mechanism events. Those figures describe the census window, not this report's.

- Contract `0x833589fcd6edb6e08f4c7c32d4f71b54bda02913`: 2,389,053 settlements
- Mechanism `eip3009_authorization_used`: 2,389,053

## Aggregate results

- Sellers observed: **19,397**
- Scored sellers at or above the 0.5 likelihood cutoff: **28**
- Sellers below the evidence minimum whose stored classification is at or above the 0.5 cutoff (no published likelihood): **95**
- Operator clusters proposed: **10** (hypotheses, not facts)
- Sellers with a numeric score: **1,433**
- Sellers returning `insufficient_evidence`: **17,964**

## Sensitivity

Every classifier threshold and weight was scaled by ±25%, one at a time and all at once. Largest move: **0.54 percentage points** (limit 15).
The result leans most on `global_down_25`: -0.54 pp.

## What this method cannot know

1. **Settlement counts are cross-checked against a second RPC provider, not an independent indexer.** Three seeded windows on different days matched exactly in count and amount on 2026-11-23 (`reports/crosscheck_A.json`). That verifies decoding and counting against independently fetched logs; it does not prove chain completeness, and every count here is still one decoder's output. The original no-zero-cost-indexer limitation was accepted by the owner on 2026-10-23 (`a recorded owner decision (2026-10-23)`).
2. **An `AuthorizationUsed` event is not provably an x402 settlement.** On chain it is indistinguishable from an ordinary EIP-3009 transfer. The population is defined by that event, and the distinction lives off-chain.
3. **The planted-pattern validation is partly circular.** It shows the rules find patterns the same author planted. It tests mechanics, not real-world truth.
4. **The classifier is rules, not a trained model.** It can only find patterns someone thought to write down, and the weights are judgement.
5. **A high likelihood is not an accusation.** It describes a payment record. The project does not label any entity, and no address appears in this report.

## Reproduce

Raw values with the command that produces each. GA.9 re-runs a random sample of these and compares the printed output to the value here.

| label | value | command |
|---|---|---|
| `settlement_count` | 2389053 | `uv run python scripts/metrics_report.py settlements` |
| `seller_count` | 19397 | `uv run python scripts/metrics_report.py sellers` |
| `scored_count` | 1433 | `uv run python scripts/metrics_report.py scored` |
| `insufficient_count` | 17964 | `uv run python scripts/metrics_report.py insufficient_evidence` |
| `flagged_seller_count` | 28 | `uv run python scripts/metrics_report.py flagged_sellers` |
| `flagged_settlement_share_pct` | 0.5902 | `uv run python scripts/metrics_report.py flagged_settlement_share_pct` |
| `flagged_below_minimum_seller_count` | 95 | `uv run python scripts/metrics_report.py flagged_below_minimum_sellers` |
| `flagged_below_minimum_settlement_share_pct` | 0.7735 | `uv run python scripts/metrics_report.py flagged_below_minimum_settlement_share_pct` |
| `nonzero_likelihood_scored_sellers` | 183 | `uv run python scripts/metrics_report.py nonzero_likelihood_scored_sellers` |
| `cluster_count` | 10 | `uv run python scripts/metrics_report.py clusters` |
| `window_start_block` | 52893065 | `uv run python scripts/metrics_report.py window_start` |
| `window_end_block` | 53201719 | `uv run python scripts/metrics_report.py window_end` |
| `window_hours` | 171.5 | `uv run python scripts/metrics_report.py window_hours` |
| `phase0_census_window_start_block` | 52677290 | `uv run python scripts/metrics_report.py phase0_census_window_start_block` |
| `phase0_census_window_end_block` | 52979689 | `uv run python scripts/metrics_report.py phase0_census_window_end_block` |
| `phase0_unsupported_token_share_pct` | 0.1015 | `uv run python scripts/metrics_report.py phase0_unsupported_token_share_pct` |
| `phase0_unresolved_mechanism_share_pct` | 0.00431 | `uv run python scripts/metrics_report.py phase0_unresolved_mechanism_share_pct` |
| `flag_cutoff` | 0.5 | `uv run python scripts/metrics_report.py flag_cutoff` |
| `sensitivity_max_shift_pp` | 0.5411 | `uv run python scripts/metrics_report.py sensitivity_max_shift_pp` |
| `sensitivity_perturbation_pct` | 25 | `uv run python scripts/metrics_report.py sensitivity_perturbation_pct` |
| `sensitivity_unstable_share_shift_pp` | 15 | `uv run python scripts/metrics_report.py sensitivity_unstable_share_shift_pp` |
