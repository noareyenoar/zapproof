# Disputes and corrections

**Status:** built and tested. `scripts/apply_correction.py` applies a decision and
writes the audit row, and `scripts/rebuild_derived.py` re-applies active corrections
after it recomputes the derived tables. Gate check GB.10 runs
`tests/test_corrections.py`, which drives the script itself rather than inserting rows
directly.

## What can be disputed

| Subject | Example |
|---|---|
| An **operator-group label** | "these three addresses are not one operator" |
| A **role label** | "this address is not a seller on this record" |
| A **classification** | "the pattern this record shows is not signaling-like" |
| **Missing evidence** | "these settlements exist on chain but are absent from your index" |

A dispute cannot target the score's *existence*. `insufficient_evidence` is a statement
about our evidence, not about the operator, and it is not a negative verdict to appeal.

## What evidence is accepted

| Accepted | Why |
|---|---|
| On-chain transaction hashes showing a settlement we missed | Directly checkable; a gap is our error |
| Proof that two addresses share a controller, or that they do not | Addresses the cluster hypothesis directly |
| A signed message from the disputed key | Proves standing to dispute that identity |
| A published correction from another index with its own method | Cross-check, not authority |

| Not accepted | Why |
|---|---|
| "This is wrong" without evidence | Unfalsifiable |
| Offers to pay for a correction | The project does not sell standing |
| Requests to raise a score | Scores follow evidence, not requests |
| Identity documents, KYC material | Storing them would create a privacy and legal exposure we refuse |

## Process

1. **Open** — the disputant submits the subject, the claim, and evidence, to the
   address published on the site.
2. **Acknowledge** — within five working days, with a reference number.
3. **Assess** — we re-run the affected pipeline against the submitted evidence. This
   is mechanical, not discretionary.
4. **Decide** — one of: *upheld* (data corrected), *not upheld* (with the reason),
   *insufficient evidence to decide* (recorded as unresolved, never as a rejection).
5. **Apply and record** — an upheld decision changes the data and, in the same
   transaction, writes a row to the `corrections` table with the evidence, the
   reasoning, the methodology version, the previous state and the new state. A
   decision that is not upheld is recorded and changes nothing.
6. **Publish** — corrections appear in the change log. We do not silently edit.

## How a correction is applied

| Action | What changes |
|---|---|
| `remove_label` | Deletes a wrong role or operator-group label; the deleted row is kept in `previous_state` so a reversal can restore it |
| `add_label` | Records a missing interpretation with method `dispute`, the dispute's evidence, and the current methodology version |
| `add_settlement` | Inserts a settlement the index missed. The row must pass the same amount check as ingestion: `normalized_amount` must equal `raw_amount / 10**decimals` |
| `override_classification` | Replaces one seller's `signaling_likelihood` with the value the upheld evidence establishes and recomputes that seller's published score in the same transaction, so the API never serves a likelihood and a score that cannot both be true. The previous classification row is kept in `previous_state`, the rules' own value is recorded in the published contributions as `dispute_override`, and the rebuild re-applies the override after the rules run and before scores are computed |

An override recorded for a seller below the evidence minimum is still audited, but no
likelihood is published: the seller is not scored, so the API and MCP report the
correction as not scored rather than putting a number beside a null. This is the same
rule as `insufficient_evidence` everywhere else — an absent judgement is not replaced
by a smaller number.

Subjects and settlement addresses may be written as a bare address, an EIP-55
checksummed address, or a Base DID (`did:ethr:...`, `did:pkh:eip155:8453:...`). The
script normalises them to the lowercase identity the index stores, and rejects an
address that does not normalise rather than silencing the correction it names.

Settlement hashes and the nonce are canonicalised to lowercase as well, and
`tx_hash`/`block_hash` must be `0x` followed by 64 hex characters. Ingestion stores
the node's lowercase hashes, so a case variant of a log already in the index is
recognised as a duplicate rather than inserted as a second settlement. The database
enforces this too: a unique case-insensitive index on `(tx_hash, log_index)` makes a
second row for the same log unrepresentable, even for a writer that bypasses the
script.

### Repairing a legacy database

A database written before that index existed can hold two rows for one on-chain log
whose hashes differ only in letter case. Opening such a file for writing now fails
*before* any schema change, with an error naming the conflicting `(tx_hash,
log_index)` pairs, so the file is not left half-migrated. The check runs through a
read-only connection: the refused attempt does not rewrite the main database file or
the contents of an existing write-ahead log (SQLite may create an empty `-wal` and a
`-shm` sidecar). To repair it, inspect each named pair:

```sql
SELECT rowid, * FROM settlements
WHERE tx_hash = '<hash>' COLLATE NOCASE AND log_index = <n>;
```

Both rows describe the same on-chain event, so keeping either preserves the evidence.
Delete the extra row and reopen the database; once every log appears once, the unique
index builds. On a live index, take a copy first.

```sh
# remove a wrong operator-group label
uv run python scripts/apply_correction.py --db data/repindex.db \
    --subject 0x... --claim "not one operator" --evidence "signed message from each key" \
    --decision upheld --reason "cluster formed from shared payers alone" \
    --action remove_label --label-type operator_group --label-value cluster:61

# add a missing settlement from a JSON file holding one settlement row
uv run python scripts/apply_correction.py --db data/repindex.db \
    --subject 0x... --claim "settlement missing" --evidence "tx 0x..." \
    --decision upheld --reason "verified on chain" \
    --action add_settlement --settlement missing_settlement.json

# override a classification the evidence contradicts
uv run python scripts/apply_correction.py --db data/repindex.db \
    --subject 0x... --claim "these payments are not one closed loop" \
    --evidence "invoices and unrelated payer statements" \
    --decision upheld --reason "the record describes billed sales" \
    --action override_classification --likelihood 0.35

# reverse an earlier decision
uv run python scripts/apply_correction.py --db data/repindex.db \
    --reverse 3 --reason "evidence retracted" --applied-by owner
```

The derived tables are recomputed by `scripts/rebuild_derived.py`, which re-applies
every active correction: settlement corrections before the recompute so features see
them, classification overrides after the rules run and before scores are computed,
and label corrections after it because the recompute rewrites `labels`. Without that
step the next rebuild would silently undo a correction.

A reversal is a new row. It undoes the *effect* where the previous state allows it — a
removed label is restored, a label added by a correction is removed, an overridden
classification is recomputed from the current features so it describes the evidence
the index holds now (not the evidence at the time of the dispute), and the score is
recomputed from it in the same transaction; an earlier override that is still active
is re-applied over the recomputed rules. A reversal never edits the original decision
and never deletes a settlement: evidence that existed is not erased.

## Audit trail

The corrections table is append-only. Each row carries:

```
correction_id, subject, claim, evidence, decision, reason,
methodology_version, applied_at, applied_by, previous_state, new_state
```

No row is ever updated or deleted. A reversal is a new row referencing the old one.

## What this process deliberately does not do

- It does not remove evidence. If a settlement happened, it stays in the database;
  a correction changes an *interpretation* or fixes a *missing record*.
- It does not guarantee a particular outcome.
- It does not require the disputant to reveal who they are. Standing is proved with a
  key signature, not with identity documents.

## Service level

| Step | Target |
|---|---|
| Acknowledge | 5 working days |
| Decide | 30 calendar days, or a written note saying why not |

These are commitments, not measurements. If the project cannot meet them, say so here
rather than letting the promise stand.
