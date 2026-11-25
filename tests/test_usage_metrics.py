"""Usage metrics (prompt task B7; gate check GB.17).

The properties under test are the ones the prompt names: what is recorded, what is
*not* recorded, that the owner and internal traffic are excluded from the market
count, and that a missing database reports zero rather than an error.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

import yaml
from fastapi.testclient import TestClient
from scripts import metrics as metrics_module

from repindex.api import app as app_module
from repindex.api.usage import (
    LogUsageStore,
    UsageStore,
    classify_agent,
    client_ip,
    hash_ip,
    is_external,
    store_from_env,
)

NOW = int(time.time())


def _store(tmp_path: Path, *, rotation_days: int = 90) -> UsageStore:
    return UsageStore(tmp_path / "usage.db", rotation_days=rotation_days)


def _record(
    store: UsageStore,
    *,
    at: int = NOW,
    ip: str = "8.8.8.8",
    path: str = "/v1/score/{identifier}",
    status: int = 200,
    agent: str = "curl/8.0",
) -> None:
    store.record(
        at=at,
        path=path,
        status=status,
        latency_ms=12.5,
        user_agent=agent,
        ip=ip,
    )


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #


def test_agent_classes_are_coarse_and_not_the_raw_header() -> None:
    assert classify_agent("Claude/1.0 (agent)") == "agent"
    assert classify_agent("ChatGPT-User/1.0") == "agent"
    assert classify_agent("Mozilla/5.0 (Windows NT 10.0) Chrome/120") == "browser"
    assert classify_agent("curl/8.4.0") == "cli"
    assert classify_agent("python-httpx/0.27") == "cli"
    assert classify_agent(None) == "unknown"
    assert classify_agent("SomethingElse/1.0") == "unknown"


def test_only_public_addresses_count_as_external() -> None:
    assert is_external("8.8.8.8") is True
    assert is_external("1.1.1.1") is True
    assert is_external("127.0.0.1") is False
    assert is_external("192.168.1.10") is False
    assert is_external("10.0.0.5") is False
    assert is_external("::1") is False
    assert is_external("testclient") is False


# --------------------------------------------------------------------------- #
# what the store keeps
# --------------------------------------------------------------------------- #


def test_the_store_keeps_a_hash_and_never_the_address(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _record(store, ip="8.8.8.8")
        row = store.conn.execute("SELECT * FROM requests").fetchone()
    finally:
        store.close()

    assert row is not None
    assert "8.8.8.8" not in json.dumps(dict(row))
    assert len(row["ip_hash"]) == 64
    assert row["path"] == "/v1/score/{identifier}"
    assert row["agent_class"] == "cli"


def test_no_payload_or_query_string_is_stored(tmp_path: Path) -> None:
    """The schema has a fixed set of columns; none of them can hold a payload."""
    store = _store(tmp_path)
    try:
        columns = {
            row["name"]
            for row in store.conn.execute("PRAGMA table_info(requests)").fetchall()
        }
    finally:
        store.close()

    assert columns == {
        "id",
        "at",
        "path",
        "status",
        "latency_ms",
        "agent_class",
        "ip_hash",
        "salt_id",
        "is_internal",
    }


# --------------------------------------------------------------------------- #
# the market count
# --------------------------------------------------------------------------- #


def test_collect_counts_unique_external_callers_and_excludes_owner_and_internal(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        _record(store, ip="8.8.8.8")
        _record(store, ip="8.8.8.8")  # same caller twice
        _record(store, ip="9.9.9.9")
        _record(store, ip="127.0.0.1")  # internal
        _record(store, ip="1.1.1.1")  # owner
    finally:
        store.close()

    result = metrics_module.collect(
        tmp_path / "usage.db", window_days=30, owner_ips=["1.1.1.1"]
    )

    assert result["unique_external_callers"] == 2
    assert result["requests_total"] == 5
    assert result["requests_external"] == 3
    assert result["excluded"]["internal_requests"] == 1
    assert result["excluded"]["owner_requests"] == 1
    assert result["by_endpoint"]["/v1/score/{identifier}"]["external"] == 3


def test_a_caller_seen_across_a_salt_rotation_counts_twice_and_is_flagged(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, rotation_days=1)
    try:
        _record(store, ip="8.8.8.8", at=NOW)
        _record(store, ip="8.8.8.8", at=NOW + 3 * 86400)
    finally:
        store.close()

    result = metrics_module.collect(
        tmp_path / "usage.db", window_days=30, owner_ips=[]
    )

    assert result["unique_external_callers"] == 2
    assert result["salt_rotations_in_window"] == 2
    assert "upper bound" in result["note"]


def test_health_checks_scans_and_rejected_identifiers_do_not_count(tmp_path: Path) -> None:
    """GB.17 must measure market demand, not uptime monitors and 404 scanners."""
    store = _store(tmp_path)
    try:
        _record(store, ip="8.8.8.8", path="/v1/health", status=200)
        _record(store, ip="9.9.9.9", path="unmatched", status=404)
        _record(store, ip="7.7.7.7", path="/v1/score/{identifier}", status=422)
    finally:
        store.close()

    result = metrics_module.collect(
        tmp_path / "usage.db", window_days=30, owner_ips=[]
    )

    assert result["requests_external"] == 3
    assert result["meaningful_external_requests"] == 0
    assert result["unique_external_callers"] == 0
    assert "none was a successful score or tool call" in result["note"]


def test_a_successful_tool_call_counts_as_a_caller(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _record(store, ip="8.8.8.8", path="/mcp/tools/call/check_counterparty", status=200)
    finally:
        store.close()

    result = metrics_module.collect(
        tmp_path / "usage.db", window_days=30, owner_ips=[]
    )

    assert result["unique_external_callers"] == 1
    assert result["meaningful_external_requests"] == 1
    assert result["by_endpoint"]["/mcp/tools/call/check_counterparty"]["meaningful"] == 1


def test_a_missing_database_reports_zero_not_an_error(tmp_path: Path) -> None:
    result = metrics_module.collect(
        tmp_path / "absent.db", window_days=30, owner_ips=[]
    )

    assert result["unique_external_callers"] == 0
    assert result["requests_total"] == 0
    assert "no usage database" in result["note"]


def test_the_window_excludes_older_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _record(store, ip="8.8.8.8", at=NOW - 40 * 86400)
        _record(store, ip="9.9.9.9", at=NOW)
    finally:
        store.close()

    result = metrics_module.collect(
        tmp_path / "usage.db", window_days=30, owner_ips=[]
    )

    assert result["requests_total"] == 1
    assert result["unique_external_callers"] == 1


# --------------------------------------------------------------------------- #
# the middleware
# --------------------------------------------------------------------------- #


def test_enabled_but_unavailable_telemetry_logs_a_warning(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """G4: the documented container disabled telemetry without saying anything."""
    monkeypatch.setenv("USAGE_METRICS", "1")
    monkeypatch.setenv("USAGE_DB", str(tmp_path))  # a directory cannot be a database

    with caplog.at_level(logging.WARNING, logger="repindex.api.usage"):
        store = store_from_env()

    assert store is None
    assert any("enabled but unavailable" in record.message for record in caplog.records)


def test_disabled_telemetry_is_off_on_purpose_without_a_warning(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    monkeypatch.setenv("USAGE_METRICS", "0")
    monkeypatch.setenv("USAGE_DB", str(tmp_path / "usage.db"))

    with caplog.at_level(logging.WARNING, logger="repindex.api.usage"):
        store = store_from_env()

    assert store is None
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_compose_gives_telemetry_a_writable_volume() -> None:
    """G4: `./data:/app/data:ro` plus no `USAGE_DB` silently disabled telemetry."""
    compose = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
    )
    service = compose["services"]["api"]
    usage_db = service["environment"]["USAGE_DB"]
    volumes = service["volumes"]

    assert usage_db.startswith("/app/telemetry/")
    assert "telemetry:/app/telemetry" in volumes
    assert "telemetry" in compose["volumes"]
    assert "./data:/app/data:ro" in volumes, "the score database must stay read-only"


def test_the_api_records_the_route_template_not_the_identifier(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(app_module, "_USAGE_STORE", store)
    monkeypatch.setattr(app_module, "_USAGE_READY", True)
    client = TestClient(app_module.app)
    try:
        client.get("/v1/health")
        row = store.conn.execute("SELECT path, status FROM requests").fetchone()
    finally:
        store.close()

    assert row is not None
    assert row["path"] == "/v1/health"
    assert row["status"] == 200


def _broken_store(monkeypatch) -> None:
    class Broken:
        def record(self, **_: object) -> None:
            raise sqlite3.OperationalError("telemetry is down")

    monkeypatch.setattr(app_module, "_USAGE_STORE", Broken())
    monkeypatch.setattr(app_module, "_USAGE_READY", True)
    monkeypatch.setattr(app_module, "_USAGE_WRITE_ERRORS", set())


def test_a_broken_telemetry_store_does_not_break_a_response_and_warns_once(
    monkeypatch, caplog
) -> None:
    """H4: the README promised write failures are logged; they were suppressed."""
    _broken_store(monkeypatch)
    client = TestClient(app_module.app)

    with caplog.at_level(logging.WARNING, logger="repindex.api.app"):
        first = client.get("/v1/health")
        second = client.get("/v1/health")

    assert first.status_code == 200
    assert second.status_code == 200
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1, "a broken store must be visible without flooding the log"
    assert "usage telemetry write failed" in warnings[0].message
    assert "OperationalError" in warnings[0].message


# --------------------------------------------------------------------------- #
# serverless sinks: the proxy address and the Cloud Logging line
# --------------------------------------------------------------------------- #


def _scope(*, forwarded: str | None, client: tuple[str, int] = ("127.0.0.1", 1234)):
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    return {"type": "http", "headers": headers, "client": client}


def test_the_last_forwarded_entry_is_the_caller_through_a_trusted_proxy(
    monkeypatch,
) -> None:
    """Cloud Run appends the peer address last; earlier entries are caller-supplied."""
    monkeypatch.setenv("ZAPPROOF_TRUST_PROXY", "1")
    scope = _scope(forwarded="6.6.6.6, 203.0.113.9, 8.8.8.8")

    assert client_ip(scope) == "8.8.8.8"


def test_a_forwarded_header_is_ignored_without_the_trust_flag(monkeypatch) -> None:
    monkeypatch.delenv("ZAPPROOF_TRUST_PROXY", raising=False)

    assert client_ip(_scope(forwarded="8.8.8.8")) == "127.0.0.1"


def test_a_forwarded_port_is_stripped_but_bare_ipv6_is_not(monkeypatch) -> None:
    monkeypatch.setenv("ZAPPROOF_TRUST_PROXY", "1")

    assert client_ip(_scope(forwarded="8.8.8.8:5555")) == "8.8.8.8"
    assert client_ip(_scope(forwarded="2001:4860:4860::8888")) == "2001:4860:4860::8888"


def test_the_stdout_sink_writes_one_hashed_json_line_and_no_raw_address(
    capsys,
) -> None:
    store = LogUsageStore(salt="fixed")
    store.record(
        at=NOW,
        path="/v1/score/{identifier}",
        status=200,
        latency_ms=12.345,
        user_agent="curl/8.0",
        ip="8.8.8.8",
    )
    line = capsys.readouterr().out.strip()

    assert "8.8.8.8" not in line
    payload = json.loads(line)
    assert payload["event"] == "zapproof_usage"
    assert payload["path"] == "/v1/score/{identifier}"
    assert payload["agent_class"] == "cli"
    assert payload["salt_source"] == "env"
    assert payload["is_internal"] == 0
    assert payload["ip_hash"] == hash_ip("8.8.8.8", "fixed")


def test_an_unset_salt_is_marked_ephemeral_and_warned_once(monkeypatch, caplog) -> None:
    monkeypatch.setenv("USAGE_METRICS", "1")
    monkeypatch.setenv("USAGE_SINK", "stdout")
    monkeypatch.delenv("USAGE_SALT", raising=False)

    with caplog.at_level(logging.WARNING, logger="repindex.api.usage"):
        store = store_from_env()

    assert store is not None
    assert isinstance(store, LogUsageStore)
    assert store.salt_source == "ephemeral"
    assert any("USAGE_SALT is unset" in record.message for record in caplog.records)


def _log_line(*, at: int = NOW, ip: str = "8.8.8.8", path: str = "/v1/score/{identifier}",
              status: int = 200, salt: str = "fixed", salt_source: str = "env") -> dict:
    return {
        "event": "zapproof_usage",
        "at": at,
        "path": path,
        "status": status,
        "latency_ms": 5.0,
        "agent_class": "agent",
        "ip_hash": hash_ip(ip, salt),
        "salt_id": 1,
        "salt_source": salt_source,
        "is_internal": 0 if is_external(ip) else 1,
    }


def test_a_log_export_reports_the_same_market_count_as_the_database(tmp_path: Path) -> None:
    export = tmp_path / "usage-log.jsonl"
    export.write_text(
        "\n".join(
            json.dumps(line)
            for line in (
                _log_line(ip="8.8.8.8"),
                _log_line(ip="8.8.8.8"),
                _log_line(ip="9.9.9.9"),
                _log_line(ip="8.8.8.8", path="/v1/health"),
                _log_line(ip="1.1.1.1"),
                _log_line(ip="7.7.7.7", at=NOW - 40 * 86400),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    result = metrics_module.collect_log_export(
        export, window_days=30, owner_ips=["1.1.1.1"], usage_salt="fixed"
    )

    assert result["usage_source"] == "log-export"
    assert result["unique_external_callers"] == 2
    assert result["requests_total"] == 5, "the 40-day-old line is outside the window"
    assert result["excluded"]["owner_requests"] == 1
    assert result["by_endpoint"]["/v1/health"]["meaningful"] == 0


def test_a_cloud_logging_entry_is_read_from_json_payload_message(tmp_path: Path) -> None:
    export = tmp_path / "entries.json"
    export.write_text(
        json.dumps(
            [
                {
                    "jsonPayload": {
                        "message": json.dumps(_log_line(ip="9.9.9.9"))
                    }
                }
            ]
        ),
        encoding="utf-8",
    )

    result = metrics_module.collect_log_export(
        export, window_days=30, owner_ips=[], usage_salt=None
    )

    assert result["unique_external_callers"] == 1


def test_a_log_export_with_an_ephemeral_salt_is_an_upper_bound(tmp_path: Path) -> None:
    export = tmp_path / "usage-log.jsonl"
    export.write_text(
        json.dumps(_log_line(ip="8.8.8.8", salt_source="ephemeral")) + "\n",
        encoding="utf-8",
    )

    result = metrics_module.collect_log_export(
        export, window_days=30, owner_ips=[], usage_salt=None
    )

    assert "upper bound" in result["note"]


def test_owner_ips_without_the_service_salt_are_reported_not_silently_ignored(
    tmp_path: Path,
) -> None:
    export = tmp_path / "usage-log.jsonl"
    export.write_text(json.dumps(_log_line(ip="1.1.1.1")) + "\n", encoding="utf-8")

    result = metrics_module.collect_log_export(
        export, window_days=30, owner_ips=["1.1.1.1"], usage_salt=None
    )

    assert result["excluded"]["owner_requests"] == 0
    assert "may include the owner" in result["note"]
