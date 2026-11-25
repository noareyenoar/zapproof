"""Privacy-preserving usage metrics (prompt task B7; review F8).

What is recorded, per request: the route template, status, latency, an agent class and
a salted hash of the caller IP. What is never recorded: the raw IP, the query string,
headers, request bodies or any identifier a caller supplied. The route template is
used rather than the path so `/v1/score/0x…` is logged as `/v1/score/{identifier}`.

The salt rotates so hashes from different periods cannot be joined. Rotation defaults
to 90 days, deliberately longer than the 30-day market window: a shorter period would
change every caller's hash mid-window and make "unique callers" count the same caller
twice. The rotation is recorded, and `scripts/metrics.py` reports when a window spans
more than one salt so the figure is read as an upper bound.

This database is separate from `data/repindex.db`. The score API stays read-only on
the score database; this module writes telemetry only, and a failed write is logged
once per exception class by the middleware, never raised, so metrics can never break
a score response.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data/usage.db")
DEFAULT_ROTATION_DAYS = 90

TRUST_PROXY_ENV = "ZAPPROOF_TRUST_PROXY"
SINK_ENV = "USAGE_SINK"
SALT_ENV = "USAGE_SALT"

SCHEMA = """
CREATE TABLE IF NOT EXISTS salts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    salt       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          INTEGER NOT NULL,
    path        TEXT NOT NULL,
    status      INTEGER NOT NULL,
    latency_ms  REAL NOT NULL,
    agent_class TEXT NOT NULL,
    ip_hash     TEXT NOT NULL,
    salt_id     INTEGER NOT NULL REFERENCES salts(id),
    is_internal INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_at ON requests (at);
CREATE INDEX IF NOT EXISTS idx_usage_hash ON requests (ip_hash);
"""

_AGENT_PATTERNS = (
    "agent",
    "claude",
    "anthropic",
    "gpt",
    "openai",
    "llm",
    "mcp",
    "bot",
    "crawler",
    "spider",
)
_BROWSER_PATTERNS = ("mozilla", "chrome", "safari", "firefox", "edg/")
_CLI_PATTERNS = ("curl", "wget", "httpie", "python-requests", "httpx", "aiohttp", "go-http-client")


def classify_agent(user_agent: str | None) -> str:
    """A coarse class, never the raw User-Agent string."""
    if not user_agent:
        return "unknown"
    lowered = user_agent.lower()
    # Agents first: "ChatGPT-User" also contains no browser hint, but a headless
    # browser driven by an agent should still count as an agent, not a reader.
    if any(pattern in lowered for pattern in _AGENT_PATTERNS):
        return "agent"
    if any(pattern in lowered for pattern in _BROWSER_PATTERNS):
        return "browser"
    if any(pattern in lowered for pattern in _CLI_PATTERNS):
        return "cli"
    return "unknown"


def is_external(ip: str) -> bool:
    """True only for a routable public address.

    Loopback, private ranges, link-local and non-IP values (the test client reports
    the literal host `testclient`) are internal: they must not inflate the market
    gate's caller count.
    """
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return address.is_global


def hash_ip(ip: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()


def _host_only(value: str) -> str:
    """Strip a port from a forwarded address without breaking a bare IPv6 literal."""
    value = value.strip()
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end != -1 else value
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value


def client_ip(scope: Mapping[str, Any]) -> str:
    """The caller address, trusting the proxy only when explicitly configured.

    On Cloud Run the process sees the Google front end as its peer, not the caller.
    The front end appends the address it saw to `X-Forwarded-For`, so the **last**
    entry is the one added by the trusted edge; anything earlier may have been
    supplied by the caller and must not be trusted. A direct (proxyless) deployment
    leaves `ZAPPROOF_TRUST_PROXY` unset, so a spoofed header is ignored entirely.
    """
    if os.environ.get(TRUST_PROXY_ENV) == "1":
        raw: str | None = None
        for name, value in scope.get("headers", ()):
            if name.lower() == b"x-forwarded-for":
                raw = value.decode("latin-1")
        if raw:
            host = _host_only(raw.split(",")[-1])
            if host:
                return host

    client = scope.get("client")
    return str(client[0]) if client else "unknown"


class UsageRecorder(Protocol):
    """What the request middleware needs; SQLite and Cloud Logging both satisfy it."""

    def record(
        self,
        *,
        at: int,
        path: str,
        status: int,
        latency_ms: float,
        user_agent: str | None,
        ip: str,
    ) -> None: ...


class UsageStore:
    """SQLite telemetry store. Writes are best-effort; reads belong to scripts/metrics.py."""

    def __init__(self, path: Path, rotation_days: int = DEFAULT_ROTATION_DAYS) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.rotation_seconds = max(1, rotation_days) * 86400
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock, self.conn:
            # The API and the remote MCP endpoint run as separate processes and write
            # the same database. WAL lets a reader (scripts/metrics.py) run while a
            # writer appends, and busy_timeout makes a concurrent writer wait instead
            # of failing the record immediately.
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.executescript(SCHEMA)

    def _active_salt(self, now: int) -> tuple[int, str]:
        with self._lock:
            row = self.conn.execute(
                "SELECT id, salt, created_at FROM salts ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is not None and now - int(row["created_at"]) < self.rotation_seconds:
                return int(row["id"]), str(row["salt"])
            salt = secrets.token_hex(32)
            cursor = self.conn.execute(
                "INSERT INTO salts (salt, created_at) VALUES (?, ?)", (salt, now)
            )
            self.conn.commit()
            assert cursor.lastrowid is not None
            return int(cursor.lastrowid), salt

    def record(
        self,
        *,
        at: int,
        path: str,
        status: int,
        latency_ms: float,
        user_agent: str | None,
        ip: str,
    ) -> None:
        salt_id, salt = self._active_salt(at)
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO requests "
                "(at, path, status, latency_ms, agent_class, ip_hash, salt_id, is_internal) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    at,
                    path,
                    status,
                    latency_ms,
                    classify_agent(user_agent),
                    hash_ip(ip, salt),
                    salt_id,
                    0 if is_external(ip) else 1,
                ),
            )

    def close(self) -> None:
        with self._lock:
            self.conn.close()


class LogUsageStore:
    """One structured JSON line per request, for hosts without a persistent disk.

    Cloud Run replaces the container filesystem on every cold start and runs several
    instances at once, so `usage.db` cannot count unique callers there. A JSON line on
    stdout lands in Cloud Logging, which `scripts/metrics.py --log-export` can read.
    The same fields are emitted as the SQLite store; no raw IP, user agent or payload.

    `USAGE_SALT` must be set to one value for every instance, otherwise each instance
    hashes the same caller differently and the unique count becomes an upper bound.
    The line says which case applied so the report can repeat that caveat.
    """

    def __init__(self, salt: str | None = None) -> None:
        configured = salt or os.environ.get(SALT_ENV)
        self.salt = configured or secrets.token_hex(32)
        self.salt_source = "env" if configured else "ephemeral"

    def record(
        self,
        *,
        at: int,
        path: str,
        status: int,
        latency_ms: float,
        user_agent: str | None,
        ip: str,
    ) -> None:
        line = {
            "event": "zapproof_usage",
            "at": at,
            "path": path,
            "status": status,
            "latency_ms": round(float(latency_ms), 2),
            "agent_class": classify_agent(user_agent),
            "ip_hash": hash_ip(ip, self.salt),
            "salt_id": 1,
            "salt_source": self.salt_source,
            "is_internal": 0 if is_external(ip) else 1,
        }
        print(json.dumps(line, separators=(",", ":")), flush=True)


def store_from_env() -> UsageRecorder | None:
    """The store the app uses, or None when disabled or unavailable.

    Telemetry must never take the score API down, so a broken database path disables
    recording rather than raising into a request. It must not be *silent* either: the
    second independent review found the documented container mounting `data/`
    read-only, which disabled telemetry with no warning at all (G4). Enabled but
    unavailable now logs a warning naming the path.

    `USAGE_SINK=stdout` selects the Cloud Logging line format. The default remains the
    SQLite store, so existing deployments and tests are unchanged.
    """
    if os.environ.get("USAGE_METRICS", "1") == "0":
        logger.info("usage metrics disabled by USAGE_METRICS=0")
        return None

    sink = os.environ.get(SINK_ENV, "sqlite").strip().lower()
    if sink in ("stdout", "log", "logs", "cloud"):
        log_store = LogUsageStore()
        if log_store.salt_source == "ephemeral":
            logger.warning(
                "USAGE_SALT is unset: each instance hashes callers differently, so the "
                "unique-caller count will be an upper bound"
            )
        logger.info("usage metrics recording as JSON lines on stdout")
        return log_store
    if sink not in ("sqlite", "db"):
        logger.warning("unknown %s=%r; falling back to sqlite", SINK_ENV, sink)

    path = Path(os.environ.get("USAGE_DB", str(DEFAULT_DB_PATH)))
    try:
        rotation = int(os.environ.get("USAGE_SALT_ROTATION_DAYS", str(DEFAULT_ROTATION_DAYS)))
        store = UsageStore(path, rotation_days=rotation)
    except (OSError, sqlite3.Error, ValueError) as exc:
        logger.warning(
            "usage metrics are enabled but unavailable at %s (%s); score responses are "
            "unaffected, but callers will not be counted",
            path,
            exc,
        )
        return None
    logger.info("usage metrics recording to %s", path)
    return store


def route_template(scope_route: object) -> str:
    """The route pattern, not the URL. Falls back for unmatched requests."""
    path = getattr(scope_route, "path", None)
    if isinstance(path, str) and path:
        return path
    return "unmatched"


def now_seconds() -> int:
    return int(time.time())
