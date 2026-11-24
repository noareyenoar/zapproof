/**
 * Call Zapproof before an x402 payment is signed.
 *
 * This is an integration example, not an SDK and not a gate. It registers one
 * before-payment hook, performs one read-only lookup of the address the agent is
 * about to pay, hands the answer to a callback, and always lets the payment proceed.
 *
 * Three rules the code exists to keep:
 *
 * 1. Fail open. A timeout, a network error, a 5xx or an answer we do not understand
 *    returns null and the payment continues. Zapproof must never become a single
 *    point of failure for someone else's payment.
 * 2. No verdict. The hook never returns { abort: true }. There is no score
 *    threshold. The record is evidence, not approval, and "unknown" is not "bad".
 * 3. No payer data. Only the payee address (payTo) is sent. No wallet address,
 *    signature, amount, resource URL, or request body.
 *
 * Verified against @x402/core on 2026-11-24 (typescript/packages/core/src/client/
 * x402Client.ts and types/payments.ts): the context is `{ paymentRequired,
 * selectedRequirements }` and `selectedRequirements` carries `payTo`, `network`,
 * `amount`, and `scheme`; the hook may return `{ abort: true, reason }` or nothing.
 * If your installed SDK version uses different names, read them from its types
 * instead of trusting this file.
 */

export const BASE_MAINNET = "eip155:8453";
export const DEFAULT_BASE_URL = "https://zapproof-hc75zyauja-uc.a.run.app";
export const DEFAULT_TIMEOUT_MS = 1500;

const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const MAX_RESPONSE_BYTES = 64 * 1024;
const USER_AGENT =
  "zapproof-x402-guard-example/0.1 (+https://github.com/noareyenoar/zapproof)";

/** The part of a Zapproof answer this example is willing to log. */
export type GuardAnswer =
  | {
      found: true;
      identifier: string;
      status: "scored" | "insufficient_evidence";
      /** null unless status is "scored". null is never a zero. */
      score: number | null;
      settlements: number | null;
      counterparties: number | null;
      activeDays: number | null;
      asOfBlock: number | null;
      methodologyVersion: string | null;
    }
  | {
      found: false;
      identifier: string;
      status: "unknown";
      reason: string;
    };

export interface GuardOptions {
  baseUrl?: string;
  timeoutMs?: number;
}

/**
 * Look up one payee. Returns null when the evidence could not be obtained.
 * Never throws; never retries.
 */
export async function checkPayTo(
  payTo: string,
  options: GuardOptions = {},
): Promise<GuardAnswer | null> {
  if (typeof payTo !== "string" || !ADDRESS.test(payTo)) {
    // Not an EVM address: do not send the string anywhere, and do not block.
    return null;
  }

  const baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, "");
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const url = `${baseUrl}/v1/score/${payTo.toLowerCase()}`;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      method: "GET",
      headers: { accept: "application/json", "user-agent": USER_AGENT },
      signal: controller.signal,
    });
    if (!response.ok) {
      return null;
    }
    const text = await response.text();
    if (text.length > MAX_RESPONSE_BYTES) {
      return null;
    }
    let payload: unknown;
    try {
      payload = JSON.parse(text);
    } catch {
      return null;
    }
    return parseAnswer(payload, payTo);
  } catch {
    // Timeout, DNS, TLS, connection reset: fail open.
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/** One log line. No interpretation, no recommendation. */
export function formatEvidence(answer: GuardAnswer): string {
  if (!answer.found) {
    return `Zapproof ${answer.identifier}: unknown — ${answer.reason} (not a low score)`;
  }
  if (answer.status === "insufficient_evidence") {
    return (
      `Zapproof ${answer.identifier}: insufficient_evidence — ` +
      `settlements=${answer.settlements} counterparties=${answer.counterparties} ` +
      `active_days=${answer.activeDays} as_of_block=${answer.asOfBlock} ` +
      `(not bad; no score was issued)`
    );
  }
  const score = answer.score === null ? "null" : answer.score.toFixed(1);
  return (
    `Zapproof ${answer.identifier}: scored ${score} — ` +
    `settlements=${answer.settlements} counterparties=${answer.counterparties} ` +
    `active_days=${answer.activeDays} as_of_block=${answer.asOfBlock} ` +
    `methodology=${answer.methodologyVersion} (one input, not an approval)`
  );
}

/**
 * Structural shape of the x402 client method this example needs. Declared locally so
 * the example has no package dependency; the real x402Client satisfies it.
 */
export interface GuardContext {
  selectedRequirements?: {
    payTo?: unknown;
    network?: unknown;
  };
}

export interface GuardClient {
  onBeforePaymentCreation(
    hook: (context: GuardContext) => Promise<void>,
  ): unknown;
}

/**
 * Register a fail-open lookup on an x402 client and return the hook.
 *
 * The hook runs before the payment payload is created or signed and always resolves
 * to undefined, which means "continue". Returning an abort here would convert an
 * observation into a gate — this example deliberately does not do that.
 */
export function attachZapproofGuard(
  client: GuardClient,
  options: GuardOptions & {
    onEvidence?: (answer: GuardAnswer) => void;
  } = {},
): (context: GuardContext) => Promise<void> {
  const hook = async (context: GuardContext): Promise<void> => {
    try {
      const requirements = context?.selectedRequirements;
      if (
        !requirements ||
        requirements.network !== BASE_MAINNET ||
        typeof requirements.payTo !== "string"
      ) {
        return;
      }
      const answer = await checkPayTo(requirements.payTo, options);
      if (answer !== null && options.onEvidence) {
        try {
          options.onEvidence(answer);
        } catch {
          // A broken logger must not break a payment.
        }
      }
    } catch {
      // Fail open.
    }
    // Always proceed. This is your client; whether to act on the evidence is your
    // policy, not our verdict.
  };

  client.onBeforePaymentCreation(hook);
  return hook;
}

function parseAnswer(payload: unknown, payTo: string): GuardAnswer | null {
  if (typeof payload !== "object" || payload === null) {
    return null;
  }
  const record = payload as Record<string, unknown>;
  const identifier =
    typeof record.identifier === "string" ? record.identifier : payTo.toLowerCase();

  if (record.found === false) {
    return {
      found: false,
      identifier,
      status: "unknown",
      reason:
        typeof record.reason === "string"
          ? record.reason
          : "no seller-side record in this index",
    };
  }

  if (record.found !== true) {
    return null;
  }

  const status = record.status;
  if (status !== "scored" && status !== "insufficient_evidence") {
    // A status we do not understand is not evidence; fail open.
    return null;
  }

  const evidence =
    typeof record.evidence === "object" && record.evidence !== null
      ? (record.evidence as Record<string, unknown>)
      : {};

  return {
    found: true,
    identifier,
    status,
    score: asScore(record.score),
    settlements: asInt(evidence.settlements),
    counterparties: asInt(evidence.counterparties),
    activeDays: asInt(evidence.active_days),
    asOfBlock: asInt(evidence.as_of_block),
    methodologyVersion:
      typeof record.methodology_version === "string"
        ? record.methodology_version
        : null,
  };
}

function asScore(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asInt(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}
