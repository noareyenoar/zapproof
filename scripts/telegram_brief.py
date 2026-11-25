"""Daily brief and outage alerts for the owner, delivered over Telegram.

The owner may not open the computer every day, so this runs in GitHub Actions —
not on the owner's machine and not in Claude Code. Two modes:

  daily   summarise the last 24 hours (commits, health, usage) in Thai with an LLM
          and send it once a day.
  health  probe the live API, the score endpoint and the site; alert on failure,
          send a recovery note when the failure clears, and stay quiet while an
          outage is unchanged (repeat at most every six hours).

Secrets come from the environment. The Telegram bot token and the Gemini key are
never printed: HTTP failures report only a status code, and every message is
redacted before it can reach stdout.

Environment:
  TELEGRAM_BOT_TOKEN   bot token from @BotFather (required)
  TELEGRAM_CHAT_ID     chat or channel id to post to (required)
  GEMINI_API_KEY       optional; without it the brief falls back to a template
  GEMINI_MODEL         default `gemini-2.5-flash`
  ZAPPROOF_API_URL     default https://zapproof-hc75zyauja-uc.a.run.app
  ZAPPROOF_SITE_URL    default https://zapproof.web.app
  BRIEF_STATE_FILE     default `.state/health.json` (restored from the Actions cache)

Run locally without sending anything:
  uv run python scripts/telegram_brief.py daily --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Thailand has no daylight saving, so a fixed offset works on Windows too (no tzdata).
ICT = timezone(timedelta(hours=7), "ICT")

API_URL = os.environ.get(
    "ZAPPROOF_API_URL", "https://zapproof-hc75zyauja-uc.a.run.app"
).rstrip("/")
SITE_URL = os.environ.get("ZAPPROOF_SITE_URL", "https://zapproof.web.app").rstrip("/")
STATE_FILE = Path(os.environ.get("BRIEF_STATE_FILE", REPO_ROOT / ".state" / "health.json"))

TELEGRAM_LIMIT = 3900
ALERT_REPEAT = timedelta(hours=6)
COMMIT_LIMIT = 12
SECRET_ENV = ("TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY")


@dataclass(frozen=True)
class Check:
    """One probe of a live surface."""

    name: str
    ok: bool
    detail: str


def redact(text: str) -> str:
    """Remove any known secret from text that may be printed or sent."""
    for name in SECRET_ENV:
        value = os.environ.get(name)
        if value:
            text = text.replace(value, "[redacted]")
    return text


def _request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> tuple[int | None, str]:
    """Return (status, body); never raise and never echo the URL."""
    request = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return None, redact(f"{type(exc).__name__}: {reason}")


def parse_health(body: str) -> tuple[bool, str]:
    """A 200 is not enough: health opens the database, so require a non-empty index."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return False, "200 but not JSON"
    if not isinstance(data, dict):
        return False, "200 but not a JSON object"
    sellers = data.get("sellers_indexed")
    block = data.get("as_of_block")
    if not isinstance(sellers, int) or sellers <= 0:
        return False, "sellers_indexed is not a positive integer"
    if not isinstance(block, int) or block <= 0:
        return False, "as_of_block is not a positive integer"
    return True, f"200 · {sellers:,} sellers · block {block:,}"


def check_api_health() -> Check:
    status, body = _request(f"{API_URL}/v1/health")
    if status != 200:
        return Check("API /v1/health", False, status or body)
    ok, detail = parse_health(body)
    return Check("API /v1/health", ok, detail)


def check_mcp() -> Check:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "zapproof-watch", "version": "1.0"},
        },
    }
    status, body = _request(
        f"{API_URL}/mcp",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    if status != 200:
        return Check("MCP /mcp initialize", False, status or body)
    return Check("MCP /mcp initialize", True, "200")


def check_site(path: str = "/", *, name: str = "Site /") -> Check:
    status, body = _request(f"{SITE_URL}{path}")
    if status != 200:
        return Check(name, False, status or body)
    if "Zapproof" not in body:
        return Check(name, False, "200 but the page has no Zapproof marker")
    return Check(name, True, "200")


def run_checks() -> list[Check]:
    # Deliberately no /v1/score probe: metrics count score calls as external demand,
    # and a monitor must not inflate the numbers it reports. /v1/health opens the
    # database, so a non-empty index is already covered.
    return [
        check_api_health(),
        check_mcp(),
        check_site("/", name="Site /"),
        check_site("/agents/", name="Site /agents/"),
    ]


def _git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def recent_commits() -> list[str]:
    output = _git("log", "--since=24 hours ago", "--pretty=format:%h %s", "-n", str(COMMIT_LIMIT))
    return [line for line in output.splitlines() if line.strip()]


def usage_numbers() -> dict[str, object] | None:
    path = REPO_ROOT / "reports" / "usage_metrics.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    keys = (
        "generated",
        "window_days",
        "unique_external_callers",
        "meaningful_external_requests",
    )
    return {key: data.get(key) for key in keys}


def _facts(checks: list[Check], commits: list[str], usage: dict[str, object] | None) -> dict:
    return {
        "date_ict": datetime.now(ICT).strftime("%Y-%m-%d %H:%M"),
        "checks": [{"surface": c.name, "ok": c.ok, "detail": str(c.detail)} for c in checks],
        "commits_last_24h": commits,
        "usage_30d": usage,
    }


def _prompt(facts: dict) -> str:
    return (
        "คุณเป็นผู้ช่วยสรุปสถานะโปรเจกต์ Zapproof ให้เจ้าของที่อาจไม่ได้เปิดคอมพิวเตอร์ทุกวัน "
        "เขียนภาษาไทยสั้น ๆ 4–7 บรรทัด (ไม่เกิน 700 ตัวอักษร) โดยใช้เฉพาะข้อเท็จจริงใน JSON นี้ "
        "ห้ามแต่งตัวเลข ห้ามกล่าวอ้างเกินข้อมูล และห้ามใช้คำว่า scam, fraud, fake, criminal "
        "ถ้าไม่มีคอมมิตใหม่ ให้บอกตรง ๆ ว่าวันนี้ไม่มีคอมมิตใหม่ "
        "ถ้ามี endpoint ล้ม ให้ขึ้นต้นด้วยการเตือนและบอกว่าอะไรล้ม "
        "ปิดท้ายด้วยสิ่งที่เจ้าของอาจต้องทำถ้ามี (หรือ 'ไม่มีอะไรต้องทำ')\n\n"
        f"{json.dumps(facts, ensure_ascii=False, indent=2)}"
    )


def llm_summary(facts: dict) -> str | None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    model = os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash"
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    payload = {
        "contents": [{"parts": [{"text": _prompt(facts)}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 800},
    }
    status, body = _request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
        timeout=45,
    )
    if status != 200:
        return None
    try:
        data = json.loads(body)
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(part.get("text", "") for part in parts).strip()
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None
    return text or None


def template_summary(facts: dict) -> str:
    commits = facts["commits_last_24h"]
    failures = [c["surface"] for c in facts["checks"] if not c["ok"]]
    lines: list[str] = []
    if failures:
        lines.append(f"มีปัญหา: {', '.join(failures)}")
    if commits:
        lines.append(f"มี {len(commits)} คอมมิตใน 24 ชั่วโมงที่ผ่านมา:")
        lines.extend(f"• {line}" for line in commits[:5])
        if len(commits) > 5:
            lines.append(f"• และอีก {len(commits) - 5} คอมมิต")
    else:
        lines.append("วันนี้ไม่มีคอมมิตใหม่")
    lines.append("สถานะ endpoint อยู่ในรายการด้านล่าง")
    return "\n".join(lines)


def _health_line(check: Check) -> str:
    mark = "✅" if check.ok else "❌"
    return f"{mark} {check.name}: {redact(str(check.detail))}"


def compose_daily(checks: list[Check], commits: list[str], usage: dict | None) -> str:
    facts = _facts(checks, commits, usage)
    summary = llm_summary(facts) or template_summary(facts)
    now = datetime.now(ICT).strftime("%d/%m/%Y %H:%M")
    lines = [
        f"📋 Zapproof — สรุปประจำวัน {now} (เวลาไทย)",
        "",
        summary.strip(),
        "",
        "—",
        "สถานะ:",
        *[_health_line(check) for check in checks],
        f"คอมมิต 24 ชม.: {len(commits)}",
    ]
    if usage and usage.get("unique_external_callers") is not None:
        lines.append(
            "ผู้เรียกภายนอก "
            f"({usage.get('window_days', 30)} วัน): {usage.get('unique_external_callers')} "
            f"(คำขอที่มีความหมาย {usage.get('meaningful_external_requests')})"
        )
    lines += ["", f"เว็บ: {SITE_URL}", f"API: {API_URL}/v1/health"]
    return "\n".join(lines)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on line boundaries so no Telegram chunk breaks a sentence."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        extra = len(line) + 1
        if current and size + extra > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        while len(line) > limit:  # a single pathological line
            chunks.append(line[:limit])
            line = line[limit:]
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in split_message(text):
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        status, body = _request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        if status != 200:
            raise SystemExit(f"Telegram sendMessage failed: HTTP {status} {redact(body)[:200]}")


def _read_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def alert_text(failures: list[Check], checks: list[Check]) -> str:
    now = datetime.now(ICT).strftime("%d/%m/%Y %H:%M")
    lines = [
        f"🚨 Zapproof — เหตุฉุกเฉิน {now} (เวลาไทย)",
        "",
        "สิ่งที่ล้ม:",
        *[f"❌ {check.name}: {redact(str(check.detail))}" for check in failures],
        "",
        "สถานะอื่น:",
        *[_health_line(check) for check in checks if check.ok],
        "",
        "จะแจ้งอีกครั้งเมื่อสถานะเปลี่ยน หรือทุก 6 ชั่วโมงถ้ายังล้มอยู่",
    ]
    return "\n".join(lines)


def recovery_text(checks: list[Check]) -> str:
    now = datetime.now(ICT).strftime("%d/%m/%Y %H:%M")
    return "\n".join(
        [
            f"✅ Zapproof — กลับมาปกติแล้ว {now} (เวลาไทย)",
            "",
            *[_health_line(check) for check in checks],
        ]
    )


def health_watch(*, dry_run: bool = False) -> int:
    checks = run_checks()
    failures = [check for check in checks if not check.ok]
    state = _read_state()
    now = datetime.now(UTC)

    if failures:
        same = state.get("failures") == [check.name for check in failures]
        last = _parse_time(state.get("last_alert_at"))
        if same and last is not None and now - last < ALERT_REPEAT:
            print(f"outage persists ({len(failures)} failing); alert suppressed")
            return 0
        text = alert_text(failures, checks)
        if dry_run:
            print(text)
            return 0
        send_telegram(text)
        _write_state(
            {
                "last_status": "fail",
                "failures": [check.name for check in failures],
                "last_alert_at": now.isoformat(),
            }
        )
        print(f"alert sent ({len(failures)} failing)")
        return 0

    if state.get("last_status") == "fail":
        text = recovery_text(checks)
        if dry_run:
            print(text)
            return 0
        send_telegram(text)
        print("recovery sent")
    else:
        print("all checks passed; no message")
    _write_state({"last_status": "ok", "failures": [], "last_alert_at": now.isoformat()})
    return 0


def daily_brief(*, dry_run: bool = False) -> int:
    checks = run_checks()
    commits = recent_commits()
    usage = usage_numbers()
    text = compose_daily(checks, commits, usage)
    if dry_run:
        print(text)
        return 0
    send_telegram(text)
    print("daily brief sent")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252; the message is Thai and must not crash a dry run.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("daily", "health"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the message instead of sending it (safe without secrets)",
    )
    args = parser.parse_args(argv)
    if args.mode == "daily":
        return daily_brief(dry_run=args.dry_run)
    return health_watch(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
