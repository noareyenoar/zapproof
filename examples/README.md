# Examples

Each file is a working starting point, not a framework. They call the same
`check_counterparty` answer the MCP server and API return, and they show the policy
decision an agent should make with it — including when the evidence is missing.

| File | What it shows |
|---|---|
| `payer_agent.py` | A payer about to pay an unknown address: check over MCP stdio, then pay, cap the amount, or stop. |
| `seller_record.py` | A seller printing its own record as a badge-like summary. |
| `langchain_tool.py` | `check_counterparty` as a LangChain tool. |
| `crewai_tool.py` | `check_counterparty` as a CrewAI tool. |
| `x402_guard/` | A fail-open before-payment hook (Python + TypeScript) that logs a counterparty check inside the x402 client. |

Run from the repository root (source checkout, full database):

```bash
uv run python examples/payer_agent.py --identifier 0x0000000000000000000000000000000000000000 --amount 25
uv run python examples/seller_record.py 0x0000000000000000000000000000000000000000
```

Installed package: `payer_agent.py` launches `python -m repindex.mcp.server` and will
use the bundled snapshot automatically.

The policy in `payer_agent.py` is deliberately conservative and deliberately not a
verdict: `found: false` caps the amount and asks a human, it does not refuse the
payment as if the address were bad.

## What not to build on these

- Do not treat `score` as an approval. The interpretation is not independently
  validated (blinded adjudication: 61.5%, 40/65; 83.3% excluding abstentions).
- Do not parse `note` strings. Branch on `status`, `found`, `error`.
- Do not call the tool once and cache the answer forever. Every answer carries `as_of`.
