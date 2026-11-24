# Running Zapproof locally

Two paths. **The first is the normal one**; Docker is optional and exists only so the
service can be put in a container without knowing Docker.

Everything below assumes the database exists at `data/repindex.db`. Build it with
`scripts/run_phase_a.py` (ingest) and `scripts/rebuild_derived.py` (derived tables).
The API opens the score database **read-only** and has no write routes. Usage telemetry
is a separate, best-effort write path (see below) and never touches it.

---

## Path 1 — plain uvicorn (primary)

```bash
uv sync
uv run uvicorn repindex.api.app:app --host 127.0.0.1 --port 8000
```

Check it:

```bash
curl -s http://127.0.0.1:8000/v1/health
curl -s http://127.0.0.1:8000/v1/score/<address>
```

The MCP tool is a separate process and needs no server:

```bash
uv run python -m repindex.mcp.server
```

### Environment

| Variable | Default | Meaning |
|---|---|---|
| `RATE_LIMIT_PER_MINUTE` | `3000` | Per-client-address request limit on every `/v1/` route (50 rps). `0` disables it and exists for tests only. |
| `USAGE_DB` | `data/usage.db` | Telemetry database. Separate from the score database, which stays read-only. |
| `USAGE_METRICS` | `1` | `0` disables request logging entirely. |
| `USAGE_SALT_ROTATION_DAYS` | `90` | How often the IP-hash salt rotates. Keep it longer than the market window or unique-caller counts inflate. |
| `USAGE_OWNER_IPS` | empty | Comma-separated owner addresses excluded by `make metrics`. |

### What "read-only" means here

- The **score database** is opened with `mode=ro` (`src/repindex/api/queries.py`).
- No route writes anything. The only `INSERT`s under `src/repindex/api/` are in
  `usage.py`, the telemetry store: a middleware, not a route, writing a *separate*
  `USAGE_DB` file. `src/repindex/mcp/` contains no write path at all.
- `POST`/`PUT`/`PATCH`/`DELETE` return `405 Method Not Allowed`.
- Gate check GB.4 hashes the score database before and after attempting those methods
  and fails if the hash changes.

Telemetry is best-effort: if `USAGE_DB` cannot be opened, the API still serves scores
and logs a warning naming the path at startup. `USAGE_METRICS=0` disables it
deliberately and silently (apart from an info log).

---

## Path 2 — Docker (optional)

```bash
docker compose up --build
```

`docker-compose.yml` builds the image in `Dockerfile`, mounts `./data` **read-only** at
`/app/data`, gives telemetry its own writable volume at `/app/telemetry`
(`USAGE_DB=/app/telemetry/usage.db`), publishes port 8000, and polls `/v1/health`
every 30 s. The score database is never copied into the image: it is 1.6 GB and
changes on rebuild, while the image should not.

If you do not have Docker installed, Path 1 is complete. Nothing in the product
requires a container.

---

## Rebuilding the data

```bash
uv run python scripts/rebuild_derived.py          # wipe + recompute derived tables
uv run python scripts/sensitivity.py              # perturb classifier thresholds
uv run python scripts/generate_report.py          # refuses stale sensitivity input
```

The report generator now compares the database hash recorded in
`reports/sensitivity.json` with the file on disk and refuses to publish a stale
headline.

---

## Usage metrics

```bash
make metrics                          # writes reports/usage_metrics.json
USAGE_OWNER_IPS=203.0.113.7 make metrics
```

Each request records the route template (not the URL), status, latency, an agent
class and a salted hash of the caller address — never the raw address, query string,
headers or payloads. `make metrics` reports unique external callers over
`market_window_days`; loopback and private addresses and the configured owner
addresses are excluded. Zero is a valid answer: it means nobody external has called.

The salt rotates every 90 days by default so hashes from different periods cannot be
joined. If a window spans a rotation, the report says so and the caller count is an
upper bound, because the same caller has a different hash on each side.

---

## Binding service (separate)

```bash
uv run uvicorn repindex.binding.api:app --host 127.0.0.1 --port 8001
```

This is the only service that writes durable identity state. It keeps its own
database (`data/bindings.db`, override with `BINDING_DB`) and is not part of the score
API's deployment. (The score API process also writes the telemetry database described
above; that is metrics, not identity.) See `docs/binding.md`.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `index database not found` | `data/repindex.db` is missing; run the ingest. |
| `429` on every route | `RATE_LIMIT_PER_MINUTE` is set very low, or many clients share one address behind a proxy. |
| `422` on a valid-looking address | Mixed case is checked as EIP-55. Lowercase or correctly checksummed input both work; a wrong checksum is rejected. |
| `rail_not_indexed` from MCP | `rail="lightning"` was requested. Only Base is indexed; Base data is never returned under another rail. |
