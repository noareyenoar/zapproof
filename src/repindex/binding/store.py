"""Persistent state for identity binding (F5).

The review found that every binding property — single-use nonces, server-issued
challenges, revocation — lived in the caller's memory. A caller that passed a fresh
set of consumed nonces could replay a challenge; a caller that constructed a
`Challenge` object could skip issuance entirely. This module is the store the service
replays against, in its own database. It is deliberately **not** part of the score
API: that service is read-only and GB.4 proves it refuses writes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS challenges (
    nonce         TEXT PRIMARY KEY,
    evm_address   TEXT NOT NULL,
    nostr_pubkey  TEXT NOT NULL,
    issued_at     INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL,
    consumed_at   INTEGER
);

CREATE TABLE IF NOT EXISTS bindings (
    evm_address   TEXT PRIMARY KEY,
    nostr_pubkey  TEXT NOT NULL,
    nonce         TEXT NOT NULL,
    evm_signature TEXT NOT NULL,
    nostr_signature TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    revoked_at    INTEGER,
    revoked_by    TEXT
);

-- Append-only: a binding's history is evidence, and evidence is never edited.
CREATE TABLE IF NOT EXISTS binding_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    evm_address TEXT NOT NULL,
    kind        TEXT NOT NULL,
    detail      TEXT NOT NULL,
    at          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_binding_events_address
    ON binding_events (evm_address, at);
"""


class BindingStore:
    """SQLite-backed challenge and binding state. Writable by design; separate file."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # FastAPI runs sync endpoints on a threadpool, so two requests can reach the
        # connection at once. One lock is enough at this scale and makes every
        # read-modify-write below atomic with respect to the others.
        self._lock = threading.RLock()
        with self._lock, self.conn:
            self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- challenges -------------------------------------------------------- #

    def save_challenge(
        self,
        *,
        nonce: str,
        evm_address: str,
        nostr_pubkey: str,
        issued_at: int,
        expires_at: int,
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO challenges (nonce, evm_address, nostr_pubkey, issued_at, expires_at) "
                "VALUES (?,?,?,?,?)",
                (nonce, evm_address, nostr_pubkey, issued_at, expires_at),
            )

    def load_challenge(self, nonce: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT nonce, evm_address, nostr_pubkey, issued_at, expires_at, consumed_at "
                "FROM challenges WHERE nonce = ?",
                (nonce,),
            ).fetchone()
        return dict(row) if row else None

    def consume_challenge(self, nonce: str, now: int) -> bool:
        """Mark a challenge used. Returns False if it was already used.

        A conditional UPDATE rather than read-then-write: two requests racing on the
        same nonce cannot both observe it unused.
        """
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "UPDATE challenges SET consumed_at = ? WHERE nonce = ? AND consumed_at IS NULL",
                (now, nonce),
            )
        return cursor.rowcount == 1

    # --- bindings ---------------------------------------------------------- #

    def get_binding(self, evm_address: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM bindings WHERE evm_address = ?", (evm_address,)
            ).fetchone()
        return dict(row) if row else None

    def save_binding(
        self,
        *,
        evm_address: str,
        nostr_pubkey: str,
        nonce: str,
        evm_signature: str,
        nostr_signature: str,
        created_at: int,
    ) -> dict[str, Any]:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO bindings (evm_address, nostr_pubkey, nonce, evm_signature, "
                "nostr_signature, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (
                    evm_address,
                    nostr_pubkey,
                    nonce,
                    evm_signature,
                    nostr_signature,
                    created_at,
                    created_at,
                ),
            )
        return self.get_binding(evm_address) or {}

    def replace_binding(
        self,
        *,
        evm_address: str,
        nostr_pubkey: str,
        nonce: str,
        evm_signature: str,
        nostr_signature: str,
        created_at: int,
    ) -> bool:
        """Re-establish a revoked binding. Clears the revocation; history stays in events."""
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "UPDATE bindings SET nostr_pubkey = ?, nonce = ?, evm_signature = ?, "
                "nostr_signature = ?, created_at = ?, updated_at = ?, "
                "revoked_at = NULL, revoked_by = NULL WHERE evm_address = ?",
                (
                    nostr_pubkey,
                    nonce,
                    evm_signature,
                    nostr_signature,
                    created_at,
                    created_at,
                    evm_address,
                ),
            )
        return cursor.rowcount == 1

    def revoke_binding(self, evm_address: str, *, by: str, now: int) -> bool:
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "UPDATE bindings SET revoked_at = ?, revoked_by = ?, updated_at = ? "
                "WHERE evm_address = ? AND revoked_at IS NULL",
                (now, by, now, evm_address),
            )
        return cursor.rowcount == 1

    def rotate_binding(self, evm_address: str, *, new_nostr_pubkey: str, now: int) -> bool:
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "UPDATE bindings SET nostr_pubkey = ?, updated_at = ? "
                "WHERE evm_address = ? AND revoked_at IS NULL",
                (new_nostr_pubkey, now, evm_address),
            )
        return cursor.rowcount == 1

    # --- audit ------------------------------------------------------------- #

    def add_event(self, evm_address: str, kind: str, detail: Any, at: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO binding_events (evm_address, kind, detail, at) VALUES (?,?,?,?)",
                (evm_address, kind, json.dumps(detail, sort_keys=True), at),
            )

    def events(self, evm_address: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT kind, detail, at FROM binding_events WHERE evm_address = ? ORDER BY id",
                (evm_address,),
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self.conn.close()
