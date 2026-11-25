"""Tests for cross-rail identity binding (prompt task C6, gate check GB.13).

The review (F5) found that these tests proved the signature primitives and then
called that an identity binding: nothing issued a challenge, nothing persisted it,
replay protection was a set the *caller* passed, and revocation needed no signature.
The tests below drive `BindingService` and its store, which is where those properties
now live. The primitive tests at the bottom remain: they are checked against the
specifications, not against each other.

Run:  uv run pytest tests/test_binding.py -q
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from coincurve import PrivateKey, PublicKeyXOnly
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from repindex.binding.api import app, get_service
from repindex.binding.service import BindingService
from repindex.binding.store import BindingStore
from repindex.binding.verify import (
    BindingError,
    issue_challenge,
    revocation_message,
    rotation_message,
)

NOW = 1_800_000_000
SECKEY = "0000000000000000000000000000000000000000000000000000000000000003"


# --------------------------------------------------------------------------- #
# helpers: sign the way a real agent would
# --------------------------------------------------------------------------- #


class Clock:
    """A clock the tests move by hand, so expiry costs no wall time."""

    def __init__(self, now: int = NOW) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def _service(tmp_path) -> tuple[BindingService, Clock, BindingStore]:
    clock = Clock()
    store = BindingStore(tmp_path / "bindings.db")
    return BindingService(store, clock=clock), clock, store


def _evm_key() -> tuple[str, str]:
    account = Account.create()
    return account.key.hex(), account.address


def _nostr_key() -> tuple[str, str]:
    private = PrivateKey()
    public = PublicKeyXOnly.from_secret(private.secret).format().hex()
    return private.secret.hex(), public


def _keys() -> tuple[str, str, str, str]:
    evm_key, evm_address = _evm_key()
    nostr_key, nostr_pubkey = _nostr_key()
    return evm_key, evm_address, nostr_key, nostr_pubkey


def _sign_bytes(message: bytes, evm_key: str, nostr_key: str) -> tuple[str, str]:
    evm_sig = (
        Account.from_key(evm_key).sign_message(encode_defunct(primitive=message)).signature.hex()
    )
    if not evm_sig.startswith("0x"):
        evm_sig = "0x" + evm_sig
    nostr_sig = (
        PrivateKey(bytes.fromhex(nostr_key)).sign_schnorr(hashlib.sha256(message).digest()).hex()
    )
    return evm_sig, nostr_sig


def _sign_challenge(challenge, evm_key: str, nostr_key: str) -> tuple[str, str]:
    return _sign_bytes(challenge.message(), evm_key, nostr_key)


def _bind(
    service: BindingService, evm_key: str, evm_address: str, nostr_key: str, nostr_pubkey: str
) -> tuple[Any, dict[str, Any]]:
    challenge = service.issue(evm_address=evm_address, nostr_pubkey=nostr_pubkey)
    evm_sig, nostr_sig = _sign_challenge(challenge, evm_key, nostr_key)
    binding = service.verify(
        nonce=challenge.nonce, evm_signature=evm_sig, nostr_signature=nostr_sig
    )
    return challenge, binding


def _revocation_signature(binding: dict[str, Any], evm_key: str, nostr_key: str, by: str) -> str:
    message = revocation_message(
        evm_address=binding["evm_address"],
        nostr_pubkey=binding["nostr_pubkey"],
        created_at=int(binding["created_at"]),
        by=by,
    )
    evm_sig, nostr_sig = _sign_bytes(message, evm_key, nostr_key)
    return evm_sig if by == "evm" else nostr_sig


def _rotation_signature(binding: dict[str, Any], nostr_key: str, new_pubkey: str) -> str:
    message = rotation_message(
        evm_address=binding["evm_address"],
        old_nostr_pubkey=binding["nostr_pubkey"],
        new_nostr_pubkey=new_pubkey,
    )
    return PrivateKey(bytes.fromhex(nostr_key)).sign_schnorr(hashlib.sha256(message).digest()).hex()


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #


def test_a_valid_binding_is_accepted(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    evm_key, evm_address, nostr_key, nostr_pubkey = _keys()
    challenge, binding = _bind(service, evm_key, evm_address, nostr_key, nostr_pubkey)

    assert binding["evm_address"] == evm_address.lower()
    assert binding["nostr_pubkey"] == nostr_pubkey.lower()
    assert binding["nonce"] == challenge.nonce
    assert binding["revoked_at"] is None
    assert service.lookup(evm_address) is not None


def test_the_signed_message_is_domain_separated() -> None:
    """A signature made for another application must not verify here."""
    challenge = issue_challenge(evm_address="ab" * 20, nostr_pubkey="cd" * 32, now=NOW)
    message = challenge.message()
    assert b"zapproof.binding.v0" in message
    assert challenge.nonce.encode() in message


# --------------------------------------------------------------------------- #
# the attack cases the prompt requires be rejected
# --------------------------------------------------------------------------- #


def test_a_replayed_challenge_is_rejected(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    evm_key, evm_address, nostr_key, nostr_pubkey = _keys()
    challenge, _ = _bind(service, evm_key, evm_address, nostr_key, nostr_pubkey)

    evm_sig, nostr_sig = _sign_challenge(challenge, evm_key, nostr_key)
    with pytest.raises(BindingError, match="challenge_already_used"):
        service.verify(nonce=challenge.nonce, evm_signature=evm_sig, nostr_signature=nostr_sig)


def test_a_challenge_the_service_never_issued_is_rejected(tmp_path) -> None:
    """A caller-constructed challenge has no stored nonce, so it cannot verify."""
    service, _, _ = _service(tmp_path)
    evm_key, evm_address, nostr_key, nostr_pubkey = _keys()

    fabricated = issue_challenge(evm_address=evm_address, nostr_pubkey=nostr_pubkey, now=NOW)
    evm_sig, nostr_sig = _sign_challenge(fabricated, evm_key, nostr_key)

    with pytest.raises(BindingError, match="challenge_not_issued"):
        service.verify(nonce=fabricated.nonce, evm_signature=evm_sig, nostr_signature=nostr_sig)


def test_an_expired_challenge_is_rejected(tmp_path) -> None:
    service, clock, _ = _service(tmp_path)
    evm_key, evm_address, nostr_key, nostr_pubkey = _keys()
    challenge = service.issue(evm_address=evm_address, nostr_pubkey=nostr_pubkey, ttl=60)

    clock.now = challenge.expires_at + 1
    evm_sig, nostr_sig = _sign_challenge(challenge, evm_key, nostr_key)
    with pytest.raises(BindingError, match="challenge_expired"):
        service.verify(nonce=challenge.nonce, evm_signature=evm_sig, nostr_signature=nostr_sig)


def test_a_challenge_from_the_future_is_rejected(tmp_path) -> None:
    service, clock, _ = _service(tmp_path)
    evm_key, evm_address, nostr_key, nostr_pubkey = _keys()
    challenge = service.issue(evm_address=evm_address, nostr_pubkey=nostr_pubkey)

    clock.now = challenge.issued_at - 3600
    evm_sig, nostr_sig = _sign_challenge(challenge, evm_key, nostr_key)
    with pytest.raises(BindingError, match="challenge_not_yet_valid"):
        service.verify(nonce=challenge.nonce, evm_signature=evm_sig, nostr_signature=nostr_sig)


def test_one_key_alone_is_rejected(tmp_path) -> None:
    """The whole point is that one key says nothing about the other."""
    service, _, _ = _service(tmp_path)
    evm_key, evm_address, nostr_key, nostr_pubkey = _keys()
    challenge = service.issue(evm_address=evm_address, nostr_pubkey=nostr_pubkey)
    evm_sig, _ = _sign_challenge(challenge, evm_key, nostr_key)

    with pytest.raises(BindingError, match="both_keys_required"):
        service.verify(nonce=challenge.nonce, evm_signature=evm_sig, nostr_signature="")


def test_a_wrong_domain_cannot_be_signed_into_acceptance(tmp_path) -> None:
    """Signing a different message — a different domain — must not verify."""
    service, _, _ = _service(tmp_path)
    evm_key, evm_address, nostr_key, nostr_pubkey = _keys()
    challenge = service.issue(evm_address=evm_address, nostr_pubkey=nostr_pubkey)

    message = challenge.message()
    wrong = message.replace(b"zapproof.binding.v0", b"zapproof.binding.v9")
    assert wrong != message
    evm_sig, nostr_sig = _sign_bytes(wrong, evm_key, nostr_key)

    with pytest.raises(BindingError, match="evm_signature_does_not_match_address"):
        service.verify(nonce=challenge.nonce, evm_signature=evm_sig, nostr_signature=nostr_sig)


def test_a_signature_from_the_wrong_evm_key_is_rejected(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    _, evm_address, nostr_key, nostr_pubkey = _keys()
    other_evm_key, _ = _evm_key()
    challenge = service.issue(evm_address=evm_address, nostr_pubkey=nostr_pubkey)
    evm_sig, nostr_sig = _sign_challenge(challenge, other_evm_key, nostr_key)

    with pytest.raises(BindingError, match="evm_signature_does_not_match_address"):
        service.verify(nonce=challenge.nonce, evm_signature=evm_sig, nostr_signature=nostr_sig)


def test_a_signature_from_the_wrong_nostr_key_is_rejected(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    evm_key, evm_address, _, nostr_pubkey = _keys()
    _, other_nostr_key = _nostr_key()
    challenge = service.issue(evm_address=evm_address, nostr_pubkey=nostr_pubkey)
    evm_sig, nostr_sig = _sign_challenge(challenge, evm_key, other_nostr_key)

    with pytest.raises(BindingError, match="nostr_signature_does_not_match_pubkey"):
        service.verify(nonce=challenge.nonce, evm_signature=evm_sig, nostr_signature=nostr_sig)


def test_an_address_holds_one_active_binding(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    evm_key, evm_address, nostr_key, nostr_pubkey = _keys()
    _bind(service, evm_key, evm_address, nostr_key, nostr_pubkey)

    other_nostr_key, other_pubkey = _nostr_key()
    challenge = service.issue(evm_address=evm_address, nostr_pubkey=other_pubkey)
    evm_sig, nostr_sig = _sign_challenge(challenge, evm_key, other_nostr_key)

    with pytest.raises(BindingError, match="already_bound"):
        service.verify(nonce=challenge.nonce, evm_signature=evm_sig, nostr_signature=nostr_sig)


# --------------------------------------------------------------------------- #
# revocation: signed, by either side, once
# --------------------------------------------------------------------------- #


def test_revocation_by_either_key_ends_the_binding(tmp_path) -> None:
    for side in ("evm", "nostr"):
        service, clock, _ = _service(tmp_path / side)
        evm_key, evm_address, nostr_key, nostr_pubkey = _keys()
        _, binding = _bind(service, evm_key, evm_address, nostr_key, nostr_pubkey)

        clock.now = NOW + 10
        revoked = service.revoke(
            evm_address=evm_address,
            by=side,
            signature=_revocation_signature(binding, evm_key, nostr_key, side),
        )

        assert revoked["revoked_at"] == NOW + 10
        assert revoked["revoked_by"] == side


def test_revocation_requires_the_matching_key(tmp_path) -> None:
    """A revocation nobody signed — or a bystander signed — is refused."""
    service, _, _ = _service(tmp_path)
    evm_key, evm_address, nostr_key, nostr_pubkey = _keys()
    _, binding = _bind(service, evm_key, evm_address, nostr_key, nostr_pubkey)

    other_evm_key, _ = _evm_key()
    with pytest.raises(BindingError, match="evm_signature_does_not_match_address"):
        service.revoke(
            evm_address=evm_address,
            by="evm",
            signature=_revocation_signature(binding, other_evm_key, nostr_key, "evm"),
        )

    _, other_nostr_key = _nostr_key()
    with pytest.raises(BindingError, match="nostr_signature_does_not_match_pubkey"):
        service.revoke(
            evm_address=evm_address,
            by="nostr",
            signature=_revocation_signature(binding, evm_key, other_nostr_key, "nostr"),
        )


def test_a_challenge_signature_cannot_be_used_as_a_revocation(tmp_path) -> None:
    """Revocation messages carry `kind`, so a signature collected for a challenge
    cannot be replayed as a revocation."""
    service, _, _ = _service(tmp_path)
    evm_key, evm_address, nostr_key, nostr_pubkey = _keys()
    challenge, binding = _bind(service, evm_key, evm_address, nostr_key, nostr_pubkey)
    evm_sig, _ = _sign_challenge(challenge, evm_key, nostr_key)

    with pytest.raises(BindingError, match="evm_signature_does_not_match_address"):
        service.revoke(evm_address=evm_address, by="evm", signature=evm_sig)


def test_a_binding_cannot_be_revoked_twice_or_by_a_third_party(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    evm_key, evm_address, nostr_key, nostr_pubkey = _keys()
    _, binding = _bind(service, evm_key, evm_address, nostr_key, nostr_pubkey)
    service.revoke(
        evm_address=evm_address,
        by="evm",
        signature=_revocation_signature(binding, evm_key, nostr_key, "evm"),
    )

    with pytest.raises(BindingError, match="already_revoked"):
        service.revoke(
            evm_address=evm_address,
            by="nostr",
            signature=_revocation_signature(binding, evm_key, nostr_key, "nostr"),
        )

    another_key, another_address, another_nostr, another_pubkey = _keys()
    _, other = _bind(service, another_key, another_address, another_nostr, another_pubkey)
    with pytest.raises(BindingError, match="revoked_by_must_be_evm_or_nostr"):
        service.revoke(
            evm_address=another_address,
            by="admin",
            signature=_revocation_signature(other, another_key, another_nostr, "evm"),
        )


# --------------------------------------------------------------------------- #
# rotation
# --------------------------------------------------------------------------- #


def test_rotation_hands_the_binding_to_a_new_nostr_key(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    evm_key, evm_address, nostr_key, nostr_pubkey = _keys()
    _, binding = _bind(service, evm_key, evm_address, nostr_key, nostr_pubkey)

    new_nostr_key, new_pubkey = _nostr_key()
    rotated = service.rotate(
        evm_address=evm_address,
        new_nostr_pubkey=new_pubkey,
        nostr_signature=_rotation_signature(binding, nostr_key, new_pubkey),
    )

    assert rotated["nostr_pubkey"] == new_pubkey.lower()
    assert rotated["revoked_at"] is None

    # The new key can revoke; the old one can no longer rotate.
    revoked = service.revoke(
        evm_address=evm_address,
        by="nostr",
        signature=_revocation_signature(rotated, evm_key, new_nostr_key, "nostr"),
    )
    assert revoked["revoked_by"] == "nostr"


def test_rotation_requires_the_current_nostr_key(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    evm_key, evm_address, nostr_key, nostr_pubkey = _keys()
    _, binding = _bind(service, evm_key, evm_address, nostr_key, nostr_pubkey)

    new_nostr_key, new_pubkey = _nostr_key()
    _, stranger_key = _nostr_key()
    with pytest.raises(BindingError, match="nostr_signature_does_not_match_pubkey"):
        service.rotate(
            evm_address=evm_address,
            new_nostr_pubkey=new_pubkey,
            nostr_signature=_rotation_signature(binding, stranger_key, new_pubkey),
        )


def test_a_revoked_binding_cannot_be_rotated(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    evm_key, evm_address, nostr_key, nostr_pubkey = _keys()
    _, binding = _bind(service, evm_key, evm_address, nostr_key, nostr_pubkey)
    service.revoke(
        evm_address=evm_address,
        by="evm",
        signature=_revocation_signature(binding, evm_key, nostr_key, "evm"),
    )

    _, new_pubkey = _nostr_key()
    with pytest.raises(BindingError, match="already_revoked"):
        service.rotate(
            evm_address=evm_address,
            new_nostr_pubkey=new_pubkey,
            nostr_signature=_rotation_signature(binding, nostr_key, new_pubkey),
        )


def test_a_revoked_address_can_bind_again_with_a_fresh_challenge(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    evm_key, evm_address, nostr_key, nostr_pubkey = _keys()
    _, binding = _bind(service, evm_key, evm_address, nostr_key, nostr_pubkey)
    service.revoke(
        evm_address=evm_address,
        by="evm",
        signature=_revocation_signature(binding, evm_key, nostr_key, "evm"),
    )

    _, again = _bind(service, evm_key, evm_address, nostr_key, nostr_pubkey)
    assert again["revoked_at"] is None


def test_the_audit_trail_records_the_lifecycle(tmp_path) -> None:
    service, _, store = _service(tmp_path)
    evm_key, evm_address, nostr_key, nostr_pubkey = _keys()
    _, binding = _bind(service, evm_key, evm_address, nostr_key, nostr_pubkey)

    new_nostr_key, new_pubkey = _nostr_key()
    rotated = service.rotate(
        evm_address=evm_address,
        new_nostr_pubkey=new_pubkey,
        nostr_signature=_rotation_signature(binding, nostr_key, new_pubkey),
    )
    service.revoke(
        evm_address=evm_address,
        by="nostr",
        signature=_revocation_signature(rotated, evm_key, new_nostr_key, "nostr"),
    )

    kinds = [event["kind"] for event in store.events(evm_address.lower())]
    assert kinds == ["created", "rotated", "revoked"]


# --------------------------------------------------------------------------- #
# the same rules through the HTTP app
# --------------------------------------------------------------------------- #


def test_the_api_exposes_the_same_rules(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    app.dependency_overrides[get_service] = lambda: service
    client = TestClient(app)
    try:
        evm_key, evm_address, nostr_key, nostr_pubkey = _keys()

        issued = client.post(
            "/v1/bindings/challenge",
            json={"evm_address": evm_address, "nostr_pubkey": nostr_pubkey},
        )
        assert issued.status_code == 200
        assert issued.json()["nonce"].startswith("0x")

        # The API hands back the exact bytes to sign, so the client does not
        # re-implement canonicalisation.
        message = bytes.fromhex(issued.json()["message"])
        evm_sig, nostr_sig = _sign_bytes(message, evm_key, nostr_key)
        body = {
            "nonce": issued.json()["nonce"],
            "evm_signature": evm_sig,
            "nostr_signature": nostr_sig,
        }

        created = client.post("/v1/bindings", json=body)
        assert created.status_code == 201
        assert created.json()["active"] is True

        replayed = client.post("/v1/bindings", json=body)
        assert replayed.status_code == 400
        assert replayed.json()["error"] == "challenge_already_used"

        read = client.get(f"/v1/bindings/{evm_address}")
        assert read.status_code == 200
        assert [e["kind"] for e in read.json()["events"]] == ["created"]
    finally:
        app.dependency_overrides.clear()


def test_the_api_refuses_a_revocation_without_a_signature(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    app.dependency_overrides[get_service] = lambda: service
    client = TestClient(app)
    try:
        evm_key, evm_address, nostr_key, nostr_pubkey = _keys()
        _bind(service, evm_key, evm_address, nostr_key, nostr_pubkey)

        response = client.post(
            "/v1/bindings/revoke",
            json={"evm_address": evm_address, "by": "evm", "signature": "0xdeadbeef"},
        )
        assert response.status_code == 400
        assert response.json()["error"] == "evm_signature_malformed"
    finally:
        app.dependency_overrides.clear()


def test_an_unknown_binding_is_404_on_the_app(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    app.dependency_overrides[get_service] = lambda: service
    client = TestClient(app)
    try:
        assert client.get(f"/v1/bindings/0x{'ab' * 20}").status_code == 404
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# the primitives against the specification
# --------------------------------------------------------------------------- #


def test_bip340_official_test_vector_0() -> None:
    """BIP-340 vector 0, so the Schnorr check is verified against the spec.

    seckey  3, message all zeros, aux all zeros. If this fails, the library is
    wrong and no binding result above means anything.
    """
    public = PublicKeyXOnly.from_secret(bytes.fromhex(SECKEY))
    assert public.format().hex().upper() == (
        "F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9"
    )
    digest = bytes(32)
    signature = bytes.fromhex(
        "E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA8215"
        "25F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0"
    )
    assert public.verify(signature, digest)


def test_eip191_round_trip_recovers_the_signer() -> None:
    account = Account.create()
    message = encode_defunct(primitive=b"zapproof binding")
    signature = account.sign_message(message).signature
    assert Account.recover_message(message, signature=signature) == account.address


# --------------------------------------------------------------------------- #
# input validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("evm", "nostr", "reason"),
    [
        ("0x1234", "ab" * 32, "evm_address_must_be_20_bytes"),
        ("zz" * 20, "ab" * 32, "evm_address_is_not_hex"),
        ("ab" * 20, "0x1234", "nostr_pubkey_must_be_32_bytes_xonly"),
        ("ab" * 20, "zz" * 32, "nostr_pubkey_is_not_hex"),
    ],
)
def test_malformed_keys_are_refused_at_issue_time(evm: str, nostr: str, reason: str) -> None:
    with pytest.raises(BindingError, match=reason):
        issue_challenge(evm_address=evm, nostr_pubkey=nostr, now=NOW)


def test_a_malformed_signature_is_refused_not_crashed(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    _, evm_address, _, nostr_pubkey = _keys()
    challenge = service.issue(evm_address=evm_address, nostr_pubkey=nostr_pubkey)

    with pytest.raises(BindingError, match="evm_signature_malformed"):
        service.verify(
            nonce=challenge.nonce,
            evm_signature="0xdeadbeef",
            nostr_signature="ab" * 64,
        )
