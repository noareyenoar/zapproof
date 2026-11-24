"""Minimal read-only JSON-RPC client for Base, with endpoint fallback.

Zero cost, no signup. Every endpoint below was measured from this machine on
2026-10-16 during Phase 0 task 0.3; see `reports/source_probe.json`.

Deliberately NOT in the default list — `https://mainnet.base.org`, the endpoint
most people reach for first. Measured 2026-10-16: its `eth_getLogs` returns
`{"code": -32011, "message": "request limit reached"}` for ranges as small as
1,000 blocks, while `eth_blockNumber` on the same endpoint answers normally. So
it is not a general rate limit; `eth_getLogs` specifically is rationed below what
ingestion needs. Retrying it just wastes time.

Nothing here writes. There is no signing code and no private key handling
anywhere in this module, by design (prompt Section 2).
"""

from __future__ import annotations

import os
import random
import time
from pathlib import Path
from typing import Any

import httpx

# Retry policy for the public endpoints. Prompt Section 3.3 requires backoff with
# jitter, and measurement made the requirement concrete: a sustained backfill hits
# 429 at Tenderly and 413 at Thirdweb within about a hundred windows.
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 6
BACKOFF_BASE_SECONDS = 1.5
BACKOFF_MAX_SECONDS = 30.0

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env"

# Measured 2026-10-16, in preference order. Both cap a single eth_getLogs at
# 1,000 blocks and both serve archive-depth queries.
DEFAULT_BASE_RPC_URLS: tuple[str, ...] = (
    "https://gateway.tenderly.co/public/base",
    "https://8453.rpc.thirdweb.com",
)

# Base produces a block every 2.000 s (measured: 2000 blocks in exactly 4000 s).
SECONDS_PER_BLOCK = 2.0
BLOCKS_PER_DAY = int(86_400 / SECONDS_PER_BLOCK)

# Both measured endpoints reject eth_getLogs ranges wider than this.
MAX_BLOCK_RANGE = 1_000


class RpcError(RuntimeError):
    """Raised when every configured endpoint failed, or the RPC returned an error."""


def _urls_from_env_file() -> tuple[str, ...]:
    if not ENV_FILE.is_file():
        return ()
    found: list[str] = []
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("#") or not line.startswith("BASE_RPC_URL="):
            continue
        value = line.split("=", 1)[1].strip().strip("\"'")
        if value:
            found.append(value)
    return tuple(found)


def base_rpc_urls() -> tuple[str, ...]:
    """Endpoints to try, in order. An explicit BASE_RPC_URL wins."""
    override = os.environ.get("BASE_RPC_URL", "").strip()
    if override:
        return (override,)
    return _urls_from_env_file() or DEFAULT_BASE_RPC_URLS


def rpc_call(
    method: str,
    params: list[Any],
    *,
    urls: tuple[str, ...] | None = None,
    timeout: float = 30.0,
) -> Any:
    """Call a JSON-RPC method, falling through to the next endpoint on failure.

    Raises RpcError only when every endpoint failed. The per-endpoint errors are
    collected into the message so a failure is diagnosable rather than opaque.
    """
    candidates = urls if urls is not None else base_rpc_urls()
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    failures: list[str] = []

    for url in candidates:
        for attempt in range(MAX_RETRIES):
            try:
                with httpx.Client(timeout=timeout) as client:
                    response = client.post(url, json=payload)
                    response.raise_for_status()
                    body = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                # Prompt Section 3.3 requires backoff with jitter. Measured 2026-10-28:
                # a long backfill reliably hits 429 at both public endpoints, and without
                # a retry the whole run stops at whichever window happens to be unlucky.
                if status in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                    # Exponential, capped, jittered. The jitter matters as much as the
                    # backoff: several requests failing together must not retry together.
                    delay = min(BACKOFF_BASE_SECONDS * (2**attempt), BACKOFF_MAX_SECONDS)
                    time.sleep(delay * (0.5 + random.random()))
                    continue
                failures.append(f"{url}: {type(exc).__name__}: {exc}")
                break

            if isinstance(body, dict) and body.get("error"):
                failures.append(f"{url}: rpc error {body['error']}")
                break

            if isinstance(body, dict) and "result" in body:
                return body["result"]
            failures.append(f"{url}: malformed response")
            break

    raise RpcError(f"{method} failed on every endpoint:\n  " + "\n  ".join(failures))


def get_logs(
    *,
    address: str | None = None,
    topics: list[Any] | None = None,
    from_block: int,
    to_block: int,
    urls: tuple[str, ...] | None = None,
    chunk_size: int = MAX_BLOCK_RANGE,
    pause: float = 0.0,
) -> list[dict[str, Any]]:
    """Fetch logs over a block range, chunked to respect the measured per-call cap.

    `address=None` omits the address filter, which is what the token census needs:
    filtering to a known token would make "only that token appears" true by
    construction instead of by measurement. That is also why the chunk default is
    conservative — an unfiltered query returns far more than a filtered one.

    Blocks are walked oldest-first so a partial failure leaves a contiguous,
    resumable prefix rather than a hole in the middle.
    """
    if to_block < from_block:
        return []

    if chunk_size > MAX_BLOCK_RANGE:
        raise ValueError(
            f"chunk_size {chunk_size} exceeds the measured per-call cap "
            f"of {MAX_BLOCK_RANGE} blocks on the public endpoints"
        )

    collected: list[dict[str, Any]] = []
    cursor = from_block
    while cursor <= to_block:
        chunk_end = min(cursor + chunk_size - 1, to_block)
        query: dict[str, Any] = {"fromBlock": hex(cursor), "toBlock": hex(chunk_end)}
        if address is not None:
            query["address"] = address
        if topics is not None:
            query["topics"] = topics
        try:
            result = rpc_call("eth_getLogs", [query], urls=urls)
        except RpcError as exc:
            # The endpoint caps a single query at 20,000 results. Event volume grows,
            # so a chunk size that worked last week can stop working — measured
            # 2026-10-23, the unfiltered AuthorizationUsed rate rose from about 2.1
            # per block to about 8-9 in six days, and a 1,000-block chunk that had
            # been fine started exceeding the cap. Halving and retrying keeps the
            # caller's chunk size a preference rather than a hard requirement.
            text = str(exc)
            # 413 joins the list: Thirdweb rejects a response that is too large for the
            # same underlying reason as the 20,000-result cap — the range asks for more
            # logs than one response may carry. Measured 2026-10-28, both fired during
            # the seven-day backfill.
            over_cap = (
                "20000" in text
                or "more than" in text.lower()
                or "413" in text
                or "payload too large" in text.lower()
            )
            if not over_cap or chunk_end <= cursor:
                raise
            width = chunk_end - cursor + 1
            half = max(1, width // 2)
            collected.extend(
                get_logs(
                    address=address,
                    topics=topics,
                    from_block=cursor,
                    to_block=cursor + half - 1,
                    urls=urls,
                    chunk_size=half,
                    pause=pause,
                )
            )
            collected.extend(
                get_logs(
                    address=address,
                    topics=topics,
                    from_block=cursor + half,
                    to_block=chunk_end,
                    urls=urls,
                    chunk_size=half,
                    pause=pause,
                )
            )
            cursor = chunk_end + 1
            continue
        if not isinstance(result, list):
            raise RpcError(f"eth_getLogs returned {type(result).__name__}, expected a list")
        collected.extend(result)
        cursor = chunk_end + 1
        if pause:
            time.sleep(pause)

    return collected


def block_number(*, urls: tuple[str, ...] | None = None) -> int:
    result = rpc_call("eth_blockNumber", [], urls=urls)
    if not isinstance(result, str):
        raise RpcError(f"eth_blockNumber returned {type(result).__name__}, expected a hex string")
    return int(result, 16)


def eth_call(to: str, data: str, *, block: str = "latest") -> str:
    """Read-only contract call. Returns the raw hex result."""
    result = rpc_call("eth_call", [{"to": to, "data": data}, block])
    if not isinstance(result, str):
        raise RpcError(f"eth_call returned {type(result).__name__}, expected hex")
    return result


def get_code(address: str, *, block: str = "latest") -> str:
    result = rpc_call("eth_getCode", [address, block])
    if not isinstance(result, str):
        raise RpcError(f"eth_getCode returned {type(result).__name__}, expected hex")
    return result
