"""Produce the API artefacts gate B checks.

Writes, in one pass:

  reports/openapi.json             the schema GB.1 checks
  reports/determinism_B.json       two fresh processes, same bytes; negative control (GB.5)
  reports/fuzz_identifier.json     malformed identifiers and entity ids must not crash (GB.4)
  reports/write_probe.json         POST/PUT/DELETE are refused and the DB hash is unchanged (GB.4)
  reports/disclaimer_check.json    every response class carries the limitation (GB.12)

Every artefact records the commit and database hash it was produced from, so the gate
can reject a stale measurement rather than re-reading it as fresh (review F12).

Run:  uv run python scripts/verify_api.py
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from repindex.api import app as app_module
from repindex.api import queries
from repindex.api.app import app
from repindex.api.models import DISCLAIMER_MARKER
from repindex.common.db import connect_readonly
from repindex.common.provenance import (
    CommitObserver,
    DatabaseChangedError,
    PendingWALError,
    attest_identity,
    data_version,
    provenance,
    require_no_commits,
    require_unchanged,
)
from repindex.common.provenance import file_sha256 as sha256_file

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS = REPO_ROOT / "reports"
DB = REPO_ROOT / "data" / "repindex.db"
PROBE = REPO_ROOT / "scripts" / "determinism_probe.py"

FUZZ_INPUTS = [
    "",
    "0x",
    "0xzzzz",
    "0x" + "1" * 39,
    "0x" + "1" * 41,
    "not-an-address",
    "../../etc/passwd",
    "'; DROP TABLE settlements; --",
    "0x0000000000000000000000000000000000000000 ",
    "0X0000000000000000000000000000000000000000",
    "0x" + "g" * 40,
    "0x833589fcd6eDb6e08f4c7c32d4f71b54bda02913",  # wrong EIP-55 checksum
    "0x" + "0" * 40,
    "did:web:example.com",
    "did:pkh:eip155:not-a-chain:0x1234",
]

ENTITY_FUZZ_INPUTS = [
    "abc",
    "-1",
    "1.5",
    "0",
    str(2**63 - 1),
    "99999999999999999999999999999999999999",
    "'; DROP TABLE entities; --",
    "%00",
]


def _probe(identifier: str) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, str(PROBE), identifier],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode not in (0, 1):
        return {"error": (proc.stderr or proc.stdout).strip()[-500:]}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]
    except (json.JSONDecodeError, IndexError):
        return {"error": "probe produced no JSON"}


def main() -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    # O2/P2: attest before the first read and confirm before returning, with an
    # observer open across the hash and the whole probe run. The API reads the
    # database through the app and through determinism subprocesses that no single
    # read bracket sees, so the observed window covers the entire run.
    observer = CommitObserver(DB)
    try:
        attested = attest_identity(DB)
    except PendingWALError as exc:
        observer.close()
        print(f"{exc} The API evidence is not written from a state its provenance cannot name.")
        return 1
    client = TestClient(app)

    # Q1: every artefact is held in memory until the identity is confirmed at the
    # end. A refused run must not leave files that individually pass the gate's
    # freshness checks while the producer exits 1.
    artifacts: dict[str, str] = {}

    # --- OpenAPI ---
    schema = app.openapi()
    artifacts["openapi.json"] = json.dumps(schema, indent=2)
    print(f"openapi: {len(schema.get('paths', {}))} paths")

    conn = connect_readonly(DB)
    conn.row_factory = sqlite3.Row
    version_before = data_version(conn)
    row = conn.execute(
        "SELECT seller FROM features ORDER BY settlement_count DESC LIMIT 1"
    ).fetchone()
    other = conn.execute(
        "SELECT seller FROM features ORDER BY settlement_count DESC LIMIT 1 OFFSET 1"
    ).fetchone()
    sample = row["seller"]
    negative = other["seller"]
    version_after = data_version(conn)
    conn.close()
    try:
        meta = provenance(DB, attested)
        require_no_commits(DB, version_before, version_after)
    except (PendingWALError, DatabaseChangedError) as exc:
        observer.close()
        print(f"{exc} The API evidence is not written from a state it cannot name.")
        return 1

    # --- determinism: two fresh processes, plus a negative control ---
    first = _probe(sample)
    second = _probe(sample)
    control = _probe(negative)
    determinism = {
        **meta,
        "identifier": sample,
        "negative_control_identifier": negative,
        "first_hash": first.get("sha256"),
        "second_hash": second.get("sha256"),
        "negative_control_hash": control.get("sha256"),
        "first_status": first.get("status"),
        "second_status": second.get("status"),
        "errors": [p.get("error") for p in (first, second, control) if p.get("error")],
    }
    artifacts["determinism_B.json"] = json.dumps(determinism, indent=2)
    print(
        f"determinism: {str(first.get('sha256'))[:16]} vs {str(second.get('sha256'))[:16]} "
        f"(control {str(control.get('sha256'))[:16]})"
    )

    # --- fuzz: identifiers and entity ids must not produce a 500 ---
    crashes = 0
    leak = 0
    details: list[dict[str, Any]] = []
    cases: list[str] = [f"/v1/score/{candidate}" for candidate in FUZZ_INPUTS] + [
        f"/v1/entity/{candidate}" for candidate in ENTITY_FUZZ_INPUTS
    ]
    for path in cases:
        try:
            response = client.get(path)
        except Exception as exc:  # noqa: BLE001 - a raised exception is the crash
            crashes += 1
            details.append({"path": path, "error": type(exc).__name__})
            continue
        body = response.text
        if response.status_code >= 500:
            crashes += 1
            details.append({"path": path, "status": response.status_code})
        if "Traceback (most recent call last)" in body or 'File "' in body:
            leak += 1
        if response.status_code not in (200, 404, 422, 429, 304):
            details.append({"path": path, "unexpected_status": response.status_code})

    # --- write probe: the API refuses writes and the database does not change ---
    before = sha256_file(DB)
    write_statuses: list[int] = []
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        write_statuses.append(client.request(method, f"/v1/score/{sample}").status_code)
        write_statuses.append(client.request(method, "/v1/entity/1").status_code)
    after = sha256_file(DB)
    writes_attempted = 0 if before == after else 1

    fuzz = {
        **meta,
        "cases": len(cases),
        "crashes": crashes,
        "writes_attempted": writes_attempted,
        "stack_traces_leaked": leak,
        "details": details,
    }
    artifacts["fuzz_identifier.json"] = json.dumps(fuzz, indent=2)
    artifacts["write_probe.json"] = json.dumps(
        {
            **meta,
            "statuses": write_statuses,
            "all_refused": all(s == 405 for s in write_statuses),
            "db_hash_before": before,
            "db_hash_after": after,
            "db_unchanged": before == after,
        },
        indent=2,
    )
    print(
        f"fuzz: {len(cases)} cases, {crashes} crashes, {leak} stack traces; "
        f"writes refused {all(s == 405 for s in write_statuses)}"
    )

    # --- disclaimer sweep: every response class ---
    checked = 0
    missing: list[str] = []
    bodyless: list[str] = []
    labels: list[str] = []

    def check(response: Any, label: str) -> None:
        nonlocal checked
        checked += 1
        labels.append(label)
        if response.status_code == 304:
            bodyless.append(label)
            return
        if DISCLAIMER_MARKER not in response.text:
            missing.append(label)

    check(client.get("/v1/health"), "GET /v1/health")
    check(client.get("/v1/methodology"), "GET /v1/methodology")
    check(client.get(f"/v1/score/{sample}"), f"GET /v1/score/{sample}")
    check(client.get("/v1/score/0x" + "f" * 40), "GET /v1/score (unknown)")
    check(client.get("/v1/nonexistent"), "GET /v1/nonexistent (404)")
    check(client.post("/v1/health"), "POST /v1/health (405)")
    check(client.get("/v1/score/not-an-address"), "GET /v1/score/bad (422)")
    check(client.get("/v1/entity/abc"), "GET /v1/entity/bad (422)")
    check(
        client.get("/v1/entity/99999999999999999999999999999999999999"),
        "GET /v1/entity/huge (422)",
    )

    # 304 is bodyless by definition. Request it explicitly so the exception is measured.
    fresh = client.get(f"/v1/score/{sample}")
    conditional = client.get(
        f"/v1/score/{sample}", headers={"If-None-Match": fresh.headers.get("etag", "")}
    )
    check(conditional, "GET /v1/score (304)")

    # 429: force the limiter with a positive limit, consume the one slot, then check
    # the refusal. This used to set the limit to 0 and rely on the off-switch bug
    # (G3) to produce the 429; the limit is now restored afterwards for the 500 probe.
    original_limit = app_module.RATE_LIMIT_PER_MINUTE
    app_module.RATE_LIMIT_PER_MINUTE = 1
    app_module._rate_buckets.clear()
    try:
        client.get("/v1/health")  # consumes the single slot for this client
        check(client.get("/v1/health"), "GET /v1/health (429)")
    finally:
        app_module.RATE_LIMIT_PER_MINUTE = original_limit
        app_module._rate_buckets.clear()

    # 500: make the query layer fail for one request. The handler must keep the
    # disclaimer and leak no stack trace.
    original_fetch = queries.fetch_score

    def boom(_conn: Any, _identifier: str) -> None:
        raise RuntimeError("synthetic failure for the disclaimer sweep")

    queries.fetch_score = boom  # type: ignore[assignment]
    try:
        tolerant = TestClient(app, raise_server_exceptions=False)
        check(tolerant.get(f"/v1/score/{sample}"), "GET /v1/score (500)")
    finally:
        queries.fetch_score = original_fetch

    disclaimer = {
        **meta,
        "marker": DISCLAIMER_MARKER,
        "responses_checked": checked,
        "missing": missing,
        "bodyless_expected": bodyless,
        "labels": labels,
    }
    artifacts["disclaimer_check.json"] = json.dumps(disclaimer, indent=2)
    print(
        f"disclaimer: {checked - len(missing)}/{checked} response classes carry the marker "
        f"(bodyless: {len(bodyless)})"
    )

    try:
        require_unchanged(DB, attested)
        observer.require_quiet()
    except (PendingWALError, DatabaseChangedError) as exc:
        print(f"{exc} The API evidence is not written from a state it cannot name.")
        return 1
    finally:
        observer.close()

    # Q1: only now, with the identity confirmed and no foreign commit observed across
    # the hash and the whole probe run, may the evidence reach disk.
    for name, text in artifacts.items():
        (REPORTS / name).write_text(text, encoding="utf-8")

    return 0 if not missing and not crashes and not leak and writes_attempted == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
