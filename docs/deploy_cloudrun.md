# Deploying the hosted API and remote MCP (Cloud Run)

The owner deploys. The agent prepares artifacts and never runs a deployment or a
public publish.

This deployment puts three surfaces on **one origin**:

| Path | What it is |
|---|---|
| `/v1/...` | The read-only score API and its OpenAPI document |
| `/mcp` | The remote MCP endpoint agents connect to (Streamable HTTP, stateless) |
| `/skill.md` | The agent skill: request → 402 → pay 1 sat → retry |
| `/skill.th.md` | Thai translation of the skill; JSON fields and status values stay English |

The static site is separate, on Firebase Hosting (`https://zapproof.web.app`). It is
*not* placed in front of the API — see "Why no Hosting rewrite" below.

---

## 1. Why Cloud Run, and why one service

- Blaze is already enabled on the owner's Firebase project. Cloud Run is the only
  part of Google Cloud that needs billing, and it has a monthly free tier
  (2M requests, 180k vCPU-seconds, 360k GiB-seconds) that a launch at this scale
  will not exhaust. A budget alert is still the first thing to set (below).
- Scale-to-zero means no idle cost: with `--min-instances 0`, a month with no
  callers costs at most a few cents of image storage in Artifact Registry.
- One service = one URL, one image, one deploy. The API and MCP share the same
  read-only database, the same telemetry sink and the same rate-limit key
  (`repindex.hosted:app` dispatches on the path).
- Remote MCP runs **stateless with JSON responses** (`ZAPPROOF_MCP_STATELESS=1`,
  `ZAPPROOF_MCP_JSON_RESPONSE=1`). Cloud Run load-balances across instances and has no
  session affinity, so a session that lives only in one instance's memory would fail
  intermittently. Stateless requests are self-contained.

### Why no Hosting rewrite

Firebase Hosting can rewrite to Cloud Run, but a rewrite replaces the `Host` header
with the run.app host and appends its own CDN address to `X-Forwarded-For` — the last
forwarded entry becomes the CDN, not the caller. Per-client rate limits and the
unique-caller metric would then key on Google infrastructure instead of the caller.
Hosting therefore serves only the static site; agents are given the Cloud Run URL for
API and MCP. This is also why the site's `__API_URL__` token exists.

---

## 2. Owner prerequisites

1. `gcloud` CLI installed and authenticated (`gcloud auth login`), and the
   `firebase` CLI (`npm install -g firebase-tools`, `firebase login`) for the site.
2. Billing/Blaze active on project `studio-226725885-31239` (already true).
3. A **billing budget alert** — Cloud console → Billing → Budgets and alerts → create
   a budget of 1 USD with email alerts at 50/90/100%. This is not a hard cap; it is
   the signal that something is looping.
4. `.firebaserc` points at `studio-226725885-31239` and `firebase.json` names the
   hosting site `zapproof`.

Nothing in the repository contains a secret. `USAGE_SALT` is generated on first run
into `.deploy/usage_salt` (gitignored); keep that file, because changing it resets
caller deduplication.

---

## 3. Deploy

From the repository root:

```bash
bash deploy/cloudrun/deploy.sh
```

The script is idempotent. In order, it:

1. sets project `studio-226725885-31239`;
2. enables `run`, `cloudbuild`, `artifactregistry` (safe to repeat);
3. builds `data/hosted.db` with `scripts/build_hosted_db.py` if absent — the compact
   read-only database the image carries (about 550 MB raw, 75 MB gzipped; the 1.9 GB
   ingest database never enters the build context);
4. generates `.deploy/usage_salt` once;
5. creates the Artifact Registry repository `zapproof` once;
6. submits the image build (`deploy/cloudrun/cloudbuild.yaml`), tagged with the git
   short SHA;
7. deploys Cloud Run service `zapproof`, `us-central1`, unauthenticated, 1 GiB RAM,
   1 vCPU, concurrency 20, min instances 0, max instances 3, 60 s timeout;
8. reads back the URL Google assigned and rewrites `ZAPPROOF_PUBLIC_URL` and
   `ZAPPROOF_MCP_ALLOWED_HOSTS` with it;
9. adds a Cloud Logging exclusion for `run.googleapis.com/requests` (see §5);
10. renders the site with `--base-url https://zapproof.web.app --api-url <run URL>`
    and deploys Firebase Hosting, creating the named site once if it does not exist.

Environment overrides: `PROJECT_ID`, `REGION`, `SERVICE`, `REPO`, `FIREBASE_SITE`,
`SKIP_HOSTING=1`, `SKIP_LOGGING_EXCLUSION=1`.

Expected duration: first run 8–15 minutes (database build + Cloud Build), later runs
5–10 minutes.

### What the deploy sets

| Variable | Value | Purpose |
|---|---|---|
| `REPINDEX_DB` | `/app/data/hosted.db` | read-only score database in the image |
| `ZAPPROOF_PUBLIC_URL` | `https://zapproof-<n>.us-central1.run.app` | OpenAPI `servers`, skill URL |
| `ZAPPROOF_MCP_ALLOWED_HOSTS` | that host | DNS-rebinding allow-list; the SDK rejects any other `Host` with 421 |
| `ZAPPROOF_TRUST_PROXY` | `1` | take the **last** `X-Forwarded-For` entry (Cloud Run's edge appends the caller) |
| `ZAPPROOF_MCP_STATELESS` | `1` | no cross-instance session state |
| `ZAPPROOF_MCP_JSON_RESPONSE` | `1` | JSON instead of SSE, connection-per-request |
| `USAGE_METRICS` | `1` | telemetry on |
| `USAGE_SINK` | `stdout` | one JSON line per request to Cloud Logging |
| `USAGE_SALT` | `.deploy/usage_salt` | one salt for every instance, so caller hashes match |

---

## 4. Verify after deploy

```bash
URL=$(gcloud run services describe zapproof --region us-central1 \
      --project studio-226725885-31239 --format='value(status.url)')

curl -s "$URL/v1/health"
curl -s "$URL/skill.md" | head -5
curl -s "$URL/v1/score/0x0000000000000000000000000000000000000000"

curl -s -X POST "$URL/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"deploy-check","version":"0"}}}'
```

The score answer for the zero address must be `found: false` with a note that this is
not a low score. The MCP answer must be `200` with `application/json` and a
`serverInfo` object; a `500` here means the session-manager lifespan did not start,
and a `421` means `ZAPPROOF_MCP_ALLOWED_HOSTS` does not match the host in the URL. If
the host you tried is not `status.url`, try `status.url` before changing anything.

Then connect a real MCP client to `$URL/mcp` and run `tools/list` and one
`check_counterparty` call.

Or run the post-deploy smoke test, which checks all of the above and exits non-zero on
any failure:

```bash
uv run python scripts/smoke_hosted.py --base-url "$URL"
```

**A deploy is not done until this exits zero.** On 2026-11-17 the service answered `200`
on `/v1/methodology` and `/skill.md` while `/v1/health` and `/v1/score` answered `500`;
checking one green route by eye is how that shipped.

---

## 5. Logs and privacy

Two kinds of log lines exist:

- **Cloud Run request logs** (`run.googleapis.com/requests`) contain the URL of every
  request. Score URLs carry the address being checked, so these logs are excluded at
  the project level with:

  ```bash
  gcloud logging sinks update _Default --project studio-226725885-31239 \
    --add-exclusion='name=zapproof-request-logs,filter=LOG_ID("run.googleapis.com/requests")'
  ```

  The deploy script does this once. It applies to the whole project, which currently
  runs nothing else. `SKIP_LOGGING_EXCLUSION=1` skips it, and that is the wrong choice
  for a public score endpoint.
- **The service's own usage lines** (`jsonPayload.event="zapproof_usage"`): route
  template, status, latency, agent class, a salted IP hash, and nothing else. No raw
  IP, no user agent, no address, no payload.

The uvicorn access log is disabled (`--no-access-log`) for the same reason.

### Reading the caller count

`usage.db` is ephemeral on Cloud Run, so metrics come from the log lines:

```bash
gcloud logging read 'jsonPayload.event="zapproof_usage"' \
  --project studio-226725885-31239 --format=json --limit=100000 > usage-log.json

uv run python scripts/metrics.py --log-export usage-log.json \
  --usage-salt "$(cat .deploy/usage_salt)" \
  --owner-ip <the owner's public IP>
```

Without `--usage-salt`, owner IPs cannot be matched, and without a fixed salt across
instances the unique count is an upper bound; the report says so in `note`. Cloud
Logging retention is 30 days by default, which matches the market window.

---

## 6. Cost guardrails

- `--max-instances 3` bounds a traffic spike; `--min-instances 0` removes idle cost.
- The database is baked into the image, so no database service is billed.
- Artifact Registry keeps every deploy's image. If deploys are frequent, add a cleanup
  policy (keep the last 10) in the console.
- `USAGE_SINK=stdout` writes no disk, so there is no volume to pay for.
- Check monthly: Cloud Run requests/CPU-seconds, Cloud Build minutes, Artifact Registry
  storage. Free tiers cover a launch; a runaway loop would show as a request spike and
  is what the 1 USD budget alert catches.

---

## 7. Rollback

- **API/MCP code:** deploy the previous image tag.
  ```bash
  gcloud run services update-traffic zapproof --region us-central1 \
    --project studio-226725885-31239 --to-revisions=<revision-name>=100
  ```
  Revisions are listed by `gcloud run revisions list --service zapproof --region us-central1`.
- **Site:** Firebase console → Hosting → release history → rollback; or re-render and
  redeploy the previous commit with `bash deploy/cloudrun/deploy.sh`.
- **Logging exclusion** can be removed with
  `gcloud logging sinks update _Default --remove-exclusion=zapproof-request-logs`.

---

## 8. The 1-sat toll is not live yet

The x402 toll (1 sat ≈ 0.001 USDC on Base per score request) is approved by the owner
but **not implemented**: it needs a receiving Base wallet, a facilitator account, the
gate in the API, and one measured live settlement (Q14 in `the owner decision record`). Public
copy therefore says the hosted index is free today and that a toll is being brought
online. Do not change that copy to "metered" until the first real 0.001 USDC settlement
has been observed, and do not submit x402 Bazaar / Circle listings before then — both
index only resources that have settled a payment.

The `500` on `/v1/health` and `/v1/score` was repaired on 2026-11-23 by removing the
stale `REPINDEX_DB` override:

```bash
gcloud run services update zapproof \
  --project studio-226725885-31239 --region us-central1 \
  --remove-env-vars REPINDEX_DB
```

Both now return `200` and the zero address returns `found: false` with no score. The
2026-11-23 deploy (revision `zapproof-00005-8ps`) then shipped `/skill.th.md` and the
Thai site; `scripts/smoke_hosted.py` passes all 8 checks on the canonical URL. That
failure is still the reason the script exists: on 2026-11-17 `/v1/methodology` and
`/skill.md` answered `200` while the score answered `500`, so a single green check is
not a deploy verification.

**Service URL note (two hostnames).** Cloud Run can present two names for the same
service. `gcloud run deploy` may print the newer
`https://zapproof-552007190449.us-central1.run.app` form, while
`gcloud run services describe … status.url` returns the older
`https://zapproof-hc75zyauja-uc.a.run.app`. `deploy.sh` trusts `status.url` and sets
`ZAPPROOF_PUBLIC_URL` / `ZAPPROOF_MCP_ALLOWED_HOSTS` from it, so **`status.url` is the
canonical URL**. Both hostnames serve REST routes and the skills, but `/mcp` answers
only on the allowed host; the other returns `421`. Run the smoke test on `status.url`,
put that URL in `server.json`, and never publish a `421` host.

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `421 Misdirected Request` on `/mcp` | `Host` is not in `ZAPPROOF_MCP_ALLOWED_HOSTS`; Cloud Run can print a second hostname that is not the one in `status.url` | call the `status.url` host; re-run `deploy.sh` or update `ZAPPROOF_MCP_ALLOWED_HOSTS` to the host you will publish, and verify with the smoke test |
| `500` on `/mcp`, `Task group is not initialized` | MCP session manager lifespan did not run | `repindex.hosted` must dispatch `/mcp` itself and start both lifespans; covered by `tests/test_hosted.py` |
| `503` on every `/v1` route | database missing inside the image | rebuild `data/hosted.db`, redeploy |
| `500` on `/v1/score` and `/v1/health`, but `/v1/methodology` is 200, log says `index database not found at C:\Program Files\Git\app\...` | Git Bash rewrote `REPINDEX_DB=/app/...` into a Windows path before gcloud saw it | remove the override (`gcloud run services update … --remove-env-vars REPINDEX_DB`); the image's own `ENV` applies. `deploy.sh` now exports `MSYS_NO_PATHCONV=1` and does not pass this path |
| `firebase deploy` says site not found | new project has only its default site | deploy script creates `zapproof` once and retries |
| Caller count looks like the owner only | `ZAPPROOF_TRUST_PROXY` unset, or salt changed | set it to `1`; keep `.deploy/usage_salt` stable |
| `/skill.md` shows `__API_URL__` | image built before render-time replacement | skill is rendered at request time from `ZAPPROOF_PUBLIC_URL`; redeploy the current image |
| Build fails on `readme`/`LICENSE` | Dockerfile omitted project-build inputs | `deploy/cloudrun/Dockerfile` copies both |
