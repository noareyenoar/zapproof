"""End-to-end identity binding (review F5, gate check GB.13).

The review's finding was that the old tests proved four signature primitives and
nothing else: no service issued a challenge, nothing stored one, single use was the
caller's job. This script drives the real HTTP app over a loopback socket — challenge,
binding, replay, wrong domain, one key, unsigned revocation, rotation, signed
revocation — and writes `reports/binding_e2e.json` for the gate.

Expiry is exercised at the service layer because the HTTP app does not expose a way
to move its clock, and a test-only clock route would be a way to forge challenges in
production. The layer is recorded per case.

Run:  uv run python scripts/binding_e2e.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from coincurve import PrivateKey, PublicKeyXOnly
from eth_account import Account
from eth_account.messages import encode_defunct

from repindex.binding import api as binding_api
from repindex.binding.service import BindingService
from repindex.binding.store import BindingStore
from repindex.binding.verify import (
    BindingError,
    issue_challenge,
    revocation_message,
    rotation_message,
)
from repindex.common.provenance import (
    DATA_DB,
    CommitObserver,
    DatabaseChangedError,
    PendingWALError,
    attest_identity,
    provenance,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = REPO_ROOT / "reports" / "binding_e2e.json"
READY_TIMEOUT = 20.0


# --------------------------------------------------------------------------- #
# keys and signatures
# --------------------------------------------------------------------------- #


def _evm_key() -> tuple[str, str]:
    account = Account.create()
    return account.key.hex(), account.address


def _nostr_key() -> tuple[str, str]:
    private = PrivateKey()
    return private.secret.hex(), PublicKeyXOnly.from_secret(private.secret).format().hex()


def _sign(message: bytes, evm_key: str, nostr_key: str) -> tuple[str, str]:
    evm_sig = (
        Account.from_key(evm_key).sign_message(encode_defunct(primitive=message)).signature.hex()
    )
    if not evm_sig.startswith("0x"):
        evm_sig = "0x" + evm_sig
    nostr_sig = (
        PrivateKey(bytes.fromhex(nostr_key)).sign_schnorr(hashlib.sha256(message).digest()).hex()
    )
    return evm_sig, nostr_sig


def _nostr_sign(message: bytes, nostr_key: str) -> str:
    return PrivateKey(bytes.fromhex(nostr_key)).sign_schnorr(
        hashlib.sha256(message).digest()
    ).hex()


# --------------------------------------------------------------------------- #
# a real server on a loopback port
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Server:
    def __init__(self, db_path: Path) -> None:
        os.environ["BINDING_DB"] = str(db_path)
        self.port = _free_port()
        config = uvicorn.Config(
            binding_api.app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> Server:
        self.thread.start()
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            try:
                # Any answer means the app is routing; 404 is the expected one.
                httpx.get(f"http://127.0.0.1:{self.port}/v1/bindings/0x{'ab' * 20}", timeout=1)
                return self
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("binding app did not start")

    def __exit__(self, *_: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _challenge(client: httpx.Client, evm_address: str, nostr_pubkey: str) -> dict[str, Any]:
    response = client.post(
        "/v1/bindings/challenge",
        json={"evm_address": evm_address, "nostr_pubkey": nostr_pubkey},
    )
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    return data


# --------------------------------------------------------------------------- #
# the cases
# --------------------------------------------------------------------------- #


def run() -> dict[str, Any]:
    # O2/P2: record the identity the run started under and confirm it when the
    # evidence is assembled. The observer opens before the hash and is checked after
    # the run, so a commit that is restored leaves a visible change counter.
    observer = CommitObserver(DATA_DB)
    try:
        attested = attest_identity(DATA_DB)
    except PendingWALError:
        observer.close()
        raise
    workdir = Path(tempfile.mkdtemp(prefix="zapproof-binding-"))
    cases: dict[str, bool] = {}
    details: dict[str, Any] = {}
    try:
        with Server(workdir / "bindings.db") as server:
            base = f"http://127.0.0.1:{server.port}"
            with httpx.Client(base_url=base, timeout=10) as client:
                evm_key, evm_address = _evm_key()
                nostr_key, nostr_pubkey = _nostr_key()

                # 1. A valid two-key binding is accepted.
                issued = _challenge(client, evm_address, nostr_pubkey)
                evm_sig, nostr_sig = _sign(bytes.fromhex(issued["message"]), evm_key, nostr_key)
                body = {
                    "nonce": issued["nonce"],
                    "evm_signature": evm_sig,
                    "nostr_signature": nostr_sig,
                }
                created = client.post("/v1/bindings", json=body)
                cases["valid_binding_accepted"] = (
                    created.status_code == 201 and created.json().get("active") is True
                )
                details["valid_binding_accepted"] = {
                    "layer": "http",
                    "status": created.status_code,
                }

                # 2. The same challenge cannot be used twice.
                replay = client.post("/v1/bindings", json=body)
                cases["replay_rejected"] = (
                    replay.status_code == 400
                    and replay.json().get("error") == "challenge_already_used"
                )
                details["replay_rejected"] = {
                    "layer": "http",
                    "status": replay.status_code,
                    "error": replay.json().get("error"),
                }

                # 3. A challenge the server never issued cannot be verified.
                other_evm_key, other_address = _evm_key()
                other_nostr_key, other_pubkey = _nostr_key()
                fabricated = issue_challenge(
                    evm_address=other_address, nostr_pubkey=other_pubkey, now=int(time.time())
                )
                fabricated_evm, fabricated_nostr = _sign(
                    fabricated.message(), other_evm_key, other_nostr_key
                )
                unissued = client.post(
                    "/v1/bindings",
                    json={
                        "nonce": fabricated.nonce,
                        "evm_signature": fabricated_evm,
                        "nostr_signature": fabricated_nostr,
                    },
                )
                cases["caller_constructed_challenge_rejected"] = (
                    unissued.status_code == 400
                    and unissued.json().get("error") == "challenge_not_issued"
                )
                details["caller_constructed_challenge_rejected"] = {
                    "layer": "http",
                    "status": unissued.status_code,
                    "error": unissued.json().get("error"),
                }

                # 4. Signing a different domain does not help.
                wrong_domain = _challenge(client, other_address, other_pubkey)
                wrong_bytes = bytes.fromhex(wrong_domain["message"]).replace(
                    b"zapproof.binding.v0", b"zapproof.binding.v9"
                )
                wrong_evm, wrong_nostr = _sign(wrong_bytes, other_evm_key, other_nostr_key)
                wrong = client.post(
                    "/v1/bindings",
                    json={
                        "nonce": wrong_domain["nonce"],
                        "evm_signature": wrong_evm,
                        "nostr_signature": wrong_nostr,
                    },
                )
                cases["wrong_domain_rejected"] = (
                    wrong.status_code == 400
                    and wrong.json().get("error") == "evm_signature_does_not_match_address"
                )
                details["wrong_domain_rejected"] = {
                    "layer": "http",
                    "status": wrong.status_code,
                    "error": wrong.json().get("error"),
                }

                # 5. One key alone is refused by the request schema.
                one_key = client.post(
                    "/v1/bindings",
                    json={
                        "nonce": wrong_domain["nonce"],
                        "evm_signature": wrong_evm,
                        "nostr_signature": "",
                    },
                )
                cases["one_key_rejected"] = 400 <= one_key.status_code < 500
                details["one_key_rejected"] = {
                    "layer": "http",
                    "status": one_key.status_code,
                    "note": "empty Nostr signature is refused before any state is touched",
                }

                # 6. Revocation without a valid signature is refused.
                unsigned = client.post(
                    "/v1/bindings/revoke",
                    json={
                        "evm_address": evm_address,
                        "by": "evm",
                        "signature": "0xdeadbeef",
                    },
                )
                cases["unsigned_revocation_rejected"] = (
                    unsigned.status_code == 400
                    and unsigned.json().get("error") == "evm_signature_malformed"
                )
                details["unsigned_revocation_rejected"] = {
                    "layer": "http",
                    "status": unsigned.status_code,
                    "error": unsigned.json().get("error"),
                }

                # 7. Rotation needs the current Nostr key and hands the binding over.
                new_nostr_key, new_pubkey = _nostr_key()
                rotation_body = {
                    "evm_address": evm_address,
                    "new_nostr_pubkey": new_pubkey,
                    "nostr_signature": _nostr_sign(
                        rotation_message(
                            evm_address=evm_address,
                            old_nostr_pubkey=nostr_pubkey,
                            new_nostr_pubkey=new_pubkey,
                        ),
                        nostr_key,
                    ),
                }
                rotated = client.post("/v1/bindings/rotate", json=rotation_body)
                cases["rotation_accepted"] = (
                    rotated.status_code == 200
                    and rotated.json().get("nostr_pubkey") == new_pubkey.lower()
                )
                details["rotation_accepted"] = {
                    "layer": "http",
                    "status": rotated.status_code,
                }

                # 8. Revocation signed by the current (rotated) key ends the binding.
                revoked = client.post(
                    "/v1/bindings/revoke",
                    json={
                        "evm_address": evm_address,
                        "by": "nostr",
                        "signature": _nostr_sign(
                            revocation_message(
                                evm_address=evm_address,
                                nostr_pubkey=new_pubkey,
                                created_at=int(created.json()["created_at"]),
                                by="nostr",
                            ),
                            new_nostr_key,
                        ),
                    },
                )
                cases["signed_revocation_accepted"] = (
                    revoked.status_code == 200 and revoked.json().get("active") is False
                )
                details["signed_revocation_accepted"] = {
                    "layer": "http",
                    "status": revoked.status_code,
                    "revoked_by": revoked.json().get("revoked_by"),
                }

                # 9. The audit trail is readable through the same app.
                trail = client.get(f"/v1/bindings/{evm_address}")
                kinds = [event["kind"] for event in trail.json().get("events", [])]
                cases["lifecycle_recorded"] = kinds == ["created", "rotated", "revoked"]
                details["lifecycle_recorded"] = {"layer": "http", "events": kinds}

        # 10. Expiry, at the service layer with a clock the test owns.
        clock = {"now": 1_800_000_000}
        store = BindingStore(workdir / "expiry.db")
        service = BindingService(store, clock=lambda: clock["now"])
        try:
            exp_evm_key, exp_address = _evm_key()
            exp_nostr_key, exp_pubkey = _nostr_key()
            exp_challenge = service.issue(
                evm_address=exp_address, nostr_pubkey=exp_pubkey, ttl=60
            )
            exp_evm, exp_nostr = _sign(exp_challenge.message(), exp_evm_key, exp_nostr_key)
            clock["now"] = exp_challenge.expires_at + 1
            try:
                service.verify(
                    nonce=exp_challenge.nonce,
                    evm_signature=exp_evm,
                    nostr_signature=exp_nostr,
                )
                expired_error = None
            except BindingError as exc:
                expired_error = str(exc)
            cases["expired_rejected"] = expired_error == "challenge_expired"
            details["expired_rejected"] = {
                "layer": "service (injected clock)",
                "error": expired_error,
            }
        finally:
            store.close()
    finally:
        binding_api.get_service().store.close()
        shutil.rmtree(workdir, ignore_errors=True)

    try:
        observer.require_quiet()
        prov = provenance(attested=attested)
    finally:
        observer.close()
    return {
        **prov,
        "transport": "uvicorn on 127.0.0.1, httpx client",
        "cases": cases,
        "details": details,
        "all_passed": all(cases.values()) and len(cases) == 10,
    }


def main() -> int:
    try:
        result = run()
    except (PendingWALError, DatabaseChangedError) as exc:
        print(f"{exc} The binding evidence is not written from a state it cannot name.")
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["cases"], indent=2))
    failed = [name for name, passed in result["cases"].items() if not passed]
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print(f"all {len(result['cases'])} binding cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
