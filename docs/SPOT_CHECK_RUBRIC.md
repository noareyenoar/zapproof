# Spot-check rubric — v2

**First written:** 2026-10-28, before any adjudication ran.
**Revised:** 2026-10-30.
**Status:** binding. Changes require a new version and a recorded reason.

## Changelog

**v2 — 2026-10-30.** Four rounds of adjudication exposed three gaps that both readers
named independently, unprompted, in every round. The two adjudicators agreed with each
other only 65.8% of the time, which is a statement about the rubric rather than about
the classifier: a ruler that two careful readers read differently cannot measure
anything.

The changes below resolve exactly those reported gaps. They are **not** tuned toward
any classifier output — the classifier's own limitations are unchanged by this
document, and a stricter rubric would make it look worse, not better.

1. **Precedence rule** for the collision between "many distinct payers" and "amount
   entropy near zero". Both fired on flat-price services, and v1 gave no tie-break.
2. **The small-sample rule is no longer absolute.** v1 forced `cannot_tell` below 20
   settlements, which abstained on records showing reciprocal payments — the clearest
   closed-loop signature there is.
3. **Operator-group combination rule.** v1 said to ask the same question of the group
   but never said how a group answer combines with the seller's own record.

---

## Why this document exists

Prompt task A6 asks for a human spot-check: the owner reads 50 sellers and says agree
or disagree. The owner's answer was that this is not human work, and inspection showed
the test was worse than tedious — it could not fail.

Measured on 2026-10-28:

| | |
|---|---|
| Sellers in the database | 444 |
| With likelihood exactly 0.00 | **436** |
| Flagged at the 0.5 cutoff | **0** |

Forty-two of the fifty sampled sellers had no evidence of anything, so the check scored
about 84% **whether or not the classifier worked at all**.

This rubric replaces it with a question that has a defined answer, evidence that is
blinded, and controls that can fail.

---

## The single question

> **Could this payment record have been produced by a small number of parties paying
> themselves, or does it require independent customers?**

The adjudicator answers with exactly one of:

| Answer | Meaning |
|---|---|
| `requires_independent_customers` | the record cannot plausibly come from a closed group |
| `could_be_self_dealing` | a small closed group could have produced it |
| `cannot_tell` | the evidence is insufficient to decide either way |

**This is a question about the record, not about the entity.**

---

## Why the adjudicator is a model and not a person

The owner has decided, on 2026-10-28, that the adjudication is performed by language
models with no human in the loop.

**This is a real weakening and is recorded as one.** A model shares training data,
priors and blind spots with the author of the classifier. The compensations are
procedural:

1. **Blinding** — the adjudicator never sees the classifier's verdict or its rules.
2. **Prior commitment** — this rubric existed before any adjudication.
3. **Two adjudicators** — if they disagree with each other, the rubric is ambiguous and
   the gate fails; that failure is about the rubric, not the classifier.
4. **Negative control** — the same adjudication is run against a deliberately broken
   classifier. If agreement stays high, the adjudication is not reading evidence.
5. **External anchor** — addresses published in Coinbase's discovery catalogue are real
   advertised services; flagging one is a measurable false positive.

---

## What the adjudicator sees

Per seller, and **only** these:

- settlement count
- distinct payer count
- repeat-buyer rate
- reciprocity rate
- amount entropy, 0 to 1
- peak-hour share
- busiest relayer's share, or `null` when no relayer was resolved
- active days
- observation window in hours
- up to three distinct sample transaction hashes
- operator group: group id, size and combined distinct payers, or an explicit note
  that no group was formed

## What the adjudicator must not see

`signaling_likelihood`, any rule name, the classifier's source, thresholds or weights,
or any label the pipeline assigned.

---

## Decision procedure (v2)

Apply these steps **in order** and stop at the first that settles the case. The order
is the tie-break that v1 lacked.

### Step 1 — reciprocity settles it

If **any** payer also sold to this seller (reciprocity > 0), answer
`could_be_self_dealing`.

A round trip between two parties is the clearest closed-loop signature available, and
in v1 it was overridden by the small-sample rule. It is not overridden now.

### Step 2 — a thin payer base settles it

If there are **three or fewer distinct payers across twenty or more settlements**,
answer `could_be_self_dealing`.

### Step 3 — a large, diverse payer set settles it

If there are **thirty or more distinct payers**, answer
`requires_independent_customers`.

Thirty distinct addresses paying one seller is not plausibly a closed group, whatever
the amounts look like. **Uniform pricing is not evidence against this** — a posted
price produces identical amounts by design.

### Step 4 — near one payer per settlement settles it

If distinct payers are **at least half** of settlements, answer
`requires_independent_customers`.

When almost every payment comes from a different address, the record is a customer
list. Flat amounts in that shape indicate a price, not a loop.

### Step 5 — everything else

Otherwise the record is genuinely ambiguous: between four and twenty-nine payers, with
repeat custom, and no reciprocity. Answer `cannot_tell` unless one of the following is
decisive:

- **Very low payer diversity with repetition**: fewer than one payer per four
  settlements, with the same amount throughout → `could_be_self_dealing`.
- **Wide variety with independent-looking spread**: entropy above 0.5, payments spread
  over several days, and payer diversity above 0.2 → `requires_independent_customers`.

### Step 6 — the small-sample floor

With **fewer than twenty settlements**, and none of steps 1–4 having applied, answer
`cannot_tell`. This floor no longer overrides step 1.

---

## Operator groups

Where a group was formed, apply the same six steps to the **group's combined record**,
using the combined distinct-payer count and the group size.

- If the group settles the case, use that answer.
- If the group cannot settle it, fall back to the seller's own record.
- If the two disagree, answer `cannot_tell` and say so in the reason.

An operator group is a hypothesis built from shared payers. Membership is not evidence
of anything by itself; only the group's payment record is.

---

## What is explicitly **not** evidence

| Observation | Why it is not evidence |
|---|---|
| Repeat buyers | A successful business has repeat customers. |
| Uniform amounts, with a diverse payer set | A posted price. |
| A single relayer carrying most settlements | Facilitators legitimately serve many sellers. |
| Membership in an operator group | A hypothesis, not a fact. |
| A burst of activity | A campaign, unless payers are also thin. |
| Absence of evidence | Not a verdict. `cannot_tell` exists for this. |

---

## How agreement is computed

| Classifier | Bucket |
|---|---|
| `signaling_likelihood` ≥ 0.5 | `could_be_self_dealing` |
| `signaling_likelihood` < 0.5 | `requires_independent_customers` |

Agreement = the adjudicator's answer equals the classifier's bucket.

**`cannot_tell` counts as disagreement.** Abstention must not be a way to score well
without committing. If abstention exceeds 30% of the sample, the rubric is inadequate
and the run fails regardless of the agreement rate.

---

## What makes the run fail

| Condition | Threshold |
|---|---|
| Sample contains fewer than 10 flagged sellers | **FAIL**, not a pass — the test is not discriminative |
| Agreement, adjudicator vs classifier | below 70% |
| Agreement, adjudicator vs adjudicator | below 70% — the rubric is ambiguous |
| Agreement under the deliberately broken classifier | above 40% — the adjudication is not reading evidence |
| Advertised services flagged | any |
| Abstention | above 30% |

---

## Change control

- Any edit increments the version and states the reason.
- The rubric must be committed **before** the adjudication it governs.
- Editing it after seeing results, without a recorded reason, invalidates the run.
