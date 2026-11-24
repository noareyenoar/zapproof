"""Prebuilt answers for the published package.

The wheel cannot carry the 1.8 GB ingest database, so it carries the answers instead:
one row per seller holding the JSON `check_counterparty` returns. Rows are produced by
`build()` through the same `queries.fetch_score` + `shape.score_answer` path a live
lookup uses, so a snapshot answer and a live answer cannot drift. The disclaimer and
the methodology version are rewritten on read, so a snapshot never freezes a caveat
that has since changed.

Coverage is sellers only. A busy payer that has never sold is `unknown` here even
when the hosted index would describe it, and every snapshot answer says so rather
than letting an offline limitation read as "no history".
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repindex.api import queries
from repindex.api.models import DISCLAIMER_TEXT
from repindex.common.db import connect_readonly
from repindex.mcp.shape import score_answer

SNAPSHOT_ENV = "ZAPPROOF_SNAPSHOT"
PACKAGE_SNAPSHOT = Path(__file__).resolve().parent / "data" / "snapshot.db"


def default_path() -> Path | None:
    """The snapshot to use, if one exists: `ZAPPROOF_SNAPSHOT` or the packaged file."""
    override = os.environ.get(SNAPSHOT_ENV)
    path = Path(override).expanduser() if override else PACKAGE_SNAPSHOT
    return path if path.is_file() else None


def build(source: Path, dest: Path) -> dict[str, Any]:
    """Write `dest` from the ingest database at `source`. Returns a manifest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()

    src = queries.connect(source)
    dst = sqlite3.connect(dest)
    try:
        dst.execute("CREATE TABLE answers (identifier TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        dst.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

        sellers = [
            str(row[0]) for row in src.execute("SELECT seller FROM features ORDER BY seller")
        ]
        written = 0
        for seller in sellers:
            data = queries.fetch_score(src, seller)
            if data is None:
                continue
            payload = score_answer(data, "any")
            dst.execute(
                "INSERT INTO answers (identifier, payload) VALUES (?, ?)",
                (seller, json.dumps(payload, sort_keys=True)),
            )
            written += 1

        as_of_row = src.execute("SELECT MAX(as_of_block) FROM scores").fetchone()
        as_of_block = int(as_of_row[0] or 0)
        meta = {
            "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "as_of_block": str(as_of_block),
            "methodology_version": queries.METHODOLOGY_VERSION,
            "answers": str(written),
            "source_sellers": str(len(sellers)),
        }
        dst.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", meta.items())
        dst.commit()
    finally:
        dst.close()
        src.close()

    manifest = {
        **meta,
        "path": str(dest),
        "size_bytes": dest.stat().st_size,
        "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
    }
    dest.with_suffix(".json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def answer(path: Path, identifier: str, rail: str) -> dict[str, Any]:
    """Answer from the snapshot. Never raises for an address that is not in it."""
    conn = connect_readonly(path)
    try:
        row = conn.execute(
            "SELECT payload FROM answers WHERE identifier = ?", (identifier.lower(),)
        ).fetchone()
        meta = dict(conn.execute("SELECT key, value FROM meta"))
    finally:
        conn.close()

    if row is None:
        return {
            "found": False,
            "identifier": identifier,
            "rail": rail,
            "evidence_rail": "base",
            "score": None,
            "status": "unknown",
            "flags": [],
            "correction": None,
            "methodology_version": queries.METHODOLOGY_VERSION,
            "source": "snapshot",
            "coverage": "sellers",
            "as_of": int(meta.get("as_of_block", 0)),
            "note": (
                "Not present in this offline snapshot. The snapshot covers sellers only "
                "and is a point-in-time copy: unknown here is not a low score, and it "
                "does not mean the hosted index has no record of the address."
            ),
            "disclaimer": DISCLAIMER_TEXT,
        }

    payload: dict[str, Any] = json.loads(row[0])
    payload.update(
        rail=rail,
        methodology_version=queries.METHODOLOGY_VERSION,
        disclaimer=DISCLAIMER_TEXT,
        source="snapshot",
        coverage="sellers",
    )
    return payload
