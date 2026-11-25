"""Canonical JSON and the evidence fingerprint shared by the API and the MCP tool.

The hash is a *fingerprint*, not an attestation: it lets a caller store an answer and
later re-fetch, then tell whether the evidence changed. It is unsigned on purpose — see
`reports/meetings/2026-11-25/06-lookup-receipt-research.md`.

Sorting keys and removing whitespace makes the bytes deterministic across processes.
The exclusions are fields that describe the record rather than the evidence: including
the permalink would make the hash depend on the host that served it, and including the
hash itself would be circular.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

EXCLUDED_FROM_HASH = ("evidence_hash", "permalink", "disclaimer")


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def evidence_hash(payload: dict[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key not in EXCLUDED_FROM_HASH}
    return "sha256:" + hashlib.sha256(canonical_json(body).encode()).hexdigest()
