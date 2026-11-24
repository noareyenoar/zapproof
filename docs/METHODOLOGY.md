# Methodology — rating v1

**Version:** `v1` (see `src/repindex/common/version.py`; the API and MCP report this
string with every answer)
**Status:** Phase B. Published as a preview, not as a validated product. Every number
this method produces carries the limitations recorded at the bottom of this page.

**What changed from v0 to v1 (2026-10-30).** Five classifier defects were corrected
after the blinded adjudication: `repeat_buyer_dominant` now requires a small payer base
(≤10) as well as repeat custom; `fixed_amount` and `concentrated_in_time` abstain when
distinct payers are at least half of settlements, because that is a customer list, not
a loop; `round_trip` fires at ≥5% reciprocal payers, matching the rubric's "any
reciprocal payer"; `single_relayer` abstains when no relayer was resolved. The unused
`fresh_payer_share` rule was already removed in v0 and remains a reported feature
only. This document now describes what `rules.py` actually does.

---

## What the score is

A number from **0 to 100** expressing *how much settled payment evidence a seller has,
discounted by how signaling-like that evidence looks*.

It is **not** a probability that the seller is honest. It is **not** a judgement about
any person or company. It is a summary of a payment record, and the record is all it
knows.

## What the score is not, and what "unknown" looks like

There are three distinct outcomes, and conflating them is the mistake this method is
built to avoid:

| Situation | Output |
|---|---|
| Enough evidence to score | a number from 0 to 100, with a confidence band |
| Some evidence, but below the minimum | **`insufficient_evidence`** — and no number |
| No seller-side evidence at all | **`found: false`** — and no number |

**Neither of the last two is a low score.** A seller nobody has paid is unknown, not
bad. Gate check GA.11 tests this, and the database enforces it with CHECK constraints:
a row with status `insufficient_evidence` cannot carry a numeric score, and a row with
status `scored` cannot lack one.

`signaling_likelihood` is **null** unless the status is `scored`, and the response
carries `judged: false`. An unclassified seller is refused by the scorer rather than
treated as having likelihood 0.0 (v1). The API additionally distinguishes an address
the index has never seen from one it knows as a payer but has never seen sell
(`role_observed`).

## Evidence minimum

A score is produced only when **all three** conditions hold
(`config/thresholds.yaml`, `phaseA.min_evidence_for_point_score`):

| Dimension | Minimum |
|---|---|
| Settlements | 20 |
| Unique counterparties | 5 |
| Active days | 3 |

Failing any one is enough to withhold the score. Volume in one dimension does not
compensate for emptiness in another.

## The score formula

```
evidence_strength = mean(
    min(1, (settlements     / 20) / 4),
    min(1, (counterparties  /  5) / 4),
    min(1, (active_days     /  3) / 4)
)

genuine_factor    = 1 - signaling_likelihood

score             = round(100 × evidence_strength × genuine_factor, 2)
```

Each dimension saturates at **four times the minimum**. Without that cap, a seller with
ten thousand settlements would max the score on volume alone, which would reward size
rather than credibility.

## Confidence band

Derived from the *weakest* dimension, as a multiple of its minimum:

| Weakest dimension is | Band |
|---|---|
| ≥ 4× the minimum | `high` |
| ≥ 2× the minimum | `medium` |
| at the minimum | `low` |

## `signaling_likelihood`

Computed by `src/repindex/classify/rules.py`. Eight named rules, each producing a
visible contribution, whether it fired, and a sentence saying why. All cutoffs live in
`RuleThresholds` so the sensitivity analysis can perturb them.

| Rule | Weight | Fires when | Abstains when |
|---|---|---|---|
| `self_loop` | 0.45 | payer and seller are the same address in at least one settlement | — |
| `thin_payer_base` | 0.30 | ≥20 settlements from ≤3 distinct payers | — |
| `repeat_buyer_dominant` | 0.20 | ≤10 distinct payers, ≥3 payers, and ≥80% of them bought more than once | >10 distinct payers: repeat custom inside a real customer base is a successful business |
| `fixed_amount` | 0.20 | ≥20 settlements and amount entropy ≤0.15 | ≥50% of settlements have a distinct payer: a fixed amount is a posted price |
| `bursty` | 0.15 | ≥20 settlements and burstiness ≥0.7 | — |
| `concentrated_in_time` | 0.30 | ≥20 settlements and ≥50% of them inside a single hour | observation window <24 h (too short to tell), or ≥50% distinct payers (a campaign, not a loop) |
| `single_relayer` | 0.10 | ≥20 settlements and ≥90% carried by one relayer | no relayer was resolved (the default backfill does not fetch the transaction envelope) |
| `round_trip` | 0.15 | ≥3 payers and ≥5% of payers also received payment from this seller | — |

The contributions sum and clamp to `[0, 1]`. Weights total **1.85**, so several rules
firing at once clamps: **a likelihood of 1.0 means "as signaling-like as this method
can express", not "certainly signaling"**.

The rules are a **classifier**, not a trained model. It can only find patterns someone
thought to write down.

### The weights are judgement, not measurement

There is no ground truth to fit them to. Prompt task A4 says so, and it is the reason
Phase A has a sensitivity analysis: perturb every threshold by ±25% and report how
much the aggregate moves. If the aggregate shift exceeds 15 percentage points, the
report's headline must say the estimate is unstable.

## Features and their proxies

| Feature | Fidelity |
|---|---|
| unique payers, repeat-buyer rate, reciprocity | exact |
| amount entropy (Shannon, normalised) | exact |
| burstiness (Goh–Barabási) and peak-hour share | exact |
| payer "age" (`fresh_payer_share`) | **proxy — computed, reported, and deliberately not used by any rule.** From settlement data alone a first-time customer and a purpose-created wallet look identical; the feature says so in its output rather than pretending to judge |
| self-funding | **exact for the same-address case only** — a settlement whose payer equals its seller. Operator-cluster membership is *not* folded in; sharing payers is a hypothesis, not proof of self-dealing |
| single-relayer concentration | exact when a relayer was resolved, `null` otherwise; the rule abstains rather than assuming zero |

A reader is entitled to discount the proxy. The method does not use it to move a score.

## Operator groups

Clusters of addresses that may share an operator. Stored as **hypotheses, never
facts**: every cluster carries its method, confidence, evidence and version, and the
evidence text says in words that overlapping payers may reflect a marketplace with
few customers rather than one operator. Cluster membership does not affect a score.

Signal used: shared payers, with a minimum of 5 shared payers and 0.6 Jaccard overlap.
Payers who paid more than 60 distinct sellers are excluded and counted — they are more
likely marketplaces than evidence of one operator.

## Determinism

Nothing in the pipeline reads the clock or uses randomness. The same database state
produces byte-identical scores. Gate check GA.5 wipes the derived tables, rebuilds
twice, and compares hashes. Gate check GB.5 additionally scores the same seller in two
fresh processes and compares the response bytes, with a different seller as a negative
control.

## How the method was checked, and what that check is worth

The classifier was compared against a **blinded adjudication** of 65 sellers
(`reports/adjudication/`). The material limitations of that comparison, which the API
and MCP repeat in the disclaimer:

1. **The adjudicators were language models, not humans** (owner decision 2026-10-28).
   They share training data and priors with the author of the classifier. Blinding, a
   prior-committed rubric, an inter-rater comparison and a negative control stand in
   for human scrutiny; they do not replace it.
2. **The headline 61.5% counts `cannot_tell` answers as disagreements** — 40
   agreements out of 65, with 17 abstentions (26.2%). Excluding the abstentions the
   agreement is **83.3% (40/48)**. Both readings are correct; the lower one is
   published because it is the conservative one.
3. **The two inter-rater verdict files are identical**, so the recorded 100%
   inter-rater agreement compares a file with itself. It does not demonstrate
   independent model agreement.
4. **No model names, prompts or run metadata were recorded** at the time. A caller
   cannot reproduce the adjudication. `reports/adjudication/run_metadata.json`
   records this absence rather than inventing detail.

## Limitations

1. **Settlement counts are cross-checked against a second RPC provider, not an
   independent indexer.** Gate A check GA.3 compares three seeded random windows on
   different days with logs fetched from an independent Base JSON-RPC provider; on
   2026-11-23 all three matched exactly in count and amount
   (`reports/crosscheck_A.json`). That verifies our ingestion and decoding against
   independently fetched data. It does not prove chain completeness, and a zero-cost
   independent indexer remains unavailable; the owner accepted that original
   limitation on 2026-10-23 (`a recorded owner decision (2026-10-23)`). Every published count
   is still one decoder's output and says so.
2. **An `AuthorizationUsed` event is not provably an x402 settlement.** On chain it is
   indistinguishable from an ordinary EIP-3009 transfer. The distinction lives
   off-chain, and this method's entire population is defined by that event.
3. **The synthetic validation is partly circular.** The planted-pattern test measures
   whether the rules find patterns the same person planted. It tests mechanics, not
   real-world truth.
4. **The classifier is rules, not a trained model.** It can only find patterns someone
   thought to write down.
5. **Only Base evidence is indexed.** A Lightning result is reported as
   `rail_not_indexed`, never as Base data under a different label.
