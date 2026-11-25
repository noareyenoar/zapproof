"""The one methodology version string.

The independent Gate B review (F10) found the version hard-coded in seven modules.
The classifier changed on 2026-10-30 — five rules were corrected — and every module
still called the result `v0`, so a caller had no way to tell which method produced a
score. Any future change to the rules must bump this string, and only this string.
"""

from __future__ import annotations

# v1: five classifier fixes on 2026-10-30 (repeat_buyer_dominant payer ceiling,
# fixed_amount and concentrated_in_time payer-diversity abstention, reciprocity
# cutoff, relayer abstention) and the removal of the unused fresh_wallets rule.
METHODOLOGY_VERSION = "v1"
