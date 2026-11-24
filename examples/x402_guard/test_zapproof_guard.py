"""Tests for the x402 guard example. No network access to the public index.

Run from the repository root:

    python -m unittest discover -s examples/x402_guard -p "test_*.py" -v
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import zapproof_guard as guard

SCORED = "0x1111111111111111111111111111111111111111"
INSUFFICIENT = "0x2222222222222222222222222222222222222222"
UNKNOWN = "0x3333333333333333333333333333333333333333"
SLOW = "0x4444444444444444444444444444444444444444"
ERROR = "0x5555555555555555555555555555555555555555"
MALFORMED = "0x6666666666666666666666666666666666666666"
ODD_STATUS = "0x7777777777777777777777777777777777777777"


def _scored() -> dict[str, Any]:
    return {
        "found": True,
        "identifier": SCORED,
        "status": "scored",
        "score": 87.5,
        "confidence_band": "medium",
        "evidence": {
            "settlements": 42,
            "counterparties": 9,
            "active_days": 6,
            "window": {"start": "2026-09-01", "end": "2026-10-28"},
            "as_of_block": 53201719,
        },
        "interpretation": {"signaling_likelihood": 0.12, "judged": True, "rules": []},
        "methodology_version": "v1",
    }


def _insufficient() -> dict[str, Any]:
    return {
        "found": True,
        "identifier": INSUFFICIENT,
        "status": "insufficient_evidence",
        "score": None,
        "confidence_band": "low",
        "evidence": {
            "settlements": 2,
            "counterparties": 1,
            "active_days": 1,
            "window": {"start": "2026-10-01", "end": "2026-10-28"},
            "as_of_block": 53201719,
        },
        "interpretation": {"signaling_likelihood": None, "judged": False, "rules": []},
        "methodology_version": "v1",
    }


def _not_found() -> dict[str, Any]:
    return {
        "found": False,
        "identifier": UNKNOWN,
        "reason": "no evidence for this identifier in the index",
        "note": "This is not a low score. An address with no activity is unknown, not bad.",
    }


class _ServerState:
    requests: list[dict[str, Any]] = []
    delay = 0.0


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # keep test output clean
        pass

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        _ServerState.requests.append(
            {
                "method": "GET",
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
            }
        )
        address = self.path.rsplit("/", 1)[-1].lower()

        if address == SLOW:
            time.sleep(_ServerState.delay)

        if address == MALFORMED:
            body = b"this is not json"
        elif address == ERROR:
            self.send_response(500)
            self.end_headers()
            return
        elif address == ODD_STATUS:
            body = json.dumps({"found": True, "status": "something_new"}).encode()
        else:
            payload = {
                SCORED: _scored,
                INSUFFICIENT: _insufficient,
                UNKNOWN: _not_found,
                SLOW: _scored,
            }.get(address)
            if payload is None:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(payload()).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _TestServer(ThreadingHTTPServer):
    daemon_threads = True


class GuardTest(unittest.TestCase):
    server: _TestServer
    thread: threading.Thread
    base_url: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _TestServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address[:2]
        cls.base_url = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self) -> None:
        _ServerState.requests.clear()
        _ServerState.delay = 0.0

    def _check(self, address: str, **kwargs: Any) -> guard.GuardAnswer | None:
        return guard.check(address, base_url=self.base_url, **kwargs)

    def test_scored_answer_is_parsed(self) -> None:
        answer = self._check(SCORED)
        assert answer is not None
        self.assertTrue(answer.found)
        self.assertEqual(answer.status, "scored")
        self.assertEqual(answer.score, 87.5)
        self.assertEqual(answer.settlements, 42)
        self.assertEqual(answer.counterparties, 9)
        self.assertEqual(answer.active_days, 6)
        self.assertEqual(answer.as_of_block, 53201719)
        self.assertEqual(answer.methodology_version, "v1")

    def test_insufficient_evidence_is_not_zero(self) -> None:
        answer = self._check(INSUFFICIENT)
        assert answer is not None
        self.assertTrue(answer.found)
        self.assertEqual(answer.status, "insufficient_evidence")
        self.assertIsNone(answer.score)
        self.assertIn("not bad", guard.format_evidence(answer))

    def test_unknown_is_not_a_low_score(self) -> None:
        answer = self._check(UNKNOWN)
        assert answer is not None
        self.assertFalse(answer.found)
        self.assertEqual(answer.status, "unknown")
        self.assertIsNone(answer.score)
        self.assertIn("not a low score", guard.format_evidence(answer))

    def test_http_error_fails_open(self) -> None:
        self.assertIsNone(self._check(ERROR))

    def test_malformed_body_fails_open(self) -> None:
        self.assertIsNone(self._check(MALFORMED))

    def test_unknown_status_fails_open(self) -> None:
        self.assertIsNone(self._check(ODD_STATUS))

    def test_timeout_fails_open(self) -> None:
        _ServerState.delay = 1.0
        started = time.monotonic()
        answer = guard.check(SLOW, base_url=self.base_url, timeout=0.15)
        elapsed = time.monotonic() - started
        self.assertIsNone(answer)
        self.assertLess(elapsed, 0.9)

    def test_invalid_address_sends_nothing(self) -> None:
        for value in ("not-an-address", "0xdeadbeef", "", "0x" + "z" * 40):
            self.assertIsNone(self._check(value))
        self.assertEqual(_ServerState.requests, [])

    def test_request_carries_only_the_payee(self) -> None:
        self._check(SCORED)
        self.assertEqual(len(_ServerState.requests), 1)
        request = _ServerState.requests[0]
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["path"], f"/v1/score/{SCORED}")
        self.assertNotIn("?", request["path"])
        headers = request["headers"]
        self.assertNotIn("authorization", headers)
        self.assertNotIn("x-payment", headers)
        self.assertNotIn("cookie", headers)
        # The path is the only place an address appears.
        self.assertEqual(request["path"].count("0x"), 1)

    def test_hook_logs_insufficient_evidence_and_continues(self) -> None:
        captured: list[guard.GuardAnswer] = []
        hook = self._attach(captured)
        result = asyncio.run(hook(_Context(INSUFFICIENT)))
        self.assertIsNone(result)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].status, "insufficient_evidence")

    def test_hook_continues_when_index_is_down(self) -> None:
        captured: list[guard.GuardAnswer] = []
        hook = self._attach(captured)
        result = asyncio.run(hook(_Context(ERROR)))
        self.assertIsNone(result)
        self.assertEqual(captured, [])

    def test_hook_never_aborts(self) -> None:
        captured: list[guard.GuardAnswer] = []
        hook = self._attach(captured)
        for address in (SCORED, INSUFFICIENT, UNKNOWN, ERROR):
            self.assertIsNone(asyncio.run(hook(_Context(address))))

    def test_hook_skips_non_base_networks(self) -> None:
        captured: list[guard.GuardAnswer] = []
        hook = self._attach(captured)
        result = asyncio.run(hook(_Context(SCORED, network="eip155:84532")))
        self.assertIsNone(result)
        self.assertEqual(_ServerState.requests, [])
        self.assertEqual(captured, [])

    def test_hook_survives_a_broken_callback(self) -> None:
        def boom(_: guard.GuardAnswer) -> None:
            raise RuntimeError("logger is broken")

        hook = self._attach([], on_evidence=boom)
        self.assertIsNone(asyncio.run(hook(_Context(SCORED))))

    def test_hook_survives_a_missing_context(self) -> None:
        hook = self._attach([])
        self.assertIsNone(asyncio.run(hook(object())))

    def _attach(self, captured: list[guard.GuardAnswer], **kwargs: Any) -> Any:
        client = _FakeClient()
        on_evidence = kwargs.pop("on_evidence", captured.append)
        guard.attach_guard(
            client,
            on_evidence=on_evidence,
            base_url=self.base_url,
            timeout=0.5,
            **kwargs,
        )
        assert client.hook is not None
        return client.hook


class _Requirements:
    def __init__(self, pay_to: str, network: str) -> None:
        self.pay_to = pay_to
        self.network = network


class _Context:
    def __init__(self, pay_to: str, network: str = guard.BASE_MAINNET) -> None:
        self.selected_requirements = _Requirements(pay_to, network)


class _FakeClient:
    def __init__(self) -> None:
        self.hook: Any = None

    def on_before_payment_creation(self, hook: Any) -> None:
        self.hook = hook


if __name__ == "__main__":
    unittest.main()
