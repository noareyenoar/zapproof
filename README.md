# Zapproof

A read-only, non-custodial reputation index for autonomous AI agents, scored from
evidence of settled payments on Base, plus a separate seven-day Solana x402 window.

**Status:** the hosted API and remote MCP endpoint are live, the package is on PyPI as
`zapproof-mcp`, and the official MCP Registry entry is active
(`io.github.noareyenoar/x402-counterparty-reputation`). A Glama connector is live;
Cursor submission follows this public repository. The interpretation is rules-based
and is **not independently validated**; every answer says so.

[![Zapproof MCP server](https://glama.ai/mcp/servers/noareyenoar/zapproof/badge)](https://glama.ai/mcp/servers/noareyenoar/zapproof)

- Hosted site: <https://zapproof.web.app>
- MCP endpoint: `https://zapproof-hc75zyauja-uc.a.run.app/mcp`
- PyPI: <https://pypi.org/project/zapproof-mcp/>
- Registry search: <https://registry.modelcontextprotocol.io/v0.1/servers?search=x402-counterparty>

## What this is

Zapproof answers one question for an agent that is about to pay a stranger: *is there
evidence that this counterparty actually does business?*

Reputation is computed only from verifiable evidence of settled payments. There is no
"give stars" feature, no user-submitted rating, and no way to buy standing.

## What this is not

- **Not a custodian.** Zapproof never holds, escrows, or controls anyone else's money.
- **Not a required link in anyone's payment path.** If this project goes offline, no
  third party's payment is blocked, delayed, or degraded.
- **Not a judge of character.** It reports what the evidence shows and how confident it
  is. It does not label anyone good or bad, and an absence of evidence is never
  rendered as a low score.
- **Not a cross-rail oracle.** Base x402 settlement is indexed. The Solana x402 rail
  is indexed from a seven-day observation window (2026-11-18 to 2026-11-25) classified
  by a public fee-payer allowlist — a sample, not a census. Lightning and MPP are not
  indexed; those queries return `rail_not_indexed`, which means missing coverage, not a
  negative finding. Evidence is never relabelled from one rail to another.

## Use it

### Hosted MCP

```json
{
  "mcpServers": {
    "zapproof": {
      "type": "http",
      "url": "https://zapproof-hc75zyauja-uc.a.run.app/mcp"
    }
  }
}
```

One tool: `check_counterparty`. No key, no wallet, no write access.

### Agent plugin (Cursor and Agent Plugins clients)

This repository is itself an installable plugin:

- `.mcp.json` and `.cursor-plugin/plugin.json` register the hosted MCP endpoint for
  Cursor;
- `skills/zapproof/SKILL.md` tells an agent when to check a counterparty and how to
  read the answer;
- `skills/x402-payer/SKILL.md` is a free, vendor-neutral guide to paying an x402
  resource safely from a dedicated Base wallet, with a reference implementation in
  `scripts/preflight_payer.py`;
- `rules/zapproof.mdc` is an optional Cursor rule for pre-payment checks;
- root `plugin.json` and `mcp.json` follow the Agent Plugins 1.0 package format.

### Offline package

```sh
uvx zapproof-mcp
```

The PyPI wheel carries a seller snapshot (19,397 seller records, Base block 53,201,719,
methodology v1), so it works without a key, wallet, or network. A lookup that is not in
the snapshot returns `found: false` and `status: "unknown"` — not a zero.

### From source

```sh
git clone https://github.com/noareyenoar/zapproof.git
cd zapproof
uv sync --group dev
```

The repository does not contain the ingest database or the 29 MB snapshot, because
both are regenerable data rather than source. To run the MCP server from a checkout:

- point it at the hosted index with `ZAPPROOF_API_URL=https://zapproof-hc75zyauja-uc.a.run.app`, or
- build a snapshot from a local ingest database (see `scripts/build_snapshot.py` and
  `docs/METHODOLOGY.md`).

## Read the limits with the number

The interpretation is rules-based and is not independently validated. In a blinded
adjudication, language models agreed with it about 62% of the time, or 83% when their
abstentions are excluded. They are not human reviewers, and the sample is small.

- `found: false` and `insufficient_evidence` are not negative signals.
- A null score is not a zero.
- Lightning and MPP are not indexed. Solana coverage is one seven-day window, and the
  offline snapshot is Base-only.
- A high `signaling_likelihood` is a statement about a payment pattern, not a finding
  about an entity.

## Repository layout

| Path | What it holds |
|---|---|
| `src/repindex/` | Ingestion, features, rules, scoring, API, MCP server, binding |
| `tests/` | Unit, integration, and packaging tests |
| `gates/` | Gate scripts, written before the phase they check |
| `docs/` | Methodology, ADRs, protocol notes, deployment, cross-rail evidence |
| `reports/` | Gate evidence: Gate A, Gate B, coverage, sensitivity, planted patterns, cross-check |
| `examples/` | Payer-agent examples and request fixtures |
| `distribution/site/templates/` | Public site and agent-skill templates |
| `config/thresholds.yaml` | Human-owned thresholds; changes require a recorded rationale |
| `scripts/` | Backfill, rebuild, snapshot, cross-check, and report tools |

## Development

```sh
uv run pytest -q
uv run ruff check .
uv run mypy
uv run python gates/gate_B.py
```

Tests and linters run without network access. Gate scripts that rebuild data need a
local index or RPC access; see `docs/METHODOLOGY.md`.

## Evidence

Every published number is produced by a script from the database, with the reproduce
command beside it. The gate outputs are committed under `reports/`:

- `reports/gate_A.json` — methodology and correctness checks, including the failures.
- `reports/gate_B.json` — hosted-surface checks.
- `reports/crosscheck_A.json` — count and amount agreement with logs fetched from an
  independent Base RPC provider on three days. This checks our decoding and counting
  against independently fetched data; it is not an independent indexer.
- `reports/planted_pattern_results.json` — what the classifier catches, and the
  circularity of that test stated in the output.

## License

MIT. The scoring data is not included in the license.

ภาษาไทย: [README.th.md](README.th.md)
