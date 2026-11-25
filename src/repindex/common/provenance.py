"""Provenance for measured artefacts: which code and which data produced them.

The independent Gate B review (F12) found that the gate could read an artefact
produced from a different commit and a different database and call it fresh. Every
producer now records the commit and the database hash it measured, and the gate
rejects anything that does not match what is on disk.

The hash covers the main database file only. A SQLite database in WAL mode can hold
uncheckpointed changes in its `-wal` file, so the same main-file hash can describe two
different logical states (review M2). `require_checkpointed` refuses to attest an
artefact while such changes exist; the checkpoint, not a broader hash over a file a
live writer can change, is what makes the identity true.
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from repindex.common.db import connect_readonly

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DB = REPO_ROOT / "data" / "repindex.db"


class PendingWALError(RuntimeError):
    """The database has uncheckpointed WAL frames, so its main-file hash is stale."""


class DatabaseChangedError(RuntimeError):
    """The database moved between the start and the end of a measurement."""


def require_checkpointed(db_path: Path) -> None:
    """Refuse to identify a database by its main file while its WAL holds changes."""
    wal = db_path.with_name(db_path.name + "-wal")
    if not wal.is_file():
        return
    size = wal.stat().st_size
    if size == 0:
        return
    raise PendingWALError(
        f"{wal.name} holds {size} bytes of uncheckpointed changes. A hash of the main "
        "database file does not describe the state these artefacts are measured from, "
        "so publishing it as provenance would be false. Stop every writer and "
        "checkpoint the database (`PRAGMA wal_checkpoint(TRUNCATE)`) before running "
        "this producer."
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def attest_identity(db_path: Path) -> str:
    """Hash a checkpointed database *before* its contents are read.

    A main-file hash is an identity only while the WAL holds no changes (review M2),
    and it identifies this measurement's data only if it is taken before the reads
    and confirmed after them (review N1). Hash here, read, then pass this value to
    `require_unchanged` or to `provenance(..., attested=...)` before writing anything.
    """
    require_checkpointed(db_path)
    return file_sha256(db_path)


def data_version(conn: sqlite3.Connection) -> int:
    """The connection's view of SQLite's change counter for other connections.

    `PRAGMA data_version` increments whenever any other connection commits a change,
    including a commit that is later undone and checkpointed. A before/after hash
    comparison cannot see that case: the restored bytes equal the attested ones even
    though the reads saw a different state (review O1).
    """
    row = conn.execute("PRAGMA data_version").fetchone()
    return int(row[0])


def require_no_commits(db_path: Path, before: int, after: int) -> None:
    """Refuse if another connection committed while the database was being read."""
    if before != after:
        raise DatabaseChangedError(
            f"another connection committed to {db_path.name} while it was being read "
            f"(data_version {before} → {after}). The numbers read may mix two states "
            "even if the file's bytes were later restored. Stop every writer and run "
            "this producer again."
        )


class CommitObserver:
    """An idle read-only connection that observes commits by other connections.

    `PRAGMA data_version` on this connection moves whenever any other connection
    commits, including a commit that is later undone and checkpointed. Open it
    *before* `attest_identity` starts hashing and check it after the reads and the
    confirming hash, so the observed window spans the hash itself. A hash equality
    check alone ends after the second hash and cannot see a commit that happened
    while the first one was running (reviews O1, P1).
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn = connect_readonly(db_path)
        self._start = data_version(self._conn)

    def __enter__(self) -> CommitObserver:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # On a body exception, close without replacing that exception.
        try:
            if exc_type is None:
                self.require_quiet()
        finally:
            self._conn.close()

    def require_quiet(self) -> None:
        """Refuse if any other connection committed since the observer opened."""
        require_no_commits(self._db_path, self._start, data_version(self._conn))

    def close(self) -> None:
        self._conn.close()


def require_unchanged(db_path: Path, attested: str) -> str:
    """Refuse if the database no longer has the identity `attest_identity` returned.

    A writer that commits and does not checkpoint leaves WAL frames, which
    `require_checkpointed` refuses; a writer that commits and checkpoints rewrites
    the main file, which changes the hash. Either way the numbers already read and
    the hash in the artefact would name different states, so no artefact may be
    written from them. Returns the confirmed hash.
    """
    require_checkpointed(db_path)
    current = file_sha256(db_path)
    if current != attested:
        raise DatabaseChangedError(
            f"{db_path.name} changed while it was being measured (main file "
            f"{attested[:16]}… before, {current[:16]}… after). A writer committed or "
            "checkpointed during the reads, so an artefact written now would bind one "
            "state's data to another state's hash. Stop every writer, checkpoint the "
            "database, and run this producer again."
        )
    return current


def git_commit() -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"


def provenance(db: Path | None = None, attested: str | None = None) -> dict[str, Any]:
    """The commit, database hash and timestamp an artefact was produced from.

    Raises `PendingWALError` rather than record a main-file hash that does not
    describe the database's current state (review M2). When `attested` is the hash
    `attest_identity` returned before the reads, the hash recorded here is confirmed
    to equal it (review N1), so the artefact cannot bind one state's data to another
    state's hash.
    """
    db_path = db or DATA_DB
    digest: str | None = None
    if db_path.is_file():
        if attested is not None:
            digest = require_unchanged(db_path, attested)
        else:
            require_checkpointed(db_path)
            digest = file_sha256(db_path)
    return {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": git_commit(),
        "db_sha256": digest,
    }
