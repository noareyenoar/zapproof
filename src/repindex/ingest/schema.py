"""Database schema for Phase A (prompt Section 7, task A1).

Design decisions worth knowing before reading the DDL:

- **Raw amounts are integers.** `raw_amount` is base units and `decimals` is stored
  beside it, so `normalized_amount` can always be re-derived exactly. Prompt task A2
  forbids floats for money and gate check GA.2 re-derives every row.
- **`(tx_hash, log_index)` is the primary key on `settlements`.** That is the natural
  key of a log, so ingestion is idempotent by construction rather than by convention.
- **`block_hash` is stored** so a reorg can be detected by comparing stored hashes
  against chain, rather than by trusting that it never happens.
- **Every derived row carries `methodology_version`.** Gate check GA.5 rebuilds the
  derived tables and compares hashes; a version stamp makes it obvious when a change
  in method, rather than a change in data, produced different output.
- **Labels are hypotheses.** `method`, `confidence`, `evidence` and
  `methodology_version` are all NOT NULL, so prompt task A3's requirement cannot be
  quietly skipped.

The database is SQLite in WAL mode (prompt Section 3.3).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from repindex.common.db import connect_readonly

SCHEMA_VERSION = 1

DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- One row per decoded settlement. Raw amount is an integer in base units.
CREATE TABLE IF NOT EXISTS settlements (
    tx_hash              TEXT    NOT NULL,
    log_index            INTEGER NOT NULL,
    block_number         INTEGER NOT NULL,
    block_hash           TEXT    NOT NULL,
    block_time           INTEGER NOT NULL,      -- unix seconds, from the block
    token                TEXT    NOT NULL,      -- lowercased contract address
    decimals             INTEGER NOT NULL,
    raw_amount           INTEGER NOT NULL,      -- base units, never a float
    normalized_amount    TEXT    NOT NULL,      -- exact decimal string
    payer                TEXT    NOT NULL,
    seller               TEXT    NOT NULL,
    -- Nullable on purpose: the relayer exists only in `tx.from`, which costs one RPC
    -- call per transaction to fetch. Measured 2026-10-28, settling 50 per block makes
    -- a seven-day relayer resolution about 125 hours. The other fields do not depend
    -- on it, so it is recorded when available and left NULL otherwise rather than
    -- making the backfill impossible.
    facilitator_relayer  TEXT,
    nonce                TEXT    NOT NULL,
    mechanism            TEXT    NOT NULL,
    PRIMARY KEY (tx_hash, log_index)
);

CREATE INDEX IF NOT EXISTS idx_settlements_block ON settlements (block_number);
CREATE INDEX IF NOT EXISTS idx_settlements_payer ON settlements (payer);
CREATE INDEX IF NOT EXISTS idx_settlements_seller ON settlements (seller);
CREATE INDEX IF NOT EXISTS idx_settlements_relayer ON settlements (facilitator_relayer);

-- Duplicate detection for `add_settlement` corrections (review I1, J2). The check
-- compares `tx_hash = ? COLLATE NOCASE`; the BINARY primary key cannot serve a NOCASE
-- comparison, and without this index each correction full-scanned 2.39 M rows
-- (measured 18-32 s on the production database). UNIQUE as well as fast: two rows for
-- one on-chain log differ only by letter case, so this makes the duplicate
-- unrepresentable rather than merely rejected.
CREATE UNIQUE INDEX IF NOT EXISTS idx_settlements_tx_nocase
    ON settlements (tx_hash COLLATE NOCASE, log_index);

-- The score endpoint reports each seller's window. With only `(seller)`, the window
-- query had to fetch every one of that seller's rows: measured 625 ms for the top
-- seller, which pushed API p95 from 264 ms to 317 ms across runs and made the
-- load-test threshold a coin toss. This covering index brings the same query to
-- 89 ms. A separate `(seller, block_time)` index was measured too and rejected: the
-- window query uses only one index, so it cost writes without helping the read.
CREATE INDEX IF NOT EXISTS idx_settlements_seller_block
    ON settlements (seller, block_number, block_time);

-- Every address seen in any role. Roles are separate columns rather than rows so a
-- single address that both pays and sells is one row, not two.
CREATE TABLE IF NOT EXISTS addresses (
    address        TEXT PRIMARY KEY,
    first_seen_block INTEGER,
    last_seen_block  INTEGER,
    is_payer       INTEGER NOT NULL DEFAULT 0,
    is_seller      INTEGER NOT NULL DEFAULT 0,
    is_relayer     INTEGER NOT NULL DEFAULT 0
);

-- Clusters of addresses believed to share an operator. A hypothesis, never a fact.
CREATE TABLE IF NOT EXISTS entities (
    entity_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    label         TEXT,
    method        TEXT NOT NULL CHECK (length(method) > 0),
    confidence    REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    evidence      TEXT NOT NULL CHECK (length(evidence) > 0),
    methodology_version TEXT NOT NULL CHECK (length(methodology_version) > 0),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS entity_members (
    entity_id  INTEGER NOT NULL REFERENCES entities(entity_id),
    address    TEXT    NOT NULL,
    PRIMARY KEY (entity_id, address)
);

-- Role and cluster labels. Prompt task A3: method, confidence, evidence and
-- methodology_version are mandatory, which the NOT NULL constraints enforce.
CREATE TABLE IF NOT EXISTS labels (
    address       TEXT    NOT NULL,
    label_type    TEXT    NOT NULL,     -- role | operator_group | other
    label_value   TEXT    NOT NULL,
    -- NOT NULL alone would accept an empty string, so provenance that is present but
    -- blank would pass. The length checks make "a label with no stated method" and
    -- "a label with an empty evidence field" unrepresentable, which is what prompt
    -- task A3 actually asks for.
    method        TEXT    NOT NULL CHECK (length(method) > 0),
    confidence    REAL    NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    evidence      TEXT    NOT NULL CHECK (length(evidence) > 0),
    methodology_version TEXT NOT NULL CHECK (length(methodology_version) > 0),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (address, label_type, label_value)
);

-- Per-seller features produced by the Phase A classifier (rules only, no ML).
CREATE TABLE IF NOT EXISTS features (
    seller                TEXT PRIMARY KEY,
    unique_payers         INTEGER NOT NULL,
    repeat_buyer_rate     REAL    NOT NULL,
    reciprocity_rate      REAL    NOT NULL,
    amount_entropy        REAL    NOT NULL,
    burstiness            REAL    NOT NULL,
    peak_hour_share       REAL    NOT NULL,
    fresh_payer_share     REAL    NOT NULL,
    self_funding_flag     INTEGER NOT NULL,
    relayer_concentration REAL,              -- NULL when no relayer was resolved
    settlement_count      INTEGER NOT NULL,
    active_days           INTEGER NOT NULL,
    observation_window_hours REAL NOT NULL DEFAULT 0.0,
    methodology_version   TEXT    NOT NULL
);

-- Per-seller classification. signaling_likelihood is in [0, 1] and every
-- contribution that produced it must be visible in `contributions`.
CREATE TABLE IF NOT EXISTS classifications (
    seller               TEXT PRIMARY KEY,
    signaling_likelihood REAL NOT NULL
        CHECK (signaling_likelihood >= 0.0 AND signaling_likelihood <= 1.0),
    contributions        TEXT NOT NULL,        -- JSON: [{rule, value, contribution}]
    methodology_version  TEXT NOT NULL
);

-- Per-settlement pattern flags, as opposed to the per-seller likelihood above.
CREATE TABLE IF NOT EXISTS settlement_flags (
    tx_hash     TEXT    NOT NULL,
    log_index   INTEGER NOT NULL,
    flag        TEXT    NOT NULL,
    detail      TEXT,
    PRIMARY KEY (tx_hash, log_index, flag)
);

-- Published scores. `score` is NULL exactly when the evidence is below the minimum,
-- and `status` then reads 'insufficient_evidence'. Gate check GA.11 tests that an
-- unknown is never rendered as zero.
CREATE TABLE IF NOT EXISTS scores (
    identifier        TEXT PRIMARY KEY,
    status            TEXT NOT NULL,          -- scored | insufficient_evidence
    score             REAL,
    confidence_band   TEXT NOT NULL,          -- low | medium | high
    evidence_settlements INTEGER NOT NULL,
    evidence_counterparties INTEGER NOT NULL,
    evidence_active_days INTEGER NOT NULL,
    data_window_start INTEGER NOT NULL,
    data_window_end   INTEGER NOT NULL,
    as_of_block       INTEGER NOT NULL,
    methodology_version TEXT NOT NULL,
    CHECK (
        (status = 'scored' AND score IS NOT NULL)
        OR (status = 'insufficient_evidence' AND score IS NULL)
    )
);

-- Where ingestion got to. Resumability depends on this being written in the same
-- transaction as the rows it describes.
CREATE TABLE IF NOT EXISTS ingest_checkpoints (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    last_block    INTEGER NOT NULL,
    last_block_hash TEXT NOT NULL,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per run, so a number can be traced to the run that produced it.
CREATE TABLE IF NOT EXISTS runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'running',
    detail        TEXT
);

-- Dispute decisions (prompt task B5). Append-only: a reversal is a new row that
-- references the old one, never an edit. Prompt task B5 requires an audit trail, and
-- an editable trail is not one.
CREATE TABLE IF NOT EXISTS corrections (
    correction_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    subject             TEXT NOT NULL CHECK (length(subject) > 0),
    claim               TEXT NOT NULL CHECK (length(claim) > 0),
    evidence            TEXT NOT NULL CHECK (length(evidence) > 0),
    decision            TEXT NOT NULL CHECK (decision IN ('upheld', 'not_upheld', 'insufficient')),
    reason              TEXT NOT NULL CHECK (length(reason) > 0),
    methodology_version TEXT NOT NULL CHECK (length(methodology_version) > 0),
    applied_at          TEXT NOT NULL DEFAULT (datetime('now')),
    applied_by          TEXT NOT NULL,
    previous_state      TEXT,
    new_state           TEXT
);

-- A reversal points at the row it reverses.
CREATE TABLE IF NOT EXISTS correction_reversals (
    reversal_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    reverses       INTEGER NOT NULL REFERENCES corrections(correction_id),
    reason         TEXT NOT NULL CHECK (length(reason) > 0),
    applied_at     TEXT NOT NULL DEFAULT (datetime('now')),
    applied_by     TEXT NOT NULL
);

-- Manifest of token and mechanism coverage (prompt task A1: unsupported tokens are
-- logged, not silently dropped).
CREATE TABLE IF NOT EXISTS coverage (
    token      TEXT NOT NULL,
    mechanism  TEXT NOT NULL,
    supported  INTEGER NOT NULL,
    settlement_count INTEGER NOT NULL,
    sample_tx_hashes TEXT,
    PRIMARY KEY (token, mechanism)
);
"""


DUPLICATE_KEY_INDEX = "idx_settlements_tx_nocase"


class SchemaError(RuntimeError):
    """The database cannot be brought up to the current schema in place."""


def _legacy_duplicate_logs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Find logs that the case-insensitive unique index would reject.

    A database written before that index existed can hold two rows for one on-chain
    log whose `tx_hash` differs only in letter case (review K2; the pre-I1
    `add_settlement` path could create them). `CREATE UNIQUE INDEX` then aborts the
    DDL halfway, and every later writable open repeats the same bare traceback. This
    check runs on a read-only connection *before* the database is opened for writing
    (review L2), so a refused open cannot checkpoint a legacy WAL or rewrite the main
    file it refuses to migrate; SQLite may create an empty `-wal` and a `-shm` sidecar
    (review M4). It returns [] when there is nothing to migrate.
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'settlements'"
    ).fetchone()
    if has_table is None:
        return []
    has_index = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
        (DUPLICATE_KEY_INDEX,),
    ).fetchone()
    if has_index is not None:
        return []
    rows = conn.execute(
        "SELECT lower(tx_hash) AS tx_hash, log_index, COUNT(*) AS row_count "
        "FROM settlements GROUP BY 1, 2 HAVING COUNT(*) > 1 ORDER BY 1, 2 LIMIT 11"
    ).fetchall()
    return list(rows)


def _duplicate_message(rows: list[sqlite3.Row]) -> str:
    shown = "; ".join(
        f"{row['tx_hash']} log_index={row['log_index']} ({row['row_count']} rows)"
        for row in rows[:10]
    )
    more = " (10 of more groups shown)" if len(rows) > 10 else ""
    return (
        "settlements holds more than one row for the same on-chain log when letter case "
        "is ignored, so the unique index on (tx_hash COLLATE NOCASE, log_index) cannot "
        f"be created: {shown}{more}. These rows predate the index. Inspect one with "
        "`SELECT rowid, * FROM settlements WHERE tx_hash = '<hash>' COLLATE NOCASE AND "
        "log_index = <n>`, keep one row per log, and reopen. The check runs read-only "
        "before the writable open, so this attempt did not change the main database "
        "file or the contents of an existing write-ahead log; SQLite may create an "
        "empty -wal and a -shm sidecar (review M4). The repair is described in "
        "DISPUTES.md ('Repairing a legacy database')."
    )


def connect(path: Path) -> sqlite3.Connection:
    """Open the database, creating the schema if this is a fresh file.

    An existing database is inspected through a read-only connection first. If it
    holds case-variant duplicate logs, the open is refused before the writable open,
    so the refused attempt does not checkpoint a legacy WAL or rewrite the main file
    (SQLite may create an empty `-wal` and a `-shm` sidecar; review L2/M4).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        probe = connect_readonly(path)
        probe.row_factory = sqlite3.Row
        try:
            duplicates = _legacy_duplicate_logs(probe)
        finally:
            probe.close()
        if duplicates:
            raise SchemaError(_duplicate_message(duplicates))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(DDL)
        conn.commit()
    except sqlite3.IntegrityError as exc:
        # M5: the read-only check and the DDL are separate transactions. If a writer
        # inserts a case-variant duplicate in that window, the unique index fails with
        # a bare constraint error and earlier DDL statements remain applied. Name the
        # cause and the repair instead of surfacing SQLite's message alone.
        conn.close()
        raise SchemaError(
            "a duplicate on-chain log appeared between the read-only check and the "
            "writable schema creation (another writer may be active), so the unique "
            "index could not be built. The rows are still present; stop other writers "
            f"and reopen, and the next attempt names the conflicting rows ({exc}). "
            "The repair is described in DISPUTES.md ('Repairing a legacy database')."
        ) from exc
    except BaseException:
        conn.close()
        raise
    return conn


def initialize(path: Path) -> None:
    conn = connect(path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO ingest_checkpoints (id, last_block, last_block_hash) "
            "VALUES (1, -1, '')"
        )
        conn.commit()
    finally:
        conn.close()
