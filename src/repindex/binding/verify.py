"""Cross-rail identity binding (prompt task C6, pulled forward into Phase B).

**Why this exists.** ADR 0003 records the constraint that shapes the whole Lightning
plan: onion routing means a node sees amounts, never parties. So "pay through
Zapproof and receive a trust level" cannot rest on the payment having traversed the
node. It can only rest on the agent *presenting* an identity, which is what this
module verifies.

The flow is a two-key challenge-response:

1. The service issues a nonce and the two public keys the agent claims.
2. The agent signs one canonical, domain-separated message with **both** keys —
   an EVM key (EIP-191 personal_sign) and a Nostr/Lightning key (BIP-340 Schnorr).
3. Both signatures are verified here. Neither alone is accepted: one key proves
   nothing about the other, and accepting either singly would let someone bind a
   stranger's key to their own.

Properties the prompt requires:

- **domain separation** — the message names `zapproof.binding.v0` and the nonce, and
  revocation and rotation messages carry their own `kind`, so a signature collected
  for a challenge cannot be replayed as a revocation;
- **expiry** — a challenge older than its `expires_at` is refused, with a small
  tolerance for clock skew;
- **replay protection** — this module checks signatures; single use of a nonce is
  state, and state lives in `BindingStore` under `BindingService`. The review (F5)
  rejected the earlier design where the *caller* supplied the consumed-nonce set:
  a caller that passed an empty set could replay forever.
- **revocation by either key** — the side revoking signs a revocation message;
- **non-transferable** — a binding is two signatures over one nonce; it cannot be
  moved to another identity without both private keys.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any

DOMAIN = "zapproof.binding.v0"

# A clock can disagree by a little. Challenges are not issued in the future, so a
# small backward tolerance stops a skewed client from being rejected for a clock that
# is a few seconds slow.
CLOCK_SKEW_SECONDS = 120

DEFAULT_TTL_SECONDS = 600


class BindingError(ValueError):
    """A binding was refused. The message is the reason code."""


@dataclass(frozen=True, slots=True)
class Challenge:
    nonce: str
    evm_address: str
    nostr_pubkey: str
    issued_at: int
    expires_at: int

    def message(self) -> bytes:
        """Canonical bytes both keys sign.

        Sorted keys and no whitespace, so the bytes are identical on every machine —
        the same reason the API's ETag uses this form. Domain separation is the
        `domain` field: a signature made for another application cannot be replayed
        here, because the string differs.
        """
        payload = {
            "domain": DOMAIN,
            "evm": self.evm_address.lower(),
            "expires_at": self.expires_at,
            "issued_at": self.issued_at,
            "nonce": self.nonce,
            "nostr": self.nostr_pubkey.lower(),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True, slots=True)
class Binding:
    evm_address: str
    nostr_pubkey: str
    nonce: str
    evm_signature: str
    nostr_signature: str
    created_at: int
    revoked_at: int | None = None
    revoked_by: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def issue_challenge(
    *,
    evm_address: str,
    nostr_pubkey: str,
    now: int,
    ttl: int = DEFAULT_TTL_SECONDS,
) -> Challenge:
    """Create a single-use challenge. `BindingService.issue` persists it before delivery."""
    _validate_evm(evm_address)
    validate_nostr_pubkey(nostr_pubkey)
    if ttl <= 0:
        raise BindingError("ttl must be positive")
    return Challenge(
        nonce="0x" + secrets.token_hex(32),
        evm_address=evm_address.lower(),
        nostr_pubkey=nostr_pubkey.lower(),
        issued_at=now,
        expires_at=now + ttl,
    )


def check_challenge_time(challenge: Challenge, now: int) -> None:
    """Refuse an expired or future-dated challenge. Single use is the store's job."""
    if now > challenge.expires_at:
        raise BindingError("challenge_expired")
    if now < challenge.issued_at - CLOCK_SKEW_SECONDS:
        raise BindingError("challenge_not_yet_valid")


def verify_binding(
    challenge: Challenge,
    *,
    evm_signature: str,
    nostr_signature: str,
    now: int,
) -> Binding:
    """Verify both signatures over the challenge.

    Raises BindingError with a stable reason code on any refusal. The order of the
    checks is deliberate: cheap structural checks first, signature work last. The
    challenge must have been issued by the service — there is no path that accepts a
    caller-constructed `Challenge` into a stored binding, because the nonce is looked
    up in `BindingStore` before this function is reached.
    """
    check_challenge_time(challenge, now)

    if not evm_signature or not nostr_signature:
        raise BindingError("both_keys_required")

    message = challenge.message()

    verify_evm_signature(challenge.evm_address, message, evm_signature)
    verify_nostr_signature(challenge.nostr_pubkey, message, nostr_signature)

    return Binding(
        evm_address=challenge.evm_address,
        nostr_pubkey=challenge.nostr_pubkey,
        nonce=challenge.nonce,
        evm_signature=evm_signature,
        nostr_signature=nostr_signature,
        created_at=now,
    )


def revocation_message(*, evm_address: str, nostr_pubkey: str, created_at: int, by: str) -> bytes:
    """Canonical bytes the revoking key signs. `kind` stops cross-use with a challenge."""
    payload = {
        "by": by,
        "created_at": created_at,
        "domain": DOMAIN,
        "evm": evm_address.lower(),
        "kind": "revocation",
        "nostr": nostr_pubkey.lower(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def rotation_message(*, evm_address: str, old_nostr_pubkey: str, new_nostr_pubkey: str) -> bytes:
    """Canonical bytes the *current* Nostr key signs to hand the binding to a new one."""
    payload = {
        "domain": DOMAIN,
        "evm": evm_address.lower(),
        "kind": "rotation",
        "new_nostr": new_nostr_pubkey.lower(),
        "old_nostr": old_nostr_pubkey.lower(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def is_active(binding: Binding) -> bool:
    return binding.revoked_at is None


# --------------------------------------------------------------------------- #
# signature verification
# --------------------------------------------------------------------------- #


def verify_evm_signature(address: str, message: bytes, signature: str) -> None:
    from eth_account import Account  # noqa: PLC0415
    from eth_account.messages import encode_defunct  # noqa: PLC0415

    try:
        recovered = Account.recover_message(encode_defunct(primitive=message), signature=signature)
    except Exception as exc:  # noqa: BLE001
        # eth_utils raises its own ValidationError, which is not a ValueError in every
        # version. A malformed signature must be a refusal with a reason code, never a
        # 500: the caller is an untrusted agent, and a crash here is a denial-of-service
        # trigger.
        raise BindingError("evm_signature_malformed") from exc

    if recovered.lower() != address.lower():
        raise BindingError("evm_signature_does_not_match_address")


def verify_nostr_signature(pubkey: str, message: bytes, signature: str) -> None:
    from coincurve import PublicKeyXOnly  # noqa: PLC0415

    try:
        public = PublicKeyXOnly(bytes.fromhex(_strip_0x(pubkey)))
        signature_bytes = bytes.fromhex(_strip_0x(signature))
    except ValueError as exc:
        raise BindingError("nostr_signature_malformed") from exc

    if len(signature_bytes) != 64:
        raise BindingError("nostr_signature_malformed")

    # BIP-340 signs a 32-byte digest. Nostr uses sha256 of the event or, for a simple
    # challenge, of the message bytes.
    digest = hashlib.sha256(message).digest()
    if not public.verify(signature_bytes, digest):
        raise BindingError("nostr_signature_does_not_match_pubkey")


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


def _strip_0x(value: str) -> str:
    return value[2:] if value.startswith("0x") else value


def _validate_evm(address: str) -> None:
    body = _strip_0x(address)
    if len(body) != 40:
        raise BindingError("evm_address_must_be_20_bytes")
    try:
        bytes.fromhex(body)
    except ValueError as exc:
        raise BindingError("evm_address_is_not_hex") from exc


def validate_nostr_pubkey(pubkey: str) -> None:
    body = _strip_0x(pubkey)
    if len(body) != 64:
        raise BindingError("nostr_pubkey_must_be_32_bytes_xonly")
    try:
        bytes.fromhex(body)
    except ValueError as exc:
        raise BindingError("nostr_pubkey_is_not_hex") from exc
