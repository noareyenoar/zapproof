/**
 * Tests for the x402 guard example. No network access to the public index.
 *
 * Run from this directory: `npm test` (Node 24; no dependencies).
 */

import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import type { AddressInfo } from "node:net";

import {
  attachZapproofGuard,
  checkPayTo,
  formatEvidence,
  type GuardAnswer,
  type GuardContext,
} from "./x402_guard.ts";

const SCORED = "0x1111111111111111111111111111111111111111";
const INSUFFICIENT = "0x2222222222222222222222222222222222222222";
const UNKNOWN = "0x3333333333333333333333333333333333333333";
const SLOW = "0x4444444444444444444444444444444444444444";
const ERROR = "0x5555555555555555555555555555555555555555";
const MALFORMED = "0x6666666666666666666666666666666666666666";
const ODD_STATUS = "0x7777777777777777777777777777777777777777";

const requests: { method: string; url: string; headers: http.IncomingHttpHeaders }[] =
  [];
let delayMs = 0;

function scored() {
  return {
    found: true,
    identifier: SCORED,
    status: "scored",
    score: 87.5,
    confidence_band: "medium",
    evidence: {
      settlements: 42,
      counterparties: 9,
      active_days: 6,
      window: { start: "2026-09-01", end: "2026-10-28" },
      as_of_block: 53201719,
    },
    interpretation: { signaling_likelihood: 0.12, judged: true, rules: [] },
    methodology_version: "v1",
  };
}

function insufficient() {
  return {
    found: true,
    identifier: INSUFFICIENT,
    status: "insufficient_evidence",
    score: null,
    confidence_band: "low",
    evidence: {
      settlements: 2,
      counterparties: 1,
      active_days: 1,
      window: { start: "2026-10-01", end: "2026-10-28" },
      as_of_block: 53201719,
    },
    interpretation: { signaling_likelihood: null, judged: false, rules: [] },
    methodology_version: "v1",
  };
}

function notFound() {
  return {
    found: false,
    identifier: UNKNOWN,
    reason: "no evidence for this identifier in the index",
    note: "This is not a low score. An address with no activity is unknown, not bad.",
  };
}

const server = http.createServer((req, res) => {
  requests.push({
    method: req.method ?? "",
    url: req.url ?? "",
    headers: req.headers,
  });
  const address = (req.url ?? "").split("/").pop()?.toLowerCase() ?? "";

  const send = (status: number, body: string) => {
    if (res.writableEnded || res.destroyed) return;
    res.writeHead(status, {
      "content-type": "application/json",
      "content-length": Buffer.byteLength(body),
    });
    res.end(body);
  };

  const respond = () => {
    if (address === MALFORMED) return send(200, "this is not json");
    if (address === ERROR) return send(500, "{}");
    if (address === ODD_STATUS) {
      return send(200, JSON.stringify({ found: true, status: "something_new" }));
    }
    const payload =
      address === SCORED || address === SLOW
        ? scored
        : address === INSUFFICIENT
          ? insufficient
          : address === UNKNOWN
            ? notFound
            : null;
    if (payload === null) return send(404, "{}");
    send(200, JSON.stringify(payload()));
  };

  if (address === SLOW) {
    setTimeout(respond, delayMs);
  } else {
    respond();
  }
});

await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
const port = (server.address() as AddressInfo).port;
const baseUrl = `http://127.0.0.1:${port}`;

test.after(() => {
  server.close();
});

function reset(): void {
  requests.length = 0;
  delayMs = 0;
}

function check(address: string, timeoutMs = 500): Promise<GuardAnswer | null> {
  return checkPayTo(address, { baseUrl, timeoutMs });
}

test("scored answer is parsed", async () => {
  reset();
  const answer = await check(SCORED);
  assert.ok(answer && answer.found);
  assert.equal(answer.status, "scored");
  assert.equal(answer.score, 87.5);
  assert.equal(answer.settlements, 42);
  assert.equal(answer.counterparties, 9);
  assert.equal(answer.activeDays, 6);
  assert.equal(answer.asOfBlock, 53201719);
  assert.equal(answer.methodologyVersion, "v1");
});

test("insufficient evidence is not zero", async () => {
  reset();
  const answer = await check(INSUFFICIENT);
  assert.ok(answer && answer.found);
  assert.equal(answer.status, "insufficient_evidence");
  assert.equal(answer.score, null);
  assert.match(formatEvidence(answer), /not bad/);
});

test("unknown is not a low score", async () => {
  reset();
  const answer = await check(UNKNOWN);
  assert.ok(answer && !answer.found);
  assert.equal(answer.status, "unknown");
  assert.match(formatEvidence(answer), /not a low score/);
});

test("http error fails open", async () => {
  reset();
  assert.equal(await check(ERROR), null);
});

test("malformed body fails open", async () => {
  reset();
  assert.equal(await check(MALFORMED), null);
});

test("unknown status fails open", async () => {
  reset();
  assert.equal(await check(ODD_STATUS), null);
});

test("timeout fails open", async () => {
  reset();
  delayMs = 1000;
  const started = Date.now();
  assert.equal(await checkPayTo(SLOW, { baseUrl, timeoutMs: 150 }), null);
  assert.ok(Date.now() - started < 900, "the lookup returned promptly");
});

test("invalid address sends nothing", async () => {
  reset();
  for (const value of ["not-an-address", "0xdeadbeef", "", "0x" + "z".repeat(40)]) {
    assert.equal(await check(value), null);
  }
  assert.equal(requests.length, 0);
});

test("request carries only the payee", async () => {
  reset();
  await check(SCORED);
  assert.equal(requests.length, 1);
  const [request] = requests;
  assert.equal(request.method, "GET");
  assert.equal(request.url, `/v1/score/${SCORED}`);
  assert.ok(!request.url.includes("?"));
  assert.equal(request.headers.authorization, undefined);
  assert.equal(request.headers["x-payment"], undefined);
  assert.equal(request.headers.cookie, undefined);
  assert.equal(request.url.split("0x").length - 1, 1);
});

interface FakeClient {
  hook: ((context: GuardContext) => Promise<void>) | null;
  onBeforePaymentCreation(hook: (context: GuardContext) => Promise<void>): unknown;
}

function attach(capture: GuardAnswer[], onEvidence?: (a: GuardAnswer) => void) {
  const client: FakeClient = {
    hook: null,
    onBeforePaymentCreation(hook) {
      this.hook = hook;
      return this;
    },
  };
  attachZapproofGuard(client, {
    baseUrl,
    timeoutMs: 500,
    onEvidence: onEvidence ?? ((answer) => capture.push(answer)),
  });
  assert.ok(client.hook);
  return client.hook;
}

function context(payTo: string, network = "eip155:8453"): GuardContext {
  return { selectedRequirements: { payTo, network } };
}

test("hook logs insufficient evidence and continues", async () => {
  reset();
  const captured: GuardAnswer[] = [];
  const hook = attach(captured);
  const result = await hook(context(INSUFFICIENT));
  assert.equal(result, undefined);
  assert.equal(captured.length, 1);
  assert.equal(captured[0].status, "insufficient_evidence");
});

test("hook continues when the index is down", async () => {
  reset();
  const captured: GuardAnswer[] = [];
  const hook = attach(captured);
  assert.equal(await hook(context(ERROR)), undefined);
  assert.equal(captured.length, 0);
});

test("hook never aborts", async () => {
  reset();
  const captured: GuardAnswer[] = [];
  const hook = attach(captured);
  for (const address of [SCORED, INSUFFICIENT, UNKNOWN, ERROR]) {
    assert.equal(await hook(context(address)), undefined);
  }
});

test("hook skips non-Base networks", async () => {
  reset();
  const captured: GuardAnswer[] = [];
  const hook = attach(captured);
  assert.equal(await hook(context(SCORED, "eip155:84532")), undefined);
  assert.equal(requests.length, 0);
  assert.equal(captured.length, 0);
});

test("hook survives a broken callback", async () => {
  reset();
  const hook = attach([], () => {
    throw new Error("logger is broken");
  });
  assert.equal(await hook(context(SCORED)), undefined);
});

test("hook survives a missing context", async () => {
  reset();
  const hook = attach([]);
  assert.equal(await hook({}), undefined);
});
