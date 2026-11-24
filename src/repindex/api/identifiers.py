"""Identifier normalisation: EIP-55 addresses and DIDs (prompt task B1).

The published contract says an identifier may be an EIP-55 address or a DID. The
independent Gate B review found that only addresses were accepted and that the DID
half of the contract had never been implemented (F14). This module implements it in
one place so the API, the MCP tool and future callers cannot disagree about what an
identifier is.

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
