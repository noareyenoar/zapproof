"""Fetch one score in a fresh process and print its response hash (gate GB.5).

The review found that the old determinism check compared two reads in the same
process, which is nearly true by construction. A third party reproducing the score
runs a separate process; this probe is that process. verify_api.py runs it twice and
also once for a different identifier as a negative control.

Run:  uv run python scripts/determinism_probe.py <identifier>
"""

from __future__ import annotations

import hashlib
import json
import sys

from fastapi.testclient import TestClient

from repindex.api.app import app


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: determinism_probe.py <identifier>", file=sys.stderr)
        return 2

    identifier = sys.argv[1]
    response = TestClient(app).get(f"/v1/score/{identifier}")
    print(
        json.dumps(
            {
                "identifier": identifier,
                "status": response.status_code,
                "sha256": hashlib.sha256(response.content).hexdigest(),
            }
        )
    )
    return 0 if response.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
