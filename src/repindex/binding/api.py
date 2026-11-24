"""A separate application for identity binding.

**Why separate.** The score API is read-only: it opens SQLite in `mode=ro`, GB.4
proves it refuses every write verb, and the deployment mounts `data/` read-only. A
binding endpoint that issues challenges and records bindings is a writer, so it lives
in its own app with its own database (`data/bindings.db`). Importing this module from
`repindex.api.app` would couple the two; nothing does.

Run it with::

    uvicorn repindex.binding.api:app --port 8001

It is not part of the score API's deployment; see docs/binding.md for what was and was
not tested.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from repindex.binding.service import BindingService
from repindex.binding.store import BindingStore
from repindex.binding.verify import BindingError

DEFAULT_DB_PATH = Path("data/bindings.db")

app = FastAPI(
    title="Zapproof binding",
    version="0.1.0",
    description=(
        "Binds an EVM address to a Nostr public key with two signatures over a "
        "server-issued, single-use challenge. Separate from the read-only score API."
    ),
)

_service: BindingService | None = None


def get_service() -> BindingService:
    """One service per process, backed by `BINDING_DB` (default `data/bindings.db`)."""
    global _service
    if _service is None:
        path = Path(os.environ.get("BINDING_DB", str(DEFAULT_DB_PATH)))
        _service = BindingService(BindingStore(path))
    return _service


@app.exception_handler(BindingError)
async def binding_error(_: Request, exc: BindingError) -> JSONResponse:
    """Every refusal is a 400 with the reason code; none is a 500."""
    return JSONResponse(status_code=400, content={"error": str(exc)})


class ChallengeRequest(BaseModel):
    evm_address: str = Field(min_length=1, description="20-byte hex EVM address")
    nostr_pubkey: str = Field(min_length=1, description="32-byte x-only hex pubkey")


class ChallengeResponse(BaseModel):
    nonce: str
    evm_address: str
    nostr_pubkey: str
    issued_at: int
    expires_at: int
    message: str = Field(description="hex of the exact bytes both keys must sign")


class VerifyRequest(BaseModel):
    nonce: str = Field(min_length=1)
    evm_signature: str = Field(min_length=1)
    nostr_signature: str = Field(min_length=1)


class RevokeRequest(BaseModel):
    evm_address: str = Field(min_length=1)
    by: Literal["evm", "nostr"]
    signature: str = Field(min_length=1)


class RotateRequest(BaseModel):
    evm_address: str = Field(min_length=1)
    new_nostr_pubkey: str = Field(min_length=1)
    nostr_signature: str = Field(min_length=1)


def _public(binding: dict[str, Any]) -> dict[str, Any]:
    """What a caller may see. Signatures are proofs, not secrets, but they are omitted."""
    return {
        "evm_address": binding["evm_address"],
        "nostr_pubkey": binding["nostr_pubkey"],
        "created_at": binding["created_at"],
        "updated_at": binding["updated_at"],
        "revoked_at": binding["revoked_at"],
        "revoked_by": binding["revoked_by"],
        "active": binding["revoked_at"] is None,
    }


@app.post("/v1/bindings/challenge", response_model=ChallengeResponse)
def create_challenge(
    body: ChallengeRequest, service: Annotated[BindingService, Depends(get_service)]
) -> ChallengeResponse:
    challenge = service.issue(evm_address=body.evm_address, nostr_pubkey=body.nostr_pubkey)
    return ChallengeResponse(
        nonce=challenge.nonce,
        evm_address=challenge.evm_address,
        nostr_pubkey=challenge.nostr_pubkey,
        issued_at=challenge.issued_at,
        expires_at=challenge.expires_at,
        message=challenge.message().hex(),
    )


@app.post("/v1/bindings", status_code=201)
def create_binding(
    body: VerifyRequest, service: Annotated[BindingService, Depends(get_service)]
) -> dict[str, Any]:
    binding = service.verify(
        nonce=body.nonce,
        evm_signature=body.evm_signature,
        nostr_signature=body.nostr_signature,
    )
    return _public(binding)


@app.post("/v1/bindings/revoke")
def revoke_binding(
    body: RevokeRequest, service: Annotated[BindingService, Depends(get_service)]
) -> dict[str, Any]:
    binding = service.revoke(evm_address=body.evm_address, by=body.by, signature=body.signature)
    return _public(binding)


@app.post("/v1/bindings/rotate")
def rotate_binding(
    body: RotateRequest, service: Annotated[BindingService, Depends(get_service)]
) -> dict[str, Any]:
    binding = service.rotate(
        evm_address=body.evm_address,
        new_nostr_pubkey=body.new_nostr_pubkey,
        nostr_signature=body.nostr_signature,
    )
    return _public(binding)


@app.get("/v1/bindings/{evm_address}")
def read_binding(
    evm_address: str, service: Annotated[BindingService, Depends(get_service)]
) -> dict[str, Any]:
    binding = service.lookup(evm_address)
    if binding is None:
        raise HTTPException(status_code=404, detail="no binding for this address")
    result = _public(binding)
    result["events"] = service.store.events(binding["evm_address"])
    return result
