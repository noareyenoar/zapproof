"""The binding service: server-issued challenges, persisted single use, signed revoke.

The review (F5) accepted the signature primitives and rejected the claim that they
were an identity binding, because nothing issued challenges, nothing stored them,
single use was the caller's job, and revocation needed no signature. This module is
the missing service; `store.py` is the state it cannot forget between requests.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from typing import Any

from repindex.binding.store import BindingStore
from repindex.binding.verify import (
    DEFAULT_TTL_SECONDS,
    BindingError,
    Challenge,
    check_challenge_time,
    issue_challenge,
    revocation_message,
    rotation_message,
    validate_nostr_pubkey,
    verify_binding,
    verify_evm_signature,
    verify_nostr_signature,
)


class BindingService:
    def __init__(
        self,
        store: BindingStore,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.store = store
        # Injected so tests and the end-to-end script can move time instead of
        # waiting for it.
        self._clock = clock or (lambda: int(time.time()))

    def issue(
        self, *, evm_address: str, nostr_pubkey: str, ttl: int = DEFAULT_TTL_SECONDS
    ) -> Challenge:
        """Create and persist a challenge. Only a nonce this method issued can verify."""
        challenge = issue_challenge(
            evm_address=evm_address, nostr_pubkey=nostr_pubkey, now=self._clock(), ttl=ttl
        )
        self.store.save_challenge(
            nonce=challenge.nonce,
            evm_address=challenge.evm_address,
            nostr_pubkey=challenge.nostr_pubkey,
            issued_at=challenge.issued_at,
            expires_at=challenge.expires_at,
        )
        return challenge

    def verify(self, *, nonce: str, evm_signature: str, nostr_signature: str) -> dict[str, Any]:
        """Verify both signatures over a stored challenge and persist the binding.

        Refusals are reason codes. The nonce is consumed with a conditional UPDATE
        after the signatures verify, so a failed attempt does not burn the challenge
        and two racing requests cannot both consume it.
        """
        now = self._clock()
        record = self.store.load_challenge(nonce)
        if record is None:
            raise BindingError("challenge_not_issued")

        challenge = Challenge(
            nonce=record["nonce"],
            evm_address=record["evm_address"],
            nostr_pubkey=record["nostr_pubkey"],
            issued_at=int(record["issued_at"]),
            expires_at=int(record["expires_at"]),
        )
        if record["consumed_at"] is not None:
            raise BindingError("challenge_already_used")
        check_challenge_time(challenge, now)

        existing = self.store.get_binding(challenge.evm_address)
        if existing is not None and existing["revoked_at"] is None:
            raise BindingError("already_bound")

        binding = verify_binding(
            challenge,
            evm_signature=evm_signature,
            nostr_signature=nostr_signature,
            now=now,
        )
        if not self.store.consume_challenge(nonce, now):
            raise BindingError("challenge_already_used")

        payload: dict[str, Any] = {
            "evm_address": binding.evm_address,
            "nostr_pubkey": binding.nostr_pubkey,
            "nonce": binding.nonce,
            "evm_signature": binding.evm_signature,
            "nostr_signature": binding.nostr_signature,
            "created_at": now,
        }
        try:
            if existing is not None:
                # A revoked binding may be re-established by a fresh challenge; the
                # history stays in binding_events.
                self.store.replace_binding(**payload)
                self.store.add_event(binding.evm_address, "recreated", {"nonce": nonce}, now)
                stored = self.store.get_binding(binding.evm_address) or {}
            else:
                stored = self.store.save_binding(**payload)
                self.store.add_event(binding.evm_address, "created", {"nonce": nonce}, now)
        except sqlite3.IntegrityError as exc:
            # Two requests raced past the `already_bound` check above. The loser's
            # nonce is spent; the caller sees a refusal, not a 500.
            raise BindingError("already_bound") from exc
        return stored

    def revoke(self, *, evm_address: str, by: str, signature: str) -> dict[str, Any]:
        """Revoke by either key. The side claiming to revoke must sign the revocation."""
        now = self._clock()
        address = evm_address.lower()
        binding = self.store.get_binding(address)
        if binding is None:
            raise BindingError("unknown_binding")
        if binding["revoked_at"] is not None:
            raise BindingError("already_revoked")
        if by not in ("evm", "nostr"):
            raise BindingError("revoked_by_must_be_evm_or_nostr")

        message = revocation_message(
            evm_address=address,
            nostr_pubkey=binding["nostr_pubkey"],
            created_at=int(binding["created_at"]),
            by=by,
        )
        if by == "evm":
            verify_evm_signature(address, message, signature)
        else:
            verify_nostr_signature(binding["nostr_pubkey"], message, signature)

        if not self.store.revoke_binding(address, by=by, now=now):
            raise BindingError("already_revoked")
        self.store.add_event(address, "revoked", {"by": by}, now)
        return self.store.get_binding(address) or {}

    def rotate(
        self, *, evm_address: str, new_nostr_pubkey: str, nostr_signature: str
    ) -> dict[str, Any]:
        """Replace the Nostr key, signed by the current Nostr key.

        The EVM side is unchanged, so the EVM key is not asked to sign. Whoever holds
        the current Nostr key can hand it to another; a person who has lost that key
        must revoke with the EVM key and bind again.
        """
        now = self._clock()
        address = evm_address.lower()
        binding = self.store.get_binding(address)
        if binding is None:
            raise BindingError("unknown_binding")
        if binding["revoked_at"] is not None:
            raise BindingError("already_revoked")
        validate_nostr_pubkey(new_nostr_pubkey)

        message = rotation_message(
            evm_address=address,
            old_nostr_pubkey=binding["nostr_pubkey"],
            new_nostr_pubkey=new_nostr_pubkey,
        )
        verify_nostr_signature(binding["nostr_pubkey"], message, nostr_signature)

        if not self.store.rotate_binding(
            address, new_nostr_pubkey=new_nostr_pubkey.lower(), now=now
        ):
            raise BindingError("already_revoked")
        self.store.add_event(
            address,
            "rotated",
            {"from": binding["nostr_pubkey"], "to": new_nostr_pubkey.lower()},
            now,
        )
        return self.store.get_binding(address) or {}

    def lookup(self, evm_address: str) -> dict[str, Any] | None:
        return self.store.get_binding(evm_address.lower())
