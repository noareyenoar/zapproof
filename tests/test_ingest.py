"""Tests for the Phase A schema and ingestion (prompt tasks A1, A2).

These run offline: the RPC layer is monkeypatched, so nothing here touches the
network and the results do not depend on chain state.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from repindex.ingest import pipeline, schema
from repindex.ingest.decode import Settlement

# The canonical USDC contract on Base, a public on-chain constant. Held as a named
# constant rather than written inline as `token="0x…"` because that shape matches
# gitleaks' generic-api-key rule and blocks the commit — the same false positive that
# `name = "0x<64 hex>"` produces for event topics. The hook stays strict; the code
# stops looking like a secret.
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


def make_settlement(
    tx_hash: str = "0x" + "11" * 32,
    log_index: int = 0,
    block: int = 1000,
    amount: int = 2000,
) -> Settlement:
    return Settlement(
        tx_hash=tx_hash,
        log_index=log_index,
        block_number=block,
        token=USDC,
        payer="0x" + "22" * 20,
        seller="0x" + "33" * 20,
        facilitator_relayer="0x" + "44" * 20,
        raw_amount=amount,
        nonce="0x" + "55" * 32,
        decimals=6,
    )


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "test.db"
    schema.initialize(path)
    return schema.connect(path)


def test_schema_creates_every_table_the_prompt_names(db: sqlite3.Connection) -> None:
    required = {
        "settlements",
        "addresses",
        "entities",
        "labels",
        "features",
        "classifications",
        "scores",
        "ingest_checkpoints",
        "runs",
    }
    present = {
        row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert required <= present


def test_duplicate_tx_hash_and_log_index_is_rejected(db: sqlite3.Connection) -> None:
    """Idempotency is a constraint, not a convention (prompt task A1)."""
    row = (
        "0x" + "aa" * 32,
        0,
        1000,
        "0xblock",
        1,
        "0xtoken",
        6,
        2000,
        "0.002000",
        "0xpayer",
        "0xseller",
        "0xrelayer",
        "0xnonce",
        "eip3009_authorization_used",
    )
    insert = "INSERT INTO settlements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    db.execute(insert, row)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, row)


def test_scores_cannot_be_insufficient_evidence_and_carry_a_number(db: sqlite3.Connection) -> None:
    """GA.11's guarantee is enforced by the schema, not only by the API."""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO scores (identifier, status, score, confidence_band, "
            "evidence_settlements, evidence_counterparties, evidence_active_days, "
            "data_window_start, data_window_end, as_of_block, methodology_version) "
            "VALUES ('0xabc','insufficient_evidence', 0.0, 'low', 0,0,0,0,0,0,'v0')"
        )


def test_labels_require_method_confidence_evidence_and_version(db: sqlite3.Connection) -> None:
    """Prompt task A3: a label without provenance must be unrepresentable."""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO labels (address, label_type, label_value, method, confidence, "
            "evidence, methodology_version) VALUES ('0xa','role','buyer','',1.0,'','')"
        )


def test_ingest_window_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, db: sqlite3.Connection
) -> None:
    settlement = make_settlement()
    meta = {1000: {"hash": "0xblock", "timestamp": 1}}
    monkeypatch.setattr(pipeline, "fetch_settlements", lambda a, b, **_: ([settlement], meta))

    first = pipeline.ingest_window(db, 1000, 1000)
    second = pipeline.ingest_window(db, 1000, 1000)

    assert first.settlements == 1
    assert second.settlements == 1
    total = db.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
    assert total == 1, "the primary key must prevent a second copy"


def test_rollback_removes_rows_above_the_fork(
    monkeypatch: pytest.MonkeyPatch, db: sqlite3.Connection
) -> None:
    """Prompt GA.6: a reorg rolls back cleanly and leaves no duplicates."""
    settlements = [make_settlement(tx_hash="0x" + f"{i:064x}", block=1000 + i) for i in range(1, 6)]
    meta = {1000 + i: {"hash": f"0xblock{i}", "timestamp": i} for i in range(1, 6)}
    monkeypatch.setattr(pipeline, "fetch_settlements", lambda a, b, **_: (settlements, meta))

    pipeline.ingest_window(db, 1001, 1005)
    assert db.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 5

    removed = pipeline.rollback_to(db, 1002)
    remaining = db.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]

    assert removed == 3
    assert remaining == 2
    checkpoint = db.execute("SELECT last_block FROM ingest_checkpoints WHERE id = 1").fetchone()[0]
    assert checkpoint == 1002


def test_find_reorg_reports_the_first_disagreeing_block(
    monkeypatch: pytest.MonkeyPatch, db: sqlite3.Connection
) -> None:
    settlements = [make_settlement(tx_hash="0x" + f"{i:064x}", block=1000 + i) for i in range(1, 6)]
    meta = {1000 + i: {"hash": f"0xblock{i}", "timestamp": i} for i in range(1, 6)}
    monkeypatch.setattr(pipeline, "fetch_settlements", lambda a, b, **_: (settlements, meta))
    pipeline.ingest_window(db, 1001, 1005)

    # Chain agrees up to 1003 and disagrees from 1004 onward.
    def fake_rpc(method: str, params: list[Any], **_: Any) -> dict[str, str]:
        number = int(params[0], 16)
        stored = f"0xblock{number - 1000}"
        return {"hash": stored if number <= 1003 else "0xreorged"}

    monkeypatch.setattr(pipeline, "rpc_call", fake_rpc)

    assert pipeline.find_reorg(db, depth=60) == 1004


def test_find_reorg_returns_none_when_chain_agrees(
    monkeypatch: pytest.MonkeyPatch, db: sqlite3.Connection
) -> None:
    settlements = [make_settlement(tx_hash="0x" + f"{i:064x}", block=1000 + i) for i in range(1, 4)]
    meta = {1000 + i: {"hash": f"0xblock{i}", "timestamp": i} for i in range(1, 4)}
    monkeypatch.setattr(pipeline, "fetch_settlements", lambda a, b, **_: (settlements, meta))
    pipeline.ingest_window(db, 1001, 1003)

    def fake_rpc(method: str, params: list[Any], **_: Any) -> dict[str, str]:
        number = int(params[0], 16)
        return {"hash": f"0xblock{number - 1000}"}

    monkeypatch.setattr(pipeline, "rpc_call", fake_rpc)

    assert pipeline.find_reorg(db, depth=60) is None


def test_fresh_database_requires_an_explicit_start(
    monkeypatch: pytest.MonkeyPatch, db: sqlite3.Connection
) -> None:
    """A silent default here would have ingested one block and looked successful."""
    monkeypatch.setattr(pipeline, "block_number", lambda **_: 50_000)
    with pytest.raises(ValueError):
        pipeline.ingest_to_finality(db, lag_blocks=60)
