"""Read-only SQLite connections whose URI cannot be confused by a filename.

SQLite's URI syntax reserves `?` for the query, `#` for a fragment and `%` for a
percent-escape. Interpolating a path into `file:{path}?mode=ro` therefore does not
reliably name the file: a `#` starts a fragment, which discards `mode=ro` and makes
SQLite open the truncated path read-write; a `%20` is decoded to a space, so a
different file is opened (review M1). `Path.as_uri()` percent-encodes the path and
appending the query keeps the parameter where SQLite reads it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast


def readonly_uri(path: Path) -> str:
    """The SQLite file URI for `path`, read-only, safe for any legal filename."""
    return f"{path.resolve().as_uri()}?mode=ro"


def connect_readonly(path: Path, **kwargs: Any) -> sqlite3.Connection:
    """Open `path` read-only without letting its name be read as URI syntax.

    `kwargs` are passed to `sqlite3.connect` (for example `check_same_thread`).
    """
    return cast("sqlite3.Connection", sqlite3.connect(readonly_uri(path), uri=True, **kwargs))
