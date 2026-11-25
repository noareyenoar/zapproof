"""A database path is a path, not SQLite URI syntax (review M1).

SQLite's `file:` URIs reserve `?`, `#` and `%`; interpolating a raw path into one can
open a different file, drop `mode=ro`, or decode percent escapes. These tests pin the
shared helper and the API's read-only entry point, which takes a configured path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from repindex.api import queries
from repindex.common.db import readonly_uri
from repindex.ingest import schema


def _db_with_marker(path: Path) -> None:
    schema.initialize(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE path_marker (value TEXT)")
        conn.execute("INSERT INTO path_marker VALUES ('intended')")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("name", ["api with space.db", "api#fragment.db", "api%20name.db"])
def test_the_api_reads_the_database_it_was_given(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    _db_with_marker(path)

    conn = queries.connect(path)
    try:
        row = conn.execute("SELECT value FROM path_marker").fetchone()
        assert row["value"] == "intended"
    finally:
        conn.close()


def test_the_readonly_uri_percent_encodes_reserved_characters(tmp_path: Path) -> None:
    path = tmp_path / "a # b %20.db"

    uri = readonly_uri(path)

    assert uri.startswith("file:///")
    assert uri.endswith("?mode=ro")
    assert "#" not in uri
    assert " " not in uri
    assert "%23" in uri
    assert "%2520" in uri
