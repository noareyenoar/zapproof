"""Reorg handling (prompt task A1, gate check GA.6).

Prompt task A1 requires that a reorg be detected by comparing stored block hashes and
that the mismatched blocks be rolled back. These tests run offline: the RPC layer is
monkeypatched, so they describe the behaviour rather than the current chain.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from repindex.ingest import pipeline, schema


def _settlement(block: int) -> Any:
    from repindex.ingest.decode import Settlement

    return Settlement(
        tx_hash=f"0x{block:064x}",
        log_index=0,
        block_number=block,
        token="0x" + "aa" * 20,
        payer="0x" + "bb" * 20,
        seller="0x" + "cc" * 20,
        facilitator_relayer="0x" + "dd" * 20,
        raw_amount=2000,
        nonce="0x" + "ee" * 32,
        decimals=6,
    )


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "reorg.db"
    schema.initialize(path)
    return schema.connect(path)


def test_a_reorg_is_detected_at_the_first_disagreeing_block(
    monkeypatch: pytest.MonkeyPatch, db: sqlite3.Connection
) -> None:
    blocks = list(range(2000, 2010))
    monkeypatch.setattr(
        pipeline,
        "fetch_settlements",
        lambda a, b, **_: (
            [_settlement(b) for b in blocks],
            {b: {"hash": f"0xoriginal{b}", "timestamp": b} for b in blocks},
        ),
    )
    pipeline.ingest_window(db, 2000, 2009)

    # The chain now differs from block 2006 onward.
    def chain(method: str, params: list[Any], **_: Any) -> dict[str, str]:
        number = int(params[0], 16)
        return {"hash": f"0xoriginal{number}" if number < 2006 else f"0xreplaced{number}"}

    monkeypatch.setattr(pipeline, "rpc_call", chain)

    assert pipeline.find_reorg(db, depth=60) == 2006


def test_rollback_removes_everything_from_the_fork_onward(
    monkeypatch: pytest.MonkeyPatch, db: sqlite3.Connection
) -> None:
    blocks = list(range(3000, 3010))
    monkeypatch.setattr(
        pipeline,
        "fetch_settlements",
        lambda a, b, **_: (
            [_settlement(b) for b in blocks],
            {b: {"hash": f"0xoriginal{b}", "timestamp": b} for b in blocks},
        ),
    )
    pipeline.ingest_window(db, 3000, 3009)

    removed = pipeline.rollback_to(db, 3005)

    assert removed == 4
    remaining = [row[0] for row in db.execute("SELECT block_number FROM settlements")]
    assert remaining == list(range(3000, 3006))
    checkpoint = db.execute("SELECT last_block FROM ingest_checkpoints WHERE id = 1").fetchone()[0]
    assert checkpoint == 3005


def test_reingesting_after_rollback_leaves_no_duplicates(
    monkeypatch: pytest.MonkeyPatch, db: sqlite3.Connection
) -> None:
    """The recovery path: roll back, re-ingest the correct blocks, count rows."""
    blocks = list(range(4000, 4010))
    monkeypatch.setattr(
        pipeline,
        "fetch_settlements",
        lambda a, b, **_: (
            [_settlement(b) for b in blocks],
            {b: {"hash": f"0xoriginal{b}", "timestamp": b} for b in blocks},
        ),
    )
    pipeline.ingest_window(db, 4000, 4009)
    pipeline.rollback_to(db, 4005)

    # The replacement blocks carry different transactions (different hashes), which is
    # what a reorg means: the old rows must not come back.
    def replacement(a: int, b: int, **_: Any) -> tuple[list[Any], dict[int, dict[str, Any]]]:
        rebuilt = []
        for block in range(a, b + 1):
            settlement = _settlement(block)
            rebuilt.append(
                type(settlement)(
                    tx_hash=f"0x{block:060x}ffff",
                    log_index=0,
                    block_number=block,
                    token=settlement.token,
                    payer=settlement.payer,
                    seller=settlement.seller,
                    facilitator_relayer=settlement.facilitator_relayer,
                    raw_amount=settlement.raw_amount,
                    nonce=settlement.nonce,
                    decimals=settlement.decimals,
                )
            )
        return rebuilt, {
            block: {"hash": f"0xnew{block}", "timestamp": block} for block in range(a, b + 1)
        }

    monkeypatch.setattr(pipeline, "fetch_settlements", replacement)
    pipeline.ingest_window(db, 4006, 4009)

    total = db.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
    duplicates = db.execute(
        "SELECT COUNT(*) FROM (SELECT tx_hash, log_index FROM settlements "
        "GROUP BY tx_hash, log_index HAVING COUNT(*) > 1)"
    ).fetchone()[0]

    assert total == 10, "six surviving originals plus four replacements"
    assert duplicates == 0
