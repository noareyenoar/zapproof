"""Sustained load test for the score API (prompt task B1, gate check GB.3).

Starts the real uvicorn server in a background thread, then drives it with a fixed
number of concurrent workers for the configured duration. Reports achieved throughput,
cached p95 latency and the error rate, and records the machine specification alongside
the result — a latency figure without a machine is not a measurement.

Writes reports/loadtest_B.json.

Run:  uv run python scripts/loadtest.py
"""

from __future__ import annotations

import asyncio
import json
import platform
import socket
import statistics
import threading
import time
from pathlib import Path

import httpx
import uvicorn
import yaml

from repindex.api.app import app
from repindex.common.db import connect_readonly
from repindex.common.provenance import (
    CommitObserver,
    DatabaseChangedError,
    PendingWALError,
    attest_identity,
    data_version,
    provenance,
    require_no_commits,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
THRESHOLDS = REPO_ROOT / "config" / "thresholds.yaml"
OUT = REPO_ROOT / "reports" / "loadtest_B.json"
DB = REPO_ROOT / "data" / "repindex.db"

CONCURRENCY = 8


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def machine_spec() -> str:
    processor = platform.processor() or "unknown"
    return f"{platform.system()} {platform.release()} / {processor} / {platform.python_version()}"


def main() -> int:
    thresholds = yaml.safe_load(THRESHOLDS.read_text(encoding="utf-8")) or {}
    phase_b = thresholds.get("phaseB", {})
    target_rps = float(phase_b.get("api_sustained_rps", 20))
    duration = float(phase_b.get("api_load_test_seconds", 120))
    if target_rps <= 0 or duration <= 0:
        print("thresholds set no load to generate")
        return 1

    # P2: the observer stays open across the whole run — attestation hash, seller
    # read, and the 120 s of measured traffic — so a commit made by any writer during
    # the run is refused even if its bytes are restored before the confirming hash.
    observer = CommitObserver(DB)
    try:
        attested = attest_identity(DB)
    except PendingWALError as exc:
        observer.close()
        print(f"{exc} The load test is not written from a state its provenance cannot name.")
        return 1
    version_before = -1
    version_after = -1
    conn = connect_readonly(DB)
    try:
        version_before = data_version(conn)
        sellers = [
            row[0]
            for row in conn.execute(
                "SELECT seller FROM features ORDER BY settlement_count DESC LIMIT 200"
            )
        ]
        version_after = data_version(conn)
    finally:
        conn.close()
    try:
        require_no_commits(DB, version_before, version_after)
    except DatabaseChangedError as exc:
        print(f"{exc} The load test is not written from a state it cannot name.")
        return 1
    if not sellers:
        observer.close()
        print("no sellers to query")
        return 1

    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/v1/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        observer.close()
        print("server did not become ready")
        return 1

    latencies: list[float] = []
    request_times: list[float] = []
    errors = 0
    counts = {"ok": 0}

    # Pace the client to the target rate.
    #
    # The earlier version drove ~295 attempts/s against a 50 rps limiter, so 83% of
    # requests were refused with 429 and the script excluded 429s from BOTH the
    # throughput numerator and the error denominator. It then reported "50.00 rps,
    # 0.00% errors" — which measured the limiter, not the service. The independent
    # review caught it. A load test must generate the load it claims to measure.
    #
    # The generator is open-loop: requests are scheduled at fixed absolute times and
    # a slow response does not consume the next slot. A closed loop with eight
    # workers delivered 18.7 rps when asked for 20, because each response slower than
    # the 50 ms slot quietly cancelled a request the run promised to send. That is
    # under-generation, which is the same defect F2 found one layer down.
    #
    # Timers on Windows fire with ~15 ms granularity, so each wake issues every
    # request whose scheduled time has passed (the catch-up loop below). Over the
    # window the average is the target rate; a single wake may carry two or three.
    interval = 1.0 / target_rps if target_rps else 0.0
    outstanding: set[asyncio.Task[None]] = set()

    async def issue(client: httpx.AsyncClient, identifier: str) -> None:
        nonlocal errors
        request_times.append(time.monotonic())
        started = time.perf_counter()
        try:
            response = await client.get(f"/v1/score/{identifier}")
            latencies.append((time.perf_counter() - started) * 1000)
            if response.status_code == 200:
                counts["ok"] += 1
            elif response.status_code == 429:
                # A 429 at the *target* rate is a real failure of the service, not
                # noise to be filtered away.
                counts["throttled"] = counts.get("throttled", 0) + 1
                errors += 1
            else:
                errors += 1
        except httpx.HTTPError:
            errors += 1

    async def drive() -> None:
        # Connect every client before the clock starts, so connection set-up is not
        # counted as load-generation time.
        clients = [httpx.AsyncClient(base_url=base, timeout=10) for _ in range(CONCURRENCY)]
        start_at = time.monotonic() + 0.2
        stop_at = start_at + duration
        next_at = start_at
        index = 0
        try:
            while True:
                now = time.monotonic()
                if now >= stop_at:
                    break
                if now < start_at:
                    await asyncio.sleep(start_at - now)
                    continue
                while next_at <= now and next_at < stop_at:
                    client = clients[index % len(clients)]
                    task = asyncio.create_task(issue(client, sellers[index % len(sellers)]))
                    outstanding.add(task)
                    task.add_done_callback(outstanding.discard)
                    index += 1
                    next_at += interval
                # Bound the queue. If the service is slower than the target rate this
                # is where the pressure shows, and the p95 records it; dropping the
                # queue instead would hide the very thing the run measures.
                if len(outstanding) >= 512:
                    await asyncio.wait(outstanding, return_when=asyncio.FIRST_COMPLETED)
                await asyncio.sleep(max(0.001, next_at - time.monotonic()))
            if outstanding:
                await asyncio.gather(*outstanding)
        finally:
            await asyncio.gather(*(c.aclose() for c in clients))

    started = time.time()
    asyncio.run(drive())
    elapsed = time.time() - started

    server.should_exit = True
    thread.join(timeout=10)

    total = counts["ok"] + errors
    # Throughput is requests over the window in which requests were actually issued,
    # so client start-up and teardown do not dilute the service's rate. Under-
    # generation is still caught by the request-count floor below, which uses the
    # wall-clock duration.
    generating = (
        max(request_times) - min(request_times) if len(request_times) > 1 else 0.0
    )
    p95 = (
        statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 20 else max(latencies or [0])
    )
    try:
        observer.require_quiet()
        prov = provenance(DB, attested)
    except (PendingWALError, DatabaseChangedError) as exc:
        print(f"{exc} The load test is not written from a state it cannot name.")
        return 1
    finally:
        observer.close()
    result = {
        **prov,
        "target_rps": target_rps,
        "duration_seconds": round(elapsed, 2),
        "generating_seconds": round(generating, 2),
        "concurrency": CONCURRENCY,
        "requests": total,
        "achieved_rps": round(total / generating, 2) if generating else 0.0,
        "p95_ms": round(p95, 2),
        "median_ms": round(statistics.median(latencies), 2) if latencies else 0.0,
        "error_rate": round(errors / total, 6) if total else 1.0,
        "throttled": counts.get("throttled", 0),
        "machine": machine_spec(),
        "note": (
            "The client is paced to the target rate, so a 429 is a service failure and "
            "counts as an error. An earlier version drove far above the limit and "
            "silently excluded the throttled responses from both sides of the ratio."
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
