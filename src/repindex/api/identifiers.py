"""Identifier normalisation: EIP-55 addresses, DIDs and non-indexed rails (task B1).

The published contract says an identifier may be an EIP-55 address or a DID. The
independent Gate B review found that only addresses were accepted and that the DID
half of the contract had never been implemented (F14). This module implements it in
one place so the API, the MCP tool and future callers cannot disagree about what an
identifier is.

Since 2026-11-25 the module also *classifies* identifiers that belong to rails this
index does not cover (Lightning node pubkeys, Lightning addresses, MPP operators).
Those must be answered with `rail_not_indexed`, never with `invalid_identifier`:
the identifier is well-formed, the coverage is missing. Base evidence is never
returned for them.

The same date added Solana addresses (base58, 32 bytes). They are a separate rail
with a separate database: a Solana answer never carries Base evidence.

A DID is normalised to the address it names; the index stores addresses, not DIDs.
That is a real limitation, not an implementation detail: two DIDs that name the same
address resolve to one record, and a DID method this module does not understand is
rejected rather than guessed at.

A DID can also name a chain. The index covers Base only, so a DID qualified with
another chain — `did:pkh:eip155:1:...`, `did:ethr:mainnet:...` — is a different
subject and is rejected. The second independent review found that this module used to
discard the qualifier and answer with the Base-chain record, which is an unknown
rendered as a value at the rail level (G1).
"""

from __future__ import annotations

import re

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# 33-byte compressed Lightning node public key, hex-encoded (66 hex characters).
LIGHTNING_PUBKEY_RE = re.compile(r"^(02|03)[0-9a-fA-F]{64}$")
# LNURL / Lightning address form is user@domain; the domain must look like a domain.
LIGHTNING_ADDRESS_RE = re.compile(
    r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$"
)
MPP_PREFIX = "mpp:"

# Solana addresses are base58-encoded 32-byte account keys. The alphabet below omits
# 0, O, I and l, and a well-formed value decodes to exactly 32 bytes. Checking both
# keeps an arbitrary 32-44 character token from being reported as a Solana address.
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# Identifier kinds. Base and Solana have an index; the others exist so a caller
# receives "missing coverage" (rail_not_indexed) instead of "bad input".
KIND_EVM = "evm"
KIND_SOLANA = "solana_address"
KIND_LIGHTNING_NODE_PUBKEY = "lightning_node_pubkey"
KIND_LIGHTNING_ADDRESS = "lightning_address"
KIND_MPP_OPERATOR = "mpp_operator"

RAIL_BY_KIND = {
    KIND_EVM: "base",
    KIND_SOLANA: "solana",
    KIND_LIGHTNING_NODE_PUBKEY: "lightning",
    KIND_LIGHTNING_ADDRESS: "lightning",
    KIND_MPP_OPERATOR: "mpp",
}


def _base58_ok(value: str) -> bool:
    """True when `value` is base58 and decodes to exactly 32 bytes."""
    number = 0
    for char in value:
        index = BASE58_ALPHABET.find(char)
        if index < 0:
            return False
        number = number * 58 + index
    leading = len(value) - len(value.lstrip("1"))
    return leading + (number.bit_length() + 7) // 8 == 32

# did:ethr:<address> or did:ethr:<network>:<address>. A bare did:ethr names no chain,
# so it carries no other-chain claim and is accepted. When a network is present it
# must be Base (chain id 8453, or its canonical hex form); any other network names a
# different subject and is rejected rather than answered with Base data.
DID_ETHR_PREFIX = "did:ethr:"
DID_PKH_PREFIX = "did:pkh:eip155:"
BASE_CHAIN_ID = "8453"
BASE_CHAIN_HEX = "0x2105"


class InvalidIdentifier(ValueError):
    """The identifier does not name an EVM address this index can look up."""


def _eip55_ok(address: str) -> bool:
    try:
        from eth_utils import is_checksum_address  # type: ignore[attr-defined]  # noqa: PLC0415
    except ImportError:
        return False
    return bool(is_checksum_address(address))


def _extract_address(identifier: str) -> str:
    """Return the 0x-address a plain address or a supported DID names."""
    if identifier.startswith(DID_ETHR_PREFIX):
        parts = identifier[len(DID_ETHR_PREFIX) :].split(":")
        if not parts or not parts[-1]:
            raise InvalidIdentifier("did:ethr must end with a 0x-prefixed address")
        if len(parts) == 2:
            network = parts[0].lower()
            if network not in (BASE_CHAIN_ID, BASE_CHAIN_HEX):
                raise InvalidIdentifier(
                    "did:ethr names a chain this index does not cover; only Base "
                    "evidence is indexed (use did:ethr:8453:<address> or drop the "
                    "network segment)"
                )
        elif len(parts) != 1:
            raise InvalidIdentifier(
                "did:ethr must be did:ethr:<address> or did:ethr:<network>:<address>"
            )
        return parts[-1]

    if identifier.startswith(DID_PKH_PREFIX):
        parts = identifier[len(DID_PKH_PREFIX) :].split(":")
        if len(parts) != 2 or not parts[0].isdigit():
            raise InvalidIdentifier(
                "did:pkh must be did:pkh:eip155:<chain-id>:<0x-address>"
            )
        if parts[0] != BASE_CHAIN_ID:
            raise InvalidIdentifier(
                "did:pkh names a chain this index does not cover; only Base "
                "evidence is indexed (eip155:8453)"
            )
        return parts[1]

    if identifier.startswith("did:"):
        raise InvalidIdentifier(
            "unsupported DID method; this index resolves did:ethr and "
            "did:pkh:eip155 identifiers only"
        )

    return identifier


def normalise_identifier(identifier: str) -> str:
    """Return the lowercase address for a plain address or a supported DID.

    Checksum handling: a mixed-case address must pass EIP-55, which needs keccak-256.
    If no keccak implementation is importable, mixed case is rejected rather than
    accepted unverified — an unverified checksum is worse than none, because it looks
    like validation happened.
    """
    address = _extract_address(identifier.strip())

    if not ADDRESS_RE.match(address):
        raise InvalidIdentifier(
            "identifier must be an EIP-55 address or a Base DID naming one "
            "(did:ethr:..., did:ethr:8453:... or did:pkh:eip155:8453:...)"
        )

    body = address[2:]
    has_lower = any(c.islower() for c in body if c.isalpha())
    has_upper = any(c.isupper() for c in body if c.isalpha())
    if not (has_lower and has_upper):
        return address.lower()  # all one case: no checksum to verify

    if _eip55_ok(address):
        return address.lower()

    raise InvalidIdentifier("EIP-55 checksum does not match")


def classify_identifier(identifier: str) -> tuple[str, str]:
    """Return `(kind, normalised)` for every identifier shape the contract names.

    `KIND_EVM` and `KIND_SOLANA` have an index behind them. The other kinds are
    recognised so that a well-formed Lightning or MPP identifier gets
    `rail_not_indexed` (missing coverage) rather than `invalid_identifier` (bad input).
    A string that matches none of the shapes still raises `InvalidIdentifier`.

    No LNURL or DNS resolution happens here: a lookup must not fetch a domain supplied
    by the caller (SSRF and query leakage). Lightning addresses are normalised
    textually only.
    """
    raw = identifier.strip()
    if LIGHTNING_PUBKEY_RE.match(raw):
        return KIND_LIGHTNING_NODE_PUBKEY, raw.lower()
    if LIGHTNING_ADDRESS_RE.match(raw):
        local, domain = raw.split("@", 1)
        return KIND_LIGHTNING_ADDRESS, f"{local.lower()}@{domain.lower()}"
    if raw.lower().startswith(MPP_PREFIX) and raw[len(MPP_PREFIX) :].strip():
        return KIND_MPP_OPERATOR, raw[len(MPP_PREFIX) :].strip()
    # Solana is case-sensitive base58: normalisation must not lowercase it.
    if _base58_ok(raw):
        return KIND_SOLANA, raw
    return KIND_EVM, normalise_identifier(raw)
