"""Build the compact database a hosted API can ship inside a container image.

The ingest database is ~1.9 GB, almost all of it raw settlement rows the API never
reads. A hosted host needs the same answers, not the same file: this script keeps the
columns `repindex.api.queries` uses and drops transaction hashes, block hashes, nonces,
mechanisms and the ingest bookkeeping. The result is small enough to bake into an
image and is opened read-only at runtime, like the full database.

It verifies itself before writing the manifest: a sample of sellers must produce a
byte-identical `fetch_score` result from the source and the compact database, and the
observed-role counts must match for one payer and one seller.

Run:  uv run python scripts/build_hosted_db.py [--source DB] [--out PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from repindex.api import queries

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "repindex.db"
DEFAULT_OUT = REPO_ROOT / "data" / "hosted.db"

SCHEMA = """
CREATE TABLE settlements (
    payer        TEXT NOT NULL,
    seller       TEXT NOT NULL,
    block_number INTEGER NOT NULL,
    block_time   INTEGER NOT NULL
);

CREATE TABLE features (
    seller                TEXT PRIMARY KEY,
    unique_payers         INTEGER NOT NULL,
    repeat_buyer_rate     REAL NOT NULL,
    reciprocity_rate      REAL NOT NULL,
    amount_entropy        REAL NOT NULL,
    burstiness            REAL NOT NULL,
    peak_hour_share       REAL NOT NULL,
    fresh_payer_share     REAL NOT NULL,
    self_funding_flag     INTEGER NOT NULL,
    relayer_concentration REAL,
    settlement_count      INTEGER NOT NULL,
    active_days           INTEGER NOT NULL,
    observation_window_hours REAL NOT NULL,
    methodology_version   TEXT NOT NULL
);

CREATE TABLE classifications (
    seller               TEXT PRIMARY KEY,
    signaling_likelihood REAL NOT NULL,
    contributions        TEXT NOT NULL,
    methodology_version  TEXT NOT NULL
);

CREATE TABLE scores (
    identifier        TEXT PRIMARY KEY,
    status            TEXT NOT NULL,
    score             REAL,
    confidence_band   TEXT NOT NULL,
    evidence_settlements    INTEGER NOT NULL,
    evidence_counterparties INTEGER NOT NULL,
    evidence_active_days    INTEGER NOT NULL,
    data_window_start INTEGER NOT NULL,
    data_window_end   INTEGER NOT NULL,
    as_of_block       INTEGER NOT NULL,
    methodology_version TEXT NOT NULL
);

CREATE TABLE labels (
    address       TEXT NOT NULL,
    label_type    TEXT NOT NULL,
    label_value   TEXT NOT NULL,
    method        TEXT NOT NULL,
    confidence    REAL NOT NULL,
    evidence      TEXT NOT NULL,
    PRIMARY KEY (address, label_type, label_value)
);

CREATE TABLE addresses (
    address          TEXT PRIMARY KEY,
    first_seen_block INTEGER,
    last_seen_block  INTEGER,
    is_payer         INTEGER NOT NULL,
    is_seller        INTEGER NOT NULL,
    is_relayer       INTEGER NOT NULL
);

CREATE TABLE entities (
    entity_id  INTEGER PRIMARY KEY,
    confidence REAL NOT NULL,
    method     TEXT NOT NULL,
    evidence   TEXT NOT NULL,
    methodology_version TEXT NOT NULL
);

CREATE TABLE entity_members (
    entity_id INTEGER NOT NULL,
    address   TEXT NOT NULL,
    PRIMARY KEY (entity_id, address)
);
"""

COPIES = (
    "INSERT INTO features SELECT * FROM src.features",
    "INSERT INTO classifications SELECT * FROM src.classifications",
    "INSERT INTO scores SELECT * FROM src.scores",
    "INSERT INTO labels SELECT address, label_type, label_value, method, confidence, "
    "evidence FROM src.labels",
    "INSERT INTO addresses SELECT * FROM src.addresses",
    "INSERT INTO entities SELECT entity_id, confidence, method, evidence, "
    "methodology_version FROM src.entities",
    "INSERT INTO entity_members SELECT entity_id, address FROM src.entity_members",
)

INDEXES = (
    "CREATE INDEX idx_settlements_seller ON settlements (seller)",
    "CREATE INDEX idx_settlements_payer ON settlements (payer)",
    "CREATE INDEX idx_labels_address ON labels (address)",
    "CREATE INDEX idx_entity_members_address ON entity_members (address)",
)


def _sample_sellers(conn: sqlite3.Connection) -> list[str]:
    """A deterministic sample spanning the evidence distribution, not just the head."""
    rows = conn.execute(
        "SELECT seller FROM features ORDER BY settlement_count DESC, seller"
    ).fetchall()
    sellers = [str(row[0]) for row in rows]
    if not sellers:
        return []
    picks = {0, 1, len(sellers) // 2, max(0, len(sellers) - 2), len(sellers) - 1}
    return [sellers[index] for index in sorted(picks)]


def _verify(source: Path, dest: Path, sellers: list[str]) -> None:
    """The compact database must answer exactly like the source for sampled records."""
    src = queries.connect(source)
    dst = queries.connect(dest)
    try:
        if queries.as_of_block(src) != queries.as_of_block(dst):
            raise SystemExit("as_of_block differs between source and hosted database")
        if queries.sellers_indexed(src) != queries.sellers_indexed(dst):
            raise SystemExit("seller count differs between source and hosted database")

        for seller in sellers:
            if queries.fetch_score(src, seller) != queries.fetch_score(dst, seller):
                raise SystemExit(f"fetch_score differs for {seller}")

        payer = src.execute(
            "SELECT payer FROM settlements GROUP BY payer ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
        if payer is not None and queries.fetch_observed_roles(
            src, str(payer[0])
        ) != queries.fetch_observed_roles(dst, str(payer[0])):
            raise SystemExit(f"payer role counts differ for {payer[0]}")

        entity = src.execute("SELECT entity_id FROM entities ORDER BY entity_id LIMIT 1").fetchone()
        if entity is not None and queries.fetch_entity(src, int(entity[0])) != queries.fetch_entity(
            dst, int(entity[0])
        ):
            raise SystemExit(f"entity differs for {entity[0]}")
    finally:
        src.close()
        dst.close()


def build(source: Path, dest: Path) -> dict[str, object]:
    if not source.is_file():
        raise SystemExit(f"source database not found: {source}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()

    conn = sqlite3.connect(dest)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("ATTACH DATABASE ? AS src", (str(source),))
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO settlements (payer, seller, block_number, block_time) "
            "SELECT payer, seller, block_number, block_time FROM src.settlements"
        )
        for statement in COPIES:
            conn.execute(statement)
        for statement in INDEXES:
            conn.execute(statement)
        conn.commit()

        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "settlements",
                "features",
                "classifications",
                "scores",
                "labels",
                "addresses",
                "entities",
                "entity_members",
            )
        }
        as_of = int(conn.execute("SELECT MAX(block_number) FROM settlements").fetchone()[0] or 0)
        sellers = _sample_sellers(conn)
    finally:
        conn.close()

    _verify(source, dest, sellers)
    conn = sqlite3.connect(dest)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()

    manifest: dict[str, object] = {
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": str(source),
        "path": str(dest),
        "size_bytes": dest.stat().st_size,
        "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
        "as_of_block": as_of,
        "counts": counts,
        "verified_sellers": sellers,
    }
    dest.with_suffix(".json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    manifest = build(args.source, args.out)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
