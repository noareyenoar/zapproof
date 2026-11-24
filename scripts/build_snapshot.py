"""Build the compact snapshot the published package ships.

The wheel carries prebuilt answers (sellers only) instead of the 1.8 GB ingest
database. Run this before `uv build`; the release runbook says so.

Run:  uv run python scripts/build_snapshot.py [--source DB] [--out PATH]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from repindex.snapshot import PACKAGE_SNAPSHOT, build

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "repindex.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=PACKAGE_SNAPSHOT)
    args = parser.parse_args()

    manifest = build(args.source, args.out)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
