"""The packaged snapshot must answer like the live index, and admit its limits.

A snapshot is sellers-only and point-in-time. The failure this guards against is the
one the product exists to avoid: an address missing from a partial copy being read as
"no history" or, worse, as a zero.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from repindex import snapshot

SELLER = "0x" + "11" * 20
UNKNOWN = "0x" + "ab" * 20


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "source.db"
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE features (
            seller TEXT PRIMARY KEY,
            settlement_count INTEGER NOT NULL,
            unique_payers INTEGER NOT NULL,
            active_days INTEGER NOT NULL
        );
        CREATE TABLE classifications (
            seller TEXT PRIMARY KEY,
            signaling_likelihood REAL,
            contributions TEXT
        );
        CREATE TABLE scores (
            identifier TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            score REAL,
            confidence_band TEXT NOT NULL,
            as_of_block INTEGER NOT NULL
        );
        CREATE TABLE settlements (
            seller TEXT NOT NULL,
            block_number INTEGER NOT NULL,
            block_time INTEGER NOT NULL
        );
        CREATE TABLE labels (
            address TEXT NOT NULL,
            label_type TEXT NOT NULL,
            label_value TEXT NOT NULL,
            method TEXT NOT NULL,
            confidence REAL NOT NULL,
            evidence TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO features VALUES (?, 25, 7, 4)", (SELLER,))
    conn.execute("INSERT INTO classifications VALUES (?, 0.2, '[]')", (SELLER,))
    conn.execute("INSERT INTO scores VALUES (?, 'scored', 80.0, 'medium', 1000)", (SELLER,))
    conn.execute("INSERT INTO settlements VALUES (?, 900, 1700000000)", (SELLER,))
    conn.execute("INSERT INTO settlements VALUES (?, 1000, 1700100000)", (SELLER,))
    conn.execute(
        "INSERT INTO labels VALUES (?, 'operator_group', 'cluster:7', 'm', 0.9, 'ev')",
        (SELLER,),
    )
    conn.commit()
    conn.close()
    return path


def test_build_writes_manifest_and_answers(tmp_path: Path) -> None:
    dest = tmp_path / "snapshot.db"
    manifest = snapshot.build(_source(tmp_path), dest)

    assert dest.is_file()
    assert manifest["answers"] == "1"
    assert manifest["as_of_block"] == "1000"
    assert manifest["sha256"] == hashlib.sha256(dest.read_bytes()).hexdigest()

    written = json.loads(dest.with_suffix(".json").read_text(encoding="utf-8"))
    assert written["answers"] == "1"


def test_known_seller_answers_like_the_index(tmp_path: Path) -> None:
    dest = tmp_path / "snapshot.db"
    snapshot.build(_source(tmp_path), dest)

    answer = snapshot.answer(dest, SELLER, "any")

    assert answer["found"] is True
    assert answer["score"] == 80.0
    assert answer["status"] == "scored"
    assert answer["evidence_summary"]["settlements"] == 25
    assert answer["operator_groups"] == [7]
    assert answer["source"] == "snapshot"
    assert answer["coverage"] == "sellers"
    assert answer["as_of"] == 1000
    assert "not independently validated" in answer["disclaimer"]


def test_unknown_address_is_unknown_not_zero_and_says_why(tmp_path: Path) -> None:
    dest = tmp_path / "snapshot.db"
    snapshot.build(_source(tmp_path), dest)

    answer = snapshot.answer(dest, UNKNOWN, "any")

    assert answer["found"] is False
    assert answer["status"] == "unknown"
    assert answer["score"] is None
    assert answer["coverage"] == "sellers"
    assert "not a low score" in answer["note"]
    assert "hosted index" in answer["note"]


def test_rebuild_replaces_an_existing_snapshot(tmp_path: Path) -> None:
    dest = tmp_path / "snapshot.db"
    snapshot.build(_source(tmp_path), dest)
    manifest = snapshot.build(_source(tmp_path), dest)
    assert manifest["answers"] == "1"
