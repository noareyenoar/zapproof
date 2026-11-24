"""Role labels derived from observed settlements (prompt task A3).

A role here is not an inference: if an address appears as `payer` in a settlement,
it is a payer in that settlement, with confidence 1.0. What is *not* claimed is that
the address is only ever a payer, or that it belongs to a human rather than a
process. The label records the observation, and `evidence` records which observation.

`methodology_version` is mandatory on every row because gate check GA.5 rebuilds the
derived tables and compares hashes; without a version stamp a change of method and a
change of data would look identical.
"""

from __future__ import annotations

import sqlite3

from repindex.common.version import METHODOLOGY_VERSION

# Roles are observations, so confidence is 1.0 by definition. Any weaker number here
# would be a claim about something the data does not support.
OBSERVED_ROLE_CONFIDENCE = 1.0

ROLE_COLUMNS = {
    "payer": "is_payer",
    "seller": "is_seller",
    "facilitator_relayer": "is_relayer",
}


def assign_roles(conn: sqlite3.Connection, methodology_version: str = METHODOLOGY_VERSION) -> int:
    """Populate `addresses` and write one role label per (address, role) observed.

    Returns the number of labels written. Runs inside one transaction, so a failure
    leaves neither the address rows nor the labels behind.
    """
    written = 0
    try:
        for role, column in ROLE_COLUMNS.items():
            # first/last seen come from the settlements themselves, not from wall
            # clock, so two rebuilds over the same data produce the same rows.
            conn.execute(
                f"""
                INSERT INTO addresses (address, first_seen_block, last_seen_block, {column})
                SELECT {role}, MIN(block_number), MAX(block_number), 1
                FROM settlements
                WHERE {role} IS NOT NULL
                GROUP BY {role}
                ON CONFLICT(address) DO UPDATE SET
                    first_seen_block = MIN(addresses.first_seen_block, excluded.first_seen_block),
                    last_seen_block  = MAX(addresses.last_seen_block,  excluded.last_seen_block),
                    {column} = 1
                """
            )

            # `facilitator_relayer` is nullable: the default backfill does not fetch the
            # transaction envelope, so every relayer is NULL. A NULL address is not an
            # address, and inserting one violated the NOT NULL constraint — the pipeline
            # stopped here on its first run over real data.
            rows = conn.execute(
                f"SELECT {role} AS address, COUNT(*) AS n, "
                "MIN(block_number) AS lo, MAX(block_number) AS hi "
                f"FROM settlements WHERE {role} IS NOT NULL GROUP BY {role}"
            ).fetchall()

            for row in rows:
                evidence = (
                    f"observed as {role} in {row['n']} settlements between blocks "
                    f"{row['lo']}-{row['hi']}"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO labels "
                    "(address, label_type, label_value, method, confidence, evidence, "
                    " methodology_version) VALUES (?,?,?,?,?,?,?)",
                    (
                        row["address"],
                        "role",
                        role,
                        "observed_role_in_settlement",
                        OBSERVED_ROLE_CONFIDENCE,
                        evidence,
                        methodology_version,
                    ),
                )
                written += 1
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    return written
