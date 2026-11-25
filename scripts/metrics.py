"""Privacy-preserving usage metrics (prompt task B7; gate check GB.17).

Reports the number of unique external callers over the market window, alongside
per-endpoint counts, status classes, latency percentiles and agent classes. Zero is a
valid answer: it means the service has not been exposed, not that the command failed.

Owner addresses are excluded so the owner's own checks do not count as market demand.
Configure them with `USAGE_OWNER_IPS` (comma-separated) or repeated `--owner-ip`.

A unique caller is counted only when it made a successful score request or MCP tool
call. Health checks, 404 scans, 4xx answers and static traffic appear in
`requests_external` but never in `unique_external_callers`; otherwise the market gate
could be satisfied without anyone asking for a single score.

Run:  uv run python scripts/metrics.py
      make metrics
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from repindex.api.usage import hash_ip
from repindex.common.db import connect_readonly
from repindex.common.provenance import file_sha256, git_commit

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "usage.db"
OUT = REPO_ROOT / "reports" / "usage_metrics.json"
THRESHOLDS = REPO_ROOT / "config" / "thresholds.yaml"


def _market_window_days() -> int:
    data = yaml.safe_load(THRESHOLDS.read_text(encoding="utf-8")) or {}
    return int(data.get("phaseB", {}).get("market_window_days", 30))


def _owner_ips(cli_values: list[str]) -> list[str]:
    configured = os.environ.get("USAGE_OWNER_IPS", "")
    from_env = [item.strip() for item in configured.split(",") if item.strip()]
    return sorted(set(from_env + cli_values))


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return round(ordered[index], 2)


def _status_class(status: int) -> str:
    return f"{status // 100}xx"


# A caller counts toward the market gate only if it actually asked for a score or
# called the tool. Before 2026-11-16 every external row counted, so a port scanner,
# an uptime monitor hitting /v1/health, or a 404 crawler could satisfy the gate
# without a single real lookup.
SCORE_PATH = "/v1/score/{identifier}"
TOOL_CALL_PREFIX = "/mcp/tools/call"


def _is_meaningful(path: str, status: int) -> bool:
    """A successful score request or MCP tool call — the only market signal."""
    if not 200 <= status < 300:
        return False
    return path == SCORE_PATH or path.startswith(TOOL_CALL_PREFIX)


def _blank_result(
    *, window_days: int, owner_ips: list[str], usage_source: str
) -> dict[str, Any]:
    window_start = int(datetime.now(UTC).timestamp()) - window_days * 86400
    return {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": git_commit(),
        "window_days": window_days,
        "window_start": datetime.fromtimestamp(window_start, UTC).isoformat(timespec="seconds"),
        "usage_source": usage_source,
        "unique_external_callers": 0,
        "requests_total": 0,
        "requests_external": 0,
        "meaningful_external_requests": 0,
        "excluded": {
            "internal_requests": 0,
            "owner_requests": 0,
            "owner_ips_configured": len(owner_ips),
        },
        "salt_rotations_in_window": 0,
        "by_endpoint": {},
        "by_status": {},
        "latency_ms": {"p50": None, "p95": None},
        "agent_classes": {},
        "usage_db_sha256": None,
    }


def _summarize(
    result: dict[str, Any],
    rows: Iterable[Mapping[str, Any]],
    owner_hashes: set[tuple[int, str]],
) -> None:
    """Accumulate one report from rows shaped like the usage table."""
    callers: set[tuple[int, str]] = set()
    by_endpoint: dict[str, dict[str, int]] = {}
    by_status: dict[str, int] = {}
    agent_classes: dict[str, int] = {}
    latencies: list[float] = []
    internal = 0
    owner = 0
    salt_ids: set[int] = set()

    for row in rows:
        result["requests_total"] += 1
        status = int(row["status"])
        by_status[_status_class(status)] = by_status.get(_status_class(status), 0) + 1
        agent_classes[row["agent_class"]] = agent_classes.get(str(row["agent_class"]), 0) + 1
        latencies.append(float(row["latency_ms"]))

        path = str(row["path"])
        meaningful = _is_meaningful(path, status)
        endpoint = by_endpoint.setdefault(path, {"requests": 0, "external": 0, "meaningful": 0})
        endpoint["requests"] += 1
        if meaningful:
            endpoint["meaningful"] += 1

        salt_id = int(row["salt_id"])
        salt_ids.add(salt_id)
        if int(row["is_internal"]):
            internal += 1
            continue
        if (salt_id, str(row["ip_hash"])) in owner_hashes:
            owner += 1
            continue

        result["requests_external"] += 1
        endpoint["external"] += 1
        if not meaningful:
            continue
        result["meaningful_external_requests"] += 1
        callers.add((salt_id, str(row["ip_hash"])))

    result["unique_external_callers"] = len(callers)
    result["excluded"] = {
        "internal_requests": internal,
        "owner_requests": owner,
        "owner_ips_configured": result["excluded"]["owner_ips_configured"],
    }
    result["salt_rotations_in_window"] = len(salt_ids)
    result["by_endpoint"] = dict(sorted(by_endpoint.items()))
    result["by_status"] = dict(sorted(by_status.items()))
    result["latency_ms"] = {
        "p50": _percentile(latencies, 0.50),
        "p95": _percentile(latencies, 0.95),
    }
    result["agent_classes"] = dict(sorted(agent_classes.items()))

    if result["salt_rotations_in_window"] > 1:
        result["note"] = (
            "the window spans more than one salt, so a caller seen on both sides is "
            "counted once per salt; the figure is an upper bound"
        )
    elif result["requests_external"] and not result["meaningful_external_requests"]:
        result["note"] = (
            "external requests were seen, but none was a successful score or tool call, "
            "so the market count is 0"
        )


def collect(db_path: Path, *, window_days: int, owner_ips: list[str]) -> dict[str, Any]:
    result = _blank_result(
        window_days=window_days, owner_ips=owner_ips, usage_source="sqlite"
    )
    result["usage_db_sha256"] = file_sha256(db_path) if db_path.is_file() else None

    if not db_path.is_file():
        result["note"] = (
            "no usage database yet — the service has not recorded a request, so the "
            "caller count is 0"
        )
        return result

    conn = connect_readonly(db_path)
    conn.row_factory = sqlite3.Row
    try:
        salts = {
            int(row["id"]): str(row["salt"])
            for row in conn.execute("SELECT id, salt FROM salts").fetchall()
        }
        owner_hashes = {
            (salt_id, hash_ip(ip, salt))
            for salt_id, salt in salts.items()
            for ip in owner_ips
        }
        rows = conn.execute(
            "SELECT at, path, status, latency_ms, agent_class, ip_hash, salt_id, is_internal "
            "FROM requests WHERE at >= ?",
            (int(datetime.now(UTC).timestamp()) - window_days * 86400,),
        ).fetchall()
    finally:
        conn.close()

    _summarize(result, rows, owner_hashes)
    return result


def _usage_payload(entry: Any) -> dict[str, Any] | None:
    """Find one usage line inside a Cloud Logging entry or a raw JSON line."""
    if not isinstance(entry, dict):
        return None
    if entry.get("event") == "zapproof_usage":
        return entry

    payload = entry.get("jsonPayload")
    if isinstance(payload, dict):
        if payload.get("event") == "zapproof_usage":
            return payload
        message = payload.get("message")
        if isinstance(message, str):
            try:
                parsed = json.loads(message)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("event") == "zapproof_usage":
                return parsed

    text = entry.get("textPayload")
    if isinstance(text, str):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict) and parsed.get("event") == "zapproof_usage":
            return parsed
    return None


def _load_log_export(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = [json.loads(line) for line in text.splitlines() if line.strip()]
    entries = data if isinstance(data, list) else [data]
    return [payload for entry in entries if (payload := _usage_payload(entry)) is not None]


def collect_log_export(
    path: Path,
    *,
    window_days: int,
    owner_ips: list[str],
    usage_salt: str | None = None,
) -> dict[str, Any]:
    """Report from the JSON lines a serverless host writes to Cloud Logging.

    Export them first:
        gcloud logging read 'jsonPayload.event="zapproof_usage"' \\
          --project PROJECT --format=json --limit=100000 > usage-log.json

    Owner IPs can only be excluded when the same `USAGE_SALT` the service used is
    available here; without it the report says so rather than silently counting them.
    """
    result = _blank_result(
        window_days=window_days, owner_ips=owner_ips, usage_source="log-export"
    )
    if not path.is_file():
        result["note"] = "no log export file"
        return result

    rows = _load_log_export(path)
    window_start = int(datetime.now(UTC).timestamp()) - window_days * 86400
    rows = [row for row in rows if int(row.get("at", 0)) >= window_start]

    owner_hashes: set[tuple[int, str]] = set()
    if owner_ips and usage_salt:
        owner_hashes = {(1, hash_ip(ip, usage_salt)) for ip in owner_ips}
    elif owner_ips:
        result["note"] = (
            "owner IPs were configured but no USAGE_SALT was provided, so they could "
            "not be matched; the caller count may include the owner"
        )

    normalized = [
        {
            "at": int(row.get("at", 0)),
            "path": str(row.get("path", "")),
            "status": int(row.get("status", 0)),
            "latency_ms": float(row.get("latency_ms", 0.0)),
            "agent_class": str(row.get("agent_class", "unknown")),
            "ip_hash": str(row.get("ip_hash", "")),
            "salt_id": int(row.get("salt_id", 1)),
            "is_internal": int(row.get("is_internal", 0)),
            "salt_source": str(row.get("salt_source", "env")),
        }
        for row in rows
    ]
    _summarize(result, normalized, owner_hashes)

    if any(row["salt_source"] == "ephemeral" for row in normalized):
        result["note"] = (
            "some instances used an ephemeral salt (USAGE_SALT was unset), so the same "
            "caller may be counted more than once; the figure is an upper bound"
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report privacy-preserving usage metrics")
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("USAGE_DB", DEFAULT_DB)))
    parser.add_argument(
        "--log-export",
        type=Path,
        default=None,
        help="JSON export of Cloud Logging entries (serverless deployments)",
    )
    parser.add_argument(
        "--usage-salt",
        default=os.environ.get("USAGE_SALT"),
        help="the USAGE_SALT the service used; needed to exclude --owner-ip from logs",
    )
    parser.add_argument("--window-days", type=int, default=_market_window_days())
    parser.add_argument("--owner-ip", action="append", default=[])
    parser.add_argument("--json-out", type=Path, default=OUT)
    args = parser.parse_args(argv)

    if args.log_export is not None:
        result = collect_log_export(
            args.log_export,
            window_days=args.window_days,
            owner_ips=_owner_ips(args.owner_ip),
            usage_salt=args.usage_salt,
        )
    else:
        result = collect(
            args.db, window_days=args.window_days, owner_ips=_owner_ips(args.owner_ip)
        )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    print(
        f"unique external callers: {result['unique_external_callers']} "
        f"over {result['window_days']} days "
        f"({result['requests_external']} external request(s) of {result['requests_total']})"
    )
    try:
        shown = args.json_out.relative_to(REPO_ROOT)
    except ValueError:
        shown = args.json_out
    print(f"written: {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
