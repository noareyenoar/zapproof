"""The daily brief must be honest, tame on Telegram, and never echo a secret.

These tests do not touch the network or send anything: they cover the message
composition, the splitter, the redaction and the alert/recovery copy that the
GitHub Actions workflow relies on.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parent.parent


def _brief() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "telegram_brief", REPO / "scripts" / "telegram_brief.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve the defining module through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_split_message_keeps_every_line_and_respects_the_limit() -> None:
    brief = _brief()
    text = "\n".join(f"line {index} " + "x" * 40 for index in range(300))

    chunks = brief.split_message(text, limit=100)

    assert chunks
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "\n".join(chunks) == text


def test_daily_message_marks_a_failing_surface_and_does_not_invent_commits(
    monkeypatch,
) -> None:
    brief = _brief()
    monkeypatch.setattr(brief, "llm_summary", lambda facts: None)
    checks = [
        brief.Check("API /v1/health", True, "200"),
        brief.Check("Site /", False, "503"),
    ]

    text = brief.compose_daily(checks, [], None)

    assert "✅ API /v1/health: 200" in text
    assert "❌ Site /: 503" in text
    assert "ไม่มีคอมมิตใหม่" in text
    assert "มีปัญหา: Site /" in text


def test_health_parser_rejects_a_response_that_does_not_prove_the_index_is_open() -> None:
    brief = _brief()

    ok, detail = brief.parse_health(
        '{"status": "ok", "as_of_block": 53201719, "sellers_indexed": 19397}'
    )
    assert ok is True
    assert "19,397" in detail

    for body in (
        "not json",
        '{"status": "ok"}',
        '{"as_of_block": 0, "sellers_indexed": 0}',
        '{"as_of_block": 1, "sellers_indexed": "19"}',
    ):
        ok, _ = brief.parse_health(body)
        assert ok is False, body


def test_redaction_removes_a_configured_secret(monkeypatch) -> None:
    brief = _brief()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:do-not-print")

    redacted = brief.redact("failed for 123456:do-not-print")

    assert "do-not-print" not in redacted
    assert "[redacted]" in redacted


def test_alert_and_recovery_copy_name_the_surface_and_the_repeat_rule() -> None:
    brief = _brief()
    failing = brief.Check("MCP /mcp initialize", False, "timeout")
    healthy = brief.Check("Site /", True, "200")

    alert = brief.alert_text([failing], [healthy, failing])
    recovery = brief.recovery_text([healthy])

    assert "เหตุฉุกเฉิน" in alert
    assert "MCP /mcp initialize: timeout" in alert
    assert "ทุก 6 ชั่วโมง" in alert
    assert "กลับมาปกติ" in recovery
    assert "✅ Site /: 200" in recovery
