"""Schema migration safety for the duplicate-log index (review K2).

The unique index on `(tx_hash COLLATE NOCASE, log_index)` is applied by
`executescript` on every writable open. A database written before the index existed
could hold two rows for one log that differ only in letter case, in which case the
index build failed halfway and every later open repeated a bare traceback. These
tests pin the fix: the duplicate check runs first, the message names the pairs, the
file is not touched, and the correction CLI reports it as an error.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from repindex.ingest import schema

REPO_ROOT = Path(__file__).resolve().parent.parent
APPLY_CORRECTION = REPO_ROOT / "scripts" / "apply_correction.py"
HASH = "0x" + "ab" * 32


def _insert(conn: sqlite3.Connection, tx_hash: str, log_index: int = 7) -> None:
    conn.execute(
        "INSERT INTO settlements (tx_hash, log_index, block_number, block_hash, "
        "block_time, token, decimals, raw_amount, normalized_amount, payer, seller, "
        "facilitator_relayer, nonce, mechanism) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            tx_hash,
            log_index,
            1000,
            "0xblock",
            1,
            "0xtoken",
            6,
            2000,
            "0.002000",
            "0xpayer",
            "0xseller",
            None,
            "0xnonce",
            "eip3009_authorization_used",
        ),
    )


def _legacy_db(path: Path, hashes: list[str]) -> None:
    """A database with the pre-K2 shape: no case-insensitive unique index."""
    conn = schema.connect(path)
    try:
        conn.execute("DROP INDEX idx_settlements_tx_nocase")
        conn.execute("DROP INDEX idx_settlements_block")
        for tx_hash in hashes:
            _insert(conn, tx_hash)
        conn.commit()
    finally:
        conn.close()


def test_a_legacy_database_without_the_index_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    _legacy_db(path, [HASH])

    conn = schema.connect(path)
    try:
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "idx_settlements_tx_nocase" in indexes
        assert "idx_settlements_block" in indexes
    finally:
        conn.close()


def test_a_legacy_case_variant_duplicate_is_refused_with_a_diagnostic(tmp_path: Path) -> None:
    path = tmp_path / "legacy_dup.db"
    _legacy_db(path, [HASH, "0x" + "AB" * 32])

    with pytest.raises(schema.SchemaError) as excinfo:
        schema.connect(path)

    message = str(excinfo.value)
    assert HASH in message
    assert "log_index=7" in message
    assert "DISPUTES.md" in message

    # The check runs before any DDL: the rows are still there, and neither the
    # unique index nor an unrelated index that the DDL would recreate exists.
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 2
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "idx_settlements_tx_nocase" not in indexes
        assert "idx_settlements_block" not in indexes
    finally:
        conn.close()


_WAL_CRASH_CHILD = """
import os
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
conn.execute("PRAGMA journal_mode = WAL")
conn.execute("PRAGMA wal_autocheckpoint = 0")
conn.execute(
    "CREATE TABLE settlements (tx_hash TEXT NOT NULL, log_index INTEGER NOT NULL)"
)
conn.executemany(
    "INSERT INTO settlements (tx_hash, log_index) VALUES (?, ?)",
    [(sys.argv[2], 7), (sys.argv[3], 7)],
)
conn.commit()
os._exit(0)
"""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_refused_dirty_wal_database_is_not_rewritten(tmp_path: Path) -> None:
    """A refused open must not checkpoint a legacy WAL (review L2).

    The child exits without closing, so the two duplicate rows live only in the
    write-ahead log. A read-write open would checkpoint them into the main file and
    delete the WAL; the read-only precheck must leave both byte-identical.
    """
    path = tmp_path / "crash.db"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    child = subprocess.run(
        [sys.executable, "-c", _WAL_CRASH_CHILD, str(path), HASH, "0x" + "AB" * 32],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert child.returncode == 0, child.stderr

    wal = path.with_name(path.name + "-wal")
    assert wal.is_file(), "the child must leave frames in the WAL"
    main_before = _sha(path)
    wal_before = _sha(wal)

    with pytest.raises(schema.SchemaError):
        schema.connect(path)

    assert _sha(path) == main_before
    assert _sha(wal) == wal_before


@pytest.mark.parametrize("name", ["db with space.db", "db#hash.db", "db%20percent.db"])
def test_the_precheck_inspects_the_file_it_names(tmp_path: Path, name: str) -> None:
    """M1: a '#' or '%' in the path must not make the probe read another file."""
    path = tmp_path / name
    _legacy_db(path, [HASH, "0x" + "AB" * 32])

    with pytest.raises(schema.SchemaError) as excinfo:
        schema.connect(path)

    assert HASH in str(excinfo.value)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 2
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "idx_settlements_tx_nocase" not in indexes
    finally:
        conn.close()


def test_a_refusal_can_create_an_empty_wal_and_says_so(tmp_path: Path) -> None:
    """M4: the probe may create empty sidecars; the message must not deny it."""
    path = tmp_path / "no_sidecars.db"
    _legacy_db(path, [HASH, "0x" + "AB" * 32])
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")
    wal.unlink(missing_ok=True)
    shm.unlink(missing_ok=True)
    main_before = _sha(path)

    with pytest.raises(schema.SchemaError) as excinfo:
        schema.connect(path)

    assert "empty -wal" in str(excinfo.value)
    assert _sha(path) == main_before
    assert wal.is_file(), "the read-only probe creates a -wal when none exists"
    assert wal.stat().st_size == 0


def test_a_duplicate_after_the_precheck_is_reported_as_a_schema_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M5: losing the check-then-act race must not surface a bare IntegrityError."""
    path = tmp_path / "race.db"
    _legacy_db(path, [HASH, "0x" + "AB" * 32])
    # Simulate the interleaving: the read-only check sees no duplicates (a writer
    # inserted them after it ran), and the writable DDL then hits the unique index.
    monkeypatch.setattr(schema, "_legacy_duplicate_logs", lambda conn: [])

    with pytest.raises(schema.SchemaError) as excinfo:
        schema.connect(path)

    message = str(excinfo.value)
    assert "appeared between" in message
    assert "UNIQUE constraint failed" in message
    assert "DISPUTES.md" in message


def test_the_correction_cli_reports_the_migration_error_without_a_traceback(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy_dup.db"
    _legacy_db(path, [HASH, "0x" + "AB" * 32])

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [sys.executable, str(APPLY_CORRECTION), "--db", str(path), "--show"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )

    assert result.returncode == 1
    assert "error:" in result.stdout
    assert HASH in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
