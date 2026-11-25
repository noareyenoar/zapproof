"""Tests for Phase A labeling (prompt task A3).

The point of these tests is not coverage. It is that a cluster is treated as a
hypothesis: it must carry its method, confidence, evidence and version, and two
sellers who merely happen to share one payer must not be linked.
"""

from __future__ import annotations

import itertools
import sqlite3
from pathlib import Path

import pytest

from repindex.ingest import schema
from repindex.labels import clusters, roles


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "labels.db"
    schema.initialize(path)
    return schema.connect(path)


_SEQ = itertools.count(1)


def add_settlement(db: sqlite3.Connection, payer: str, seller: str, relayer: str, n: int) -> None:
    # A unique key per call: the table's primary key is (tx_hash, log_index), so
    # deriving the hash from `n` alone collided whenever a test reused an n.
    unique = next(_SEQ)
    row = (
        f"0x{unique:064x}",
        0,
        1000 + n,
        f"0xblock{n}",
        1_700_000_000 + n,
        "0xtoken",
        6,
        2000,
        "0.002000",
        payer,
        seller,
        relayer,
        f"0xnonce{n}",
        "eip3009_authorization_used",
    )
    db.execute("INSERT INTO settlements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)


def test_roles_are_written_with_provenance(db: sqlite3.Connection) -> None:
    add_settlement(db, payer="0xpayer1", seller="0xseller1", relayer="0xrelayer", n=1)
    add_settlement(db, payer="0xpayer1", seller="0xseller2", relayer="0xrelayer", n=2)

    written = roles.assign_roles(db)

    assert written == 4  # payer1, seller1, seller2, relayer
    row = db.execute(
        "SELECT method, confidence, evidence, methodology_version FROM labels "
        "WHERE address='0xseller1' AND label_value='seller'"
    ).fetchone()
    assert row is not None
    assert row["method"] == "observed_role_in_settlement"
    assert row["confidence"] == 1.0
    assert "observed as seller in 1 settlements" in row["evidence"]
    assert row["methodology_version"] == "v1"


def test_role_labels_cover_every_address_in_every_role(db: sqlite3.Connection) -> None:
    add_settlement(db, payer="0xboth", seller="0xboth", relayer="0xrelayer", n=1)
    roles.assign_roles(db)

    address = db.execute("SELECT * FROM addresses WHERE address='0xboth'").fetchone()
    assert address["is_payer"] == 1
    assert address["is_seller"] == 1
    assert address["is_relayer"] == 0


def test_sellers_sharing_payers_are_clustered(db: sqlite3.Connection) -> None:
    # Two sellers paid by the same twelve payers, ten settlements each.
    for n in range(20):
        seller = "0xsellerA" if n % 2 == 0 else "0xsellerB"
        # Both sellers must be paid by the SAME payers, otherwise there is nothing to
        # cluster on. `n % 12` split them by parity and the test failed correctly.
        add_settlement(db, payer=f"0xpayer{(n // 2) % 10:02d}", seller=seller, relayer="0xr", n=n)

    found, _ = clusters.find_clusters(
        db, min_shared_payers=5, min_jaccard=0.5, min_settlements_per_seller=5
    )
    assert any(set(cluster) == {"0xsellerA", "0xsellerB"} for cluster in found)


def test_one_shared_payer_does_not_make_a_cluster(db: sqlite3.Connection) -> None:
    """A single coincidence is not evidence of a shared operator."""
    add_settlement(db, payer="0xshared", seller="0xsellerA", relayer="0xr", n=1)
    add_settlement(db, payer="0xshared", seller="0xsellerB", relayer="0xr", n=2)
    for n in range(10):
        add_settlement(db, payer=f"0xonlyA{n:02d}", seller="0xsellerA", relayer="0xr", n=10 + n)
        add_settlement(db, payer=f"0xonlyB{n:02d}", seller="0xsellerB", relayer="0xr", n=30 + n)

    found, _ = clusters.find_clusters(
        db, min_shared_payers=5, min_jaccard=0.5, min_settlements_per_seller=5
    )
    assert found == []


def test_cluster_labels_carry_method_confidence_evidence_and_version(
    db: sqlite3.Connection,
) -> None:
    for n in range(20):
        seller = "0xsellerA" if n % 2 == 0 else "0xsellerB"
        # Both sellers must be paid by the SAME payers, otherwise there is nothing to
        # cluster on. `n % 12` split them by parity and the test failed correctly.
        add_settlement(db, payer=f"0xpayer{(n // 2) % 10:02d}", seller=seller, relayer="0xr", n=n)

    found, _ = clusters.find_clusters(
        db, min_shared_payers=5, min_jaccard=0.5, min_settlements_per_seller=5
    )
    clusters.write_clusters(db, found)

    entity = db.execute("SELECT * FROM entities").fetchone()
    assert entity["method"] == "shared_payers_jaccard"
    assert 0.0 <= entity["confidence"] <= 1.0
    assert entity["methodology_version"] == "v1"
    assert "hypothesis, not a fact" in entity["evidence"]

    label = db.execute("SELECT * FROM labels WHERE label_type='operator_group'").fetchone()
    assert label is not None
    assert label["method"] == "shared_payers_jaccard"
    assert label["methodology_version"] == "v1"


def test_clustering_is_deterministic(db: sqlite3.Connection) -> None:
    """GA.5 compares rebuild hashes, so the same data must give the same clusters."""
    for n in range(20):
        seller = "0xsellerA" if n % 2 == 0 else "0xsellerB"
        # Both sellers must be paid by the SAME payers, otherwise there is nothing to
        # cluster on. `n % 12` split them by parity and the test failed correctly.
        add_settlement(db, payer=f"0xpayer{(n // 2) % 10:02d}", seller=seller, relayer="0xr", n=n)

    first, _ = clusters.find_clusters(
        db, min_shared_payers=5, min_jaccard=0.5, min_settlements_per_seller=5
    )
    second, _ = clusters.find_clusters(
        db, min_shared_payers=5, min_jaccard=0.5, min_settlements_per_seller=5
    )
    assert first == second


def test_a_payer_paying_many_sellers_is_excluded_and_counted(db: sqlite3.Connection) -> None:
    """A marketplace with many sellers would otherwise light every seller up."""
    many = clusters.MAX_SELLERS_PER_PAYER + 5
    for n in range(many):
        add_settlement(db, payer="0xmarketplace", seller=f"0xseller{n:03d}", relayer="0xr", n=n)
        for k in range(5):
            add_settlement(
                db, payer=f"0xown{n:03d}{k}", seller=f"0xseller{n:03d}", relayer="0xr", n=n * 10 + k
            )

    found, skipped = clusters.find_clusters(
        db, min_shared_payers=2, min_jaccard=0.5, min_settlements_per_seller=5
    )
    assert skipped == 1
    assert found == []
