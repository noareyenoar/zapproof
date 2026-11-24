#!/bin/sh
# Documented fallback credential scan — prompt Section 6, task 0.2.
#
# Used only when gitleaks is unavailable. Scans the STAGED diff for
# high-confidence credential shapes and exits non-zero if any is found.
#
# Design note: this blocks only on patterns that are near-certainly credentials.
# A scanner that cries wolf gets bypassed with `--no-verify`, which is worse than
# a narrow scanner, because then nothing is scanned at all.

staged=$(git diff --cached --no-color --diff-filter=ACM)
if [ -z "$staged" ]; then
    echo "leak-check: nothing staged"
    exit 0
fi

fail=0

check() {
    name="$1"
    pattern="$2"
    hits=$(printf '%s\n' "$staged" | grep -nE "$pattern" || true)
    if [ -n "$hits" ]; then
        echo "leak-check: BLOCKED by rule [$name]"
        printf '%s\n' "$hits" | head -5
        fail=1
    fi
}

# PEM-encoded key material of any kind.
check "pem-private-key"   'BEGIN [A-Z0-9 ]*PRIVATE KEY'

# Bitcoin / Lightning key material.
check "xprv"              'xprv[0-9a-zA-Z]{50,}'
check "wif-private-key"   '(^|[^1-9A-HJ-NP-Za-km-z])[5KL][1-9A-HJ-NP-Za-km-z]{50}([^1-9A-HJ-NP-Za-km-z]|$)'

# Nostr secret keys.
check "nostr-nsec"        'nsec1[02-9ac-hj-np-z]{20,}'

# Seed phrases / mnemonics assigned to something.
check "mnemonic"          '(seed|mnemonic|mnem)[[:space:]]*[:=][[:space:]]*["'"'"'][a-z]+([[:space:]]+[a-z]+){11,}'

# Generic long-lived API credentials.
check "vendor-api-key"    'sk-[A-Za-z0-9]{20,}'
check "github-token"      'gh[pousr]_[A-Za-z0-9]{36,}'
check "aws-access-key"    'AKIA[0-9A-Z]{16}'
check "slack-token"       'xox[baprs]-[0-9A-Za-z-]{10,}'

if [ "$fail" -ne 0 ]; then
    echo ""
    echo "leak-check: potential credential in staged changes. Commit blocked."
    echo "If this is a false positive, use 'git commit --no-verify' AND record why"
    echo "in the owner decision record — do not make that a habit."
    exit 1
fi

echo "leak-check: clean"
exit 0
