"""Public Phase B analysis (prompt task B4; gate check GB.16).

Derives an aggregate-only report and three static charts from the index. No address,
transaction hash or other identifier is published: the reader learns about the
population, never about an identifiable seller.

The charts are hand-built SVG rather than a plotting dependency. That keeps the output
diffable in review, needs no new package, and lets the palette and mark specs follow the
project's design reference exactly.

Run:  uv run python scripts/public_analysis.py
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from repindex import adjudication
from repindex.common.db import connect_readonly
from repindex.common.provenance import (
    CommitObserver,
    DatabaseChangedError,
    PendingWALError,
    attest_identity,
    data_version,
    provenance,
    require_no_commits,
)
from repindex.common.version import METHODOLOGY_VERSION

REPO_ROOT = Path(__file__).resolve().parent.parent
DB = REPO_ROOT / "data" / "repindex.db"
ANALYSIS_DIR = REPO_ROOT / "reports" / "analysis"
OUT = REPO_ROOT / "reports" / "phase_B_public_analysis.md"

# Design reference (dataviz skill, reference palette). Single series throughout, so
# the categorical slot-1 blue is the only data colour; both steps validated against
# their own surface (light #fcfcfb, dark #1a1a19).
STYLE = """
<style>
.surface{fill:#fcfcfb}
.ink{fill:#0b0b0b}
.secondary{fill:#52514e}
.muted{fill:#898781}
.grid{stroke:#e1e0d9;stroke-width:1}
.axis{stroke:#c3c2b7;stroke-width:1}
.series{fill:#2a78d6;stroke:#2a78d6}
.series-fill{fill:#2a78d6;fill-opacity:.10;stroke:none}
.line{fill:none;stroke:#2a78d6;stroke-width:2}
.ring{stroke:#fcfcfb;stroke-width:2}
@media (prefers-color-scheme: dark){
.surface{fill:#1a1a19}
.ink{fill:#f0efec}
.secondary{fill:#c3c2b7}
.muted{fill:#898781}
.grid{stroke:#2c2c2a}
.axis{stroke:#383835}
.series{fill:#3987e5;stroke:#3987e5}
.series-fill{fill:#3987e5}
.line{stroke:#3987e5}
.ring{stroke:#1a1a19}
}
</style>
"""

FONT = 'font-family="system-ui, -apple-system, Segoe UI, sans-serif"'

RULES = {
    "fixed_amount": "Fixed amount",
    "bursty": "Bursty activity",
    "thin_payer_base": "Thin payer base",
    "concentrated_in_time": "Concentrated in time",
    "repeat_buyer_dominant": "Repeat-buyer dominant",
    "self_loop": "Self-payment",
    "round_trip": "Round trip",
    "single_relayer": "Single relayer",
}


# --------------------------------------------------------------------------- #
# SVG primitives
# --------------------------------------------------------------------------- #


def _open_svg(width: int, height: int, title: str, desc: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" aria-labelledby="t d" {FONT}>',
        f'<title id="t">{title}</title>',
        f"<desc>{desc}</desc>",
        STYLE,
        f'<rect class="surface" width="{width}" height="{height}" rx="8"/>',
    ]


def _rounded_right(x: float, y: float, w: float, h: float, r: float = 4.0) -> str:
    r = min(r, w, h / 2)
    return (
        f"M {x:.2f} {y:.2f} H {x + w - r:.2f} Q {x + w:.2f} {y:.2f} {x + w:.2f} {y + r:.2f} "
        f"V {y + h - r:.2f} Q {x + w:.2f} {y + h:.2f} {x + w - r:.2f} {y + h:.2f} "
        f"H {x:.2f} Z"
    )


def _rounded_top(x: float, y: float, w: float, h: float, r: float = 4.0) -> str:
    r = min(r, w / 2, h)
    return (
        f"M {x:.2f} {y + h:.2f} V {y + r:.2f} Q {x:.2f} {y:.2f} {x + r:.2f} {y:.2f} "
        f"H {x + w - r:.2f} Q {x + w:.2f} {y:.2f} {x + w:.2f} {y + r:.2f} "
        f"V {y + h:.2f} Z"
    )


def _tick(value: float) -> str:
    return f"{value:,.0f}"


# --------------------------------------------------------------------------- #
# chart 1 — rule prevalence (ranked horizontal bars)
# --------------------------------------------------------------------------- #


def rule_chart(rules: list[tuple[str, int]], total_sellers: int) -> str:
    width, top, row, bar_h = 760, 88, 34, 18
    left, right = 214, 74
    plot_w = width - left - right
    height = top + row * len(rules) + 30

    percents = [(name, 100 * count / total_sellers) for name, count in rules]
    axis_max = max(0.5, math.ceil(max(p for _, p in percents) * 1.2 * 2) / 2)

    parts = _open_svg(
        width,
        height,
        "Share of sellers where each classifier rule fired",
        f"Horizontal bars over {total_sellers:,} classified sellers. "
        "Values are percentages and are printed beside each bar.",
    )
    parts.append('<text class="ink" x="28" y="34" font-size="17" font-weight="600">'
                 "Which signals the classifier finds</text>")
    parts.append(
        f'<text class="secondary" x="28" y="56" font-size="12">'
        f"Share of {total_sellers:,} classified sellers where each rule fired. "
        "A seller can trigger more than one.</text>"
    )

    plot_bottom = top + row * len(rules)
    ticks = [axis_max * i / 3 for i in range(4)]
    for tick in ticks:
        x = left + (tick / axis_max) * plot_w
        parts.append(
            f'<line class="grid" x1="{x:.0f}" y1="{top - 10}" x2="{x:.0f}" y2="{plot_bottom}"/>'
        )
        label = f"{tick:g}%"
        parts.append(
            f'<text class="muted" x="{x:.0f}" y="{plot_bottom + 18}" font-size="11" '
            f'text-anchor="middle" style="font-variant-numeric:tabular-nums">{label}</text>'
        )

    for index, (name, percent) in enumerate(percents):
        y = top + index * row
        centre = y + bar_h / 2
        parts.append(
            f'<text class="ink" x="{left - 14}" y="{centre:.1f}" font-size="13" '
            f'text-anchor="end" dominant-baseline="middle">{RULES.get(name, name)}</text>'
        )
        w = (percent / axis_max) * plot_w
        if w > 0.5:
            parts.append(f'<path class="series" d="{_rounded_right(left, y, w, bar_h)}"/>')
        value_x = left + w + 8
        parts.append(
            f'<text class="secondary" x="{value_x:.1f}" y="{centre:.1f}" font-size="12" '
            f'dominant-baseline="middle" style="font-variant-numeric:tabular-nums">'
            f"{percent:.2f}%</text>"
        )

    parts.append("</svg>")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# chart 2 — evidence per seller (columns)
# --------------------------------------------------------------------------- #


def evidence_chart(bins: list[tuple[str, int]]) -> str:
    width, height = 760, 380
    left, right, top, bottom = 66, 24, 88, 56
    plot_w = width - left - right
    plot_h = height - top - bottom
    base = top + plot_h

    total = sum(count for _, count in bins)
    percents = [(label, 100 * count / total) for label, count in bins]
    axis_max = max(10, math.ceil(max(p for _, p in percents) / 20) * 20)

    parts = _open_svg(
        width,
        height,
        "Sellers by number of observed settlements",
        f"Column chart over {total:,} sellers in six settlement-count bands.",
    )
    parts.append('<text class="ink" x="28" y="34" font-size="17" font-weight="600">'
                 "How much evidence most sellers have</text>")
    parts.append(
        f'<text class="secondary" x="28" y="56" font-size="12">'
        f"Share of {total:,} sellers in each band of observed settlements.</text>"
    )

    ticks = [axis_max * i / 4 for i in range(5)]
    for tick in ticks:
        y = base - (tick / axis_max) * plot_h
        parts.append(f'<line class="grid" x1="{left}" y1="{y:.1f}" x2="{width - right}" '
                     f'y2="{y:.1f}"/>')
        parts.append(
            f'<text class="muted" x="{left - 10}" y="{y:.1f}" font-size="11" '
            f'text-anchor="end" dominant-baseline="middle" '
            f'style="font-variant-numeric:tabular-nums">{tick:g}%</text>'
        )

    slot = plot_w / len(bins)
    bar_w = min(72.0, slot - 24)
    for index, (label, percent) in enumerate(percents):
        centre = left + slot * index + slot / 2
        h = (percent / axis_max) * plot_h
        x = centre - bar_w / 2
        if h > 0.5:
            parts.append(f'<path class="series" d="{_rounded_top(x, base - h, bar_w, h)}"/>')
        parts.append(
            f'<text class="secondary" x="{centre:.1f}" y="{base - h - 9:.1f}" font-size="11" '
            f'text-anchor="middle" style="font-variant-numeric:tabular-nums">'
            f"{percent:.1f}%</text>"
        )
        parts.append(
            f'<text class="muted" x="{centre:.1f}" y="{base + 20}" font-size="11" '
            f'text-anchor="middle">{label}</text>'
        )

    parts.append(f'<line class="axis" x1="{left}" y1="{base}" x2="{width - right}" y2="{base}"/>')
    parts.append("</svg>")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# chart 3 — payments per day (line)
# --------------------------------------------------------------------------- #


def daily_chart(days: list[tuple[str, int]]) -> str:
    width, height = 760, 380
    left, right, top, bottom = 74, 34, 88, 52
    plot_w = width - left - right
    plot_h = height - top - bottom
    base = top + plot_h

    values = [count for _, count in days]
    axis_max = max(100_000, math.ceil(max(values) / 200_000) * 200_000)

    parts = _open_svg(
        width,
        height,
        "Settled payments per day",
        f"Line chart of daily settled payment counts from {days[0][0]} to {days[-1][0]}.",
    )
    parts.append('<text class="ink" x="28" y="34" font-size="17" font-weight="600">'
                 "Settled payments per day</text>")
    parts.append(
        f'<text class="secondary" x="28" y="56" font-size="12">'
        f"Payments observed on Base from {days[0][0]} to {days[-1][0]}.</text>"
    )

    ticks = [axis_max * i / 3 for i in range(4)]
    for tick in ticks:
        y = base - (tick / axis_max) * plot_h
        parts.append(f'<line class="grid" x1="{left}" y1="{y:.1f}" x2="{width - right}" '
                     f'y2="{y:.1f}"/>')
        label = f"{tick / 1000:,.0f}k" if tick else "0"
        parts.append(
            f'<text class="muted" x="{left - 10}" y="{y:.1f}" font-size="11" '
            f'text-anchor="end" dominant-baseline="middle" '
            f'style="font-variant-numeric:tabular-nums">{label}</text>'
        )

    step = plot_w / max(1, len(days) - 1)
    points = [
        (left + step * index, base - (count / axis_max) * plot_h)
        for index, count in enumerate(values)
    ]
    line = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in points)
    area = (
        line
        + f" L {points[-1][0]:.1f} {base:.1f} L {points[0][0]:.1f} {base:.1f} Z"
    )
    parts.append(f'<path class="series-fill" d="{area}"/>')
    parts.append(f'<path class="line" d="{line}"/>')

    for x, y in points:
        parts.append(f'<circle class="series ring" cx="{x:.1f}" cy="{y:.1f}" r="4"/>')

    for index, label in enumerate([day for day, _ in days]):
        x = left + step * index
        display = datetime.strptime(label, "%Y-%m-%d").strftime("%b %d")
        parts.append(
            f'<text class="muted" x="{x:.1f}" y="{base + 22}" font-size="11" '
            f'text-anchor="middle">{display}</text>'
        )

    peak = max(range(len(values)), key=lambda i: values[i])
    for index, anchor, offset in ((peak, "middle", -14), (len(values) - 1, "end", -14)):
        x, y = points[index]
        parts.append(
            f'<text class="secondary" x="{x + (4 if anchor == "end" else 0):.1f}" '
            f'y="{y + offset:.1f}" font-size="11" text-anchor="{anchor}" '
            f'style="font-variant-numeric:tabular-nums">{values[index]:,}</text>'
        )

    parts.append(f'<line class="axis" x1="{left}" y1="{base}" x2="{width - right}" y2="{base}"/>')
    parts.append("</svg>")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #


def _read(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    return conn.execute(sql, params).fetchone()


def collect() -> dict[str, Any]:
    version_before = -1
    version_after = -1
    conn = connect_readonly(DB)
    try:
        version_before = data_version(conn)
        settlements = int(_read(conn, "SELECT COUNT(*) FROM settlements")[0])
        sellers = int(_read(conn, "SELECT COUNT(*) FROM features")[0])
        start, end = _read(conn, "SELECT MIN(block_time), MAX(block_time) FROM settlements")
        as_of_block = int(_read(conn, "SELECT MAX(block_number) FROM settlements")[0])

        statuses = {
            str(row[0]): int(row[1])
            for row in conn.execute("SELECT status, COUNT(*) FROM scores GROUP BY status")
        }

        # Every rule appears in the table even at zero: `single_relayer` abstains when
        # no relayer was resolved, and a rule that never fires is a finding, not a gap.
        fired: Counter[str] = Counter(dict.fromkeys(RULES, 0))
        flagged = 0
        overrides = 0
        for (raw,) in conn.execute("SELECT contributions FROM classifications"):
            contributions = json.loads(raw)
            # H3: `dispute_override` is a correction, not a classifier rule. Counting
            # every fired contribution invented a ninth rule in the public table and
            # let an override alone mark a seller as having triggered a rule.
            rules_fired = [
                item
                for item in contributions
                if item.get("fired") and item.get("rule") in RULES
            ]
            if rules_fired:
                flagged += 1
            for item in rules_fired:
                fired[str(item["rule"])] += 1
            overrides += sum(
                1
                for item in contributions
                if item.get("fired") and item.get("rule") == "dispute_override"
            )

        bins = [
            ("1", 1, 1),
            ("2-9", 2, 9),
            ("10-99", 10, 99),
            ("100-999", 100, 999),
            ("1,000-9,999", 1_000, 9_999),
            ("10,000+", 10_000, 2**62),
        ]
        bin_counts = [
            (label, int(_read(
                conn,
                "SELECT COUNT(*) FROM features WHERE settlement_count BETWEEN ? AND ?",
                (low, high),
            )[0]))
            for label, low, high in bins
        ]

        days = [
            (str(row[0]), int(row[1]))
            for row in conn.execute(
                "SELECT date(block_time, 'unixepoch') AS day, COUNT(*) "
                "FROM settlements GROUP BY day ORDER BY day"
            )
        ]
        scored = [
            float(row[0])
            for row in conn.execute("SELECT score FROM scores WHERE status = 'scored'")
        ]
        tokens = int(_read(conn, "SELECT COUNT(DISTINCT token) FROM settlements")[0])
        version_after = data_version(conn)
    finally:
        conn.close()

    # O1/O2: a commit made by another connection during these reads makes the
    # aggregates a mixture of two states even if the file's bytes are later restored.
    require_no_commits(DB, version_before, version_after)

    return {
        "settlements": settlements,
        "sellers": sellers,
        "start": datetime.fromtimestamp(int(start), UTC).date(),
        "end": datetime.fromtimestamp(int(end), UTC).date(),
        "span_days": round((int(end) - int(start)) / 86400, 1),
        "as_of_block": as_of_block,
        "statuses": statuses,
        "rule_counts": fired,
        "flagged": flagged,
        "overrides": overrides,
        "bins": bin_counts,
        "days": days,
        "score_min": min(scored) if scored else None,
        "score_max": max(scored) if scored else None,
        "score_mean": (sum(scored) / len(scored)) if scored else None,
        "tokens": tokens,
    }


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def render(data: dict[str, Any], *, commit: str, fingerprint: str) -> str:
    sellers = data["sellers"]
    scored = data["statuses"].get("scored", 0)
    insufficient = data["statuses"].get("insufficient_evidence", 0)
    rule_rows = sorted(data["rule_counts"].items(), key=lambda item: (-item[1], item[0]))
    rules = [(name, count) for name, count in rule_rows]

    rule_lines = "\n".join(
        f"| {RULES.get(name, name)} | {count:,} | {100 * count / sellers:.2f}% |"
        for name, count in rules
    )
    override_note = ""
    if data.get("overrides"):
        override_note = (
            f"\n**{data['overrides']:,} seller(s)** carry an upheld classification "
            "dispute. An override is a correction, not one of the eight rules, and is "
            "not counted in the table or the percentage above.\n"
        )
    bin_lines = "\n".join(
        f"| {label} | {count:,} | {100 * count / sellers:.1f}% |"
        for label, count in data["bins"]
    )
    day_lines = "\n".join(
        f"| {datetime.strptime(day, '%Y-%m-%d').strftime('%b %d')} | {count:,} |"
        for day, count in data["days"]
    )

    return f"""# Zapproof: what the settled payment data shows

*A public Phase B analysis, methodology `v1`. Generated {date.today().isoformat()} from
the index at block {data['as_of_block']:,}.*

## What this is

Zapproof reads settled payments made by software agents and summarises the payment
record of the seller on the other side. The product answers one narrow question with
evidence: **how much settled payment activity does this address have, and how much of it
looks like the seller paying itself rather than a real customer?**

It is not a credit score, a dishonesty verdict or a judgement about a person or
company. A
seller with no record is **unknown, not bad**. The service returns `found: false` rather
than a score of zero.

## The data behind this analysis

- **{data['settlements']:,} settled payments** made through one settlement mechanism on
  Base, from {data['start'].isoformat()} to {data['end'].isoformat()}.
- **{sellers:,} seller addresses** with enough activity to classify.
- **{scored:,} sellers** met the evidence minimum and received a score;
  **{insufficient:,}** had some evidence but not enough and received
  `insufficient_evidence`.
- One token (USDC, 6 decimals) and one chain. Lightning is **not** indexed.

The observation window is short: the first and last settlement are
{data['span_days']:.1f} days apart. Nothing here should be read as a seasonality or
growth claim.

## What a score means in plain language

A score runs from 0 to 100. It rises with evidence — settlements, distinct payers and
active days — and falls when the payment pattern matches one of eight named signals.

A score is produced only when **all three** of these hold: at least 20 settlements, at
least 5 distinct payers, and at least 3 active days. Below any of them the answer is
`insufficient_evidence`, with no number. Each evidence dimension saturates at four times
the minimum, so sheer volume stops helping early.

The signalling side is a transparent rule set, not a trained model:

| Rule | Weight | Fires when |
|---|---|---|
| Self-payment | 0.45 | the payer and the seller are the same address |
| Thin payer base | 0.30 | many settlements from three or fewer payers |
| Repeat-buyer dominant | 0.20 | ten or fewer payers, most buying more than once |
| Fixed amount | 0.20 | near-identical amounts from a small payer set |
| Bursty activity | 0.15 | payments arrive in one burst rather than steadily |
| Concentrated in time | 0.30 | half the settlements fall inside a single hour |
| Single relayer | 0.10 | one facilitator carried almost everything |
| Round trip | 0.15 | payers also received payments from the seller |

Weights total 1.85, so several rules firing at once clamp at 1.0. That number means
"as signalling-like as this method can express", not "certainly dishonest". The weights
judgement, not measurement, and the sensitivity analysis perturbs every threshold by
±25%.

Among the {scored:,} sellers with a score, values ranged from
{data['score_min']:.1f} to {data['score_max']:.1f} (mean {data['score_mean']:.1f}). The
mean is high because most scored sellers have substantial, diverse payment histories;
the signals below are rare.

## What the rules find

![Share of sellers where each classifier rule fired](analysis/rule_prevalence.svg)

| Signal | Sellers | Share |
|---|---:|---:|
{rule_lines}

**{(100 * data['flagged'] / sellers):.1f}% of sellers** triggered at least one rule;
the rest triggered none. These are not a dishonesty rate. Each rule is a pattern worth
looking at, and several have innocent explanations the classifier itself records in its
per-rule notes.
{override_note}

## How much evidence most sellers have

![Sellers by number of observed settlements](analysis/seller_evidence_distribution.svg)

| Settlements per seller | Sellers | Share |
|---|---:|---:|
{bin_lines}

More than half the sellers appear once. That is the population a payment index sees:
many addresses that received a single payment, and a long tail that sells repeatedly.
A single payment is not evidence of anything, which is why the score abstains rather
than guessing.

## Payments per day

![Settled payments per day](analysis/payments_per_day.svg)

| Day | Settlements |
|---|---:|
{day_lines}

Daily volume varied between {min(count for _, count in data['days']):,} and
{max(count for _, count in data['days']):,}. With {data['span_days']:.1f} days of data
there is no basis for a trend claim.

## Limitations

1. **Settlement counts are not independently cross-verified.** No zero-cost independent
   source exists. Every published number carries that caveat.
2. **An on-chain authorisation event is not provably an x402 settlement.** The
   distinction lives off-chain, and this population is defined by that event.
3. **The classifier is a rule set someone wrote down.** It cannot find a pattern nobody
   thought of.
4. **The interpretation was checked against language-model adjudicators, not humans.**
   Agreement was {adjudication.AGREEMENT_RATE:.1%} counting abstentions as disagreements
   ({adjudication.EXCLUDING_ABSTENTIONS_RATE:.1%} excluding them), and the two
   inter-rater files are identical, so the recorded inter-rater figure is not
   independent evidence. See `docs/METHODOLOGY.md`.
5. **The window is {data['span_days']:.1f} days, one chain and one token.** Results may
   not hold for another period, chain, token or rail.
6. **Synthetic validation is partly circular.** Planted-pattern tests measure whether the
   rules find patterns the same person planted; they test mechanics, not truth.

## Corrections

If you operate a seller and believe a record is wrong, `DISPUTES.md` explains how to
contest a label or score and how corrections are logged.

## Reproducing this analysis

```bash
uv sync
uv run python scripts/public_analysis.py     # writes this report and the charts
uv run python scripts/rebuild_derived.py     # recompute derived tables
```

`scripts/public_analysis.py` opens `data/repindex.db` read-only, publishes aggregates
only, and writes deterministic SVG. The charts use a single series colour and print
their values beside the marks; every value is also in the tables above.

Provenance of this run: commit `{commit}`, database fingerprint `{fingerprint}`.
"""


def main() -> int:
    if not DB.is_file():
        print(f"missing {DB}; build the index first")
        return 1

    # P1/P2: the observer opens before the attestation hash and is checked after the
    # confirming hash, so a commit that happens while the 1.6 GB file is being hashed
    # is refused even if the bytes are restored before the confirmation. `collect()`
    # keeps its own bracket around the named reads.
    try:
        with CommitObserver(DB):
            attested = attest_identity(DB)
            data = collect()
            prov = provenance(DB, attested)
    except (PendingWALError, DatabaseChangedError) as exc:
        print(f"{exc} The analysis is not written from a state it cannot name.")
        return 1
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)

    charts = (
        "rule_prevalence.svg",
        "seller_evidence_distribution.svg",
        "payments_per_day.svg",
    )
    (ANALYSIS_DIR / charts[0]).write_text(
        rule_chart(
            sorted(data["rule_counts"].items(), key=lambda item: (-item[1], item[0])),
            data["sellers"],
        ),
        encoding="utf-8",
    )
    (ANALYSIS_DIR / charts[1]).write_text(
        evidence_chart(data["bins"]), encoding="utf-8"
    )
    (ANALYSIS_DIR / charts[2]).write_text(daily_chart(data["days"]), encoding="utf-8")
    OUT.write_text(
        render(
            data,
            commit=str(prov["commit"]),
            fingerprint=str(prov["db_sha256"] or "unavailable")[:16],
        ),
        encoding="utf-8",
    )
    (ANALYSIS_DIR / "provenance.json").write_text(
        json.dumps(
            {
                **prov,
                "report": "reports/phase_B_public_analysis.md",
                "charts": [f"reports/analysis/{name}" for name in charts],
                "methodology_version": METHODOLOGY_VERSION,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        f"wrote {OUT.relative_to(REPO_ROOT)} and 3 charts under "
        f"{ANALYSIS_DIR.relative_to(REPO_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
