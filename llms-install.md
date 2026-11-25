# Zapproof MCP

Zapproof is a read-only MCP server that checks an address's settled-payment record
before an agent pays it: an EVM address on Base, or a Solana address on the Solana
x402 rail. It reports settlements, distinct payers, active days, and a rules-based
interpretation — and says plainly what the evidence does not prove. Unknown is not bad.

- No API key, no wallet, no write access; the package is free.
- Works offline from a bundled snapshot of 19,397 seller records (Base, as of block
  53,201,719, methodology v1).
- One tool: `check_counterparty`.

**Publication status (2026-11-25):** `zapproof-mcp 0.1.3` is on PyPI, so the `uvx`
entries below work. The hosted MCP endpoint is deployed at
`https://zapproof-hc75zyauja-uc.a.run.app/mcp`, and `scripts/smoke_hosted.py` passes all
9 checks (2026-11-25): `/v1/health`, `/v1/score` (unknown address), the Solana index,
`/v1/methodology`, `/skill.md`, `/skill.th.md`, MCP initialize, `tools/list` and
`tools/call`. The official MCP Registry entry
`io.github.noareyenoar/x402-counterparty-reputation@0.1.3` is live (status `active`)
with both the hosted remote and the PyPI package. The Thai page is live at
`https://zapproof.web.app/th/`. The public repository is
`https://github.com/noareyenoar/zapproof` (MIT).

## Install from PyPI

Add this to the MCP host's configuration. No environment variables are required.

```json
{
  "mcpServers": {
    "zapproof": {
      "command": "uvx",
      "args": ["zapproof-mcp"]
    }
  }
}
```

For VS Code's `mcp.json`, the same entry uses its own shape:

```json
{
  "servers": {
    "zapproof": {
      "type": "stdio",
      "command": "uvx",
      "args": ["zapproof-mcp"]
    }
  }
}
```

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

## Hosted endpoint (no local install)

A remote Streamable HTTP endpoint is linked from
<https://zapproof.web.app>; the current origin is
`https://zapproof-hc75zyauja-uc.a.run.app`:

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

The hosted index answers free today. A
1-sat (about 0.001 USDC on Base) x402 toll is being brought online; if a request
returns `402`, the instructions at `/skill.md` explain the single payment and retry.
A Thai translation is at `/skill.th.md`. The bundled snapshot stays free and needs no
wallet.

## Use it

Ask the agent to check an address before paying it, or call the tool directly:

```text
check_counterparty(identifier="0x0000000000000000000000000000000000000000")
```

An address with no record returns, in part:

```json
{
  "found": false,
  "status": "unknown",
  "score": null,
  "source": "snapshot",
  "coverage": "sellers",
  "note": "Not present in this offline snapshot. The snapshot covers sellers only and is a point-in-time copy: unknown here is not a low score, and it does not mean the hosted index has no record of the address."
}
```

`score: null` is not a zero. `found: false` is not a warning. `insufficient_evidence`
is not bad. Those three readings are the failure mode the tool is designed to avoid.

## Options

| Variable | Meaning |
|---|---|
| `ZAPPROOF_SNAPSHOT` | Use another snapshot database file. |
| `ZAPPROOF_API_URL` | Use a hosted index (`https://…`) instead of the snapshot. |
| `REPINDEX_DB` | Source checkouts only: path to the full index database. |

Hosted mode uses the hosted index, which answers free today; a 1-sat x402 toll is
being brought online. The bundled snapshot stays free and needs no wallet either way.

## What it cannot do

- **Solana, offline.** The bundled snapshot is Base-only, so a Solana address answers
  `status: "rail_not_indexed"` from `uvx` unless `ZAPPROOF_API_URL` points at the hosted
  server. Hosted Solana coverage is a seven-day observation window (2026-11-18 to
  2026-11-25) classified by a public fee-payer allowlist — a sample, not a census.
  Solana evidence is never Base evidence relabelled, or the reverse.
- **Lightning.** `rail="lightning"` returns `status: "rail_not_indexed"`. No Lightning
  evidence is indexed, and Base evidence is never relabelled as Lightning.
- **Payers.** The offline snapshot covers sellers only. An address that only pays is
  `unknown` locally even when the hosted index has a record of it.
- **Verdicts.** The interpretation is rules-based and is not independently validated.
  Blinded adjudication agreed with it about 62% of the time (83% excluding abstentions);
  the adjudicators were language models, not humans. Use it as one input among several,
  never as an approval to pay.

## Reproduce the snapshot

```bash
uv run python scripts/build_snapshot.py
```

The public repository is <https://github.com/noareyenoar/zapproof>. The hosted
reference is the site at <https://zapproof.web.app> and the skill at
`https://zapproof-hc75zyauja-uc.a.run.app/skill.md` (Thai: `/skill.th.md`).
