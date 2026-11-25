"""Decode x402 settlements out of Base mainnet logs.

Everything asserted here was verified on-chain from this machine on 2026-10-16
(Phase 0 task 0.4). See `docs/protocol_notes.md` for the transaction hashes.

The three facts that shape this module:

1. USDC on Base is `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`, `symbol()` returns
   "USDC", `decimals()` returns 6.

2. `AuthorizationUsed(address authorizer, bytes32 nonce)` is emitted with **three
   topics and empty data**. The amount is *not* in the event. It has to be read
   from the paired `Transfer` in the same transaction. This is a hard constraint
   of the token contract, not a decoding choice we get to make.

3. `transaction.from` is the facilitator's relayer, not the payer. The payer is
   `AuthorizationUsed.authorizer`, which equals the paired `Transfer.from`.

Amounts are handled as integers throughout. Nothing here converts to float, and
nothing formats a human-readable amount for storage — prompt task A2 requires raw
integers so raw-to-normalized round-trips exactly.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

# keccak256("AuthorizationUsed(address,bytes32)")
AUTHORIZATION_USED_TOPIC = "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5"

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DECIMALS = 6

# EIP-3009. Verified against a real Base transaction on 2026-10-16: selector
# 0xe3ee160e, and the decoded (from, to, value, nonce) matched the
# AuthorizationUsed + Transfer events for the same transaction exactly.
TRANSFER_WITH_AUTHORIZATION_SELECTOR = "0xe3ee160e"
_REQUIRED_WORDS = 9


class DecodeError(ValueError):
    """A log could not be decoded. Raised rather than skipping the record."""


@dataclass(frozen=True, slots=True)
class Settlement:
    """One decoded EIP-3009 settlement. Raw amount is an integer in token base units."""

    tx_hash: str
    log_index: int
    block_number: int
    token: str
    payer: str
    seller: str
    # None when the transaction envelope was not fetched. The relayer lives only in
    # `tx.from`, not in any log, and resolving it costs one RPC call per transaction.
    # Measured 2026-10-28: settlement volume grew to about 50 per block, making a
    # seven-day relayer resolution cost roughly 125 hours. The field is therefore
    # optional and the `single_relayer` rule abstains when it is absent, rather than
    # the whole backfill being impossible.
    facilitator_relayer: str | None
    raw_amount: int
    nonce: str
    decimals: int

    @property
    def mechanism(self) -> str:
        return "eip3009_authorization_used"

    @property
    def amount_usdc(self) -> str:
        """Exact decimal string, derived from the integer. Not a float."""
        whole, frac = divmod(self.raw_amount, 10**self.decimals)
        return f"{whole}.{frac:0{self.decimals}d}"


def _topic_to_address(topic: str) -> str:
    """A 32-byte topic holding a left-padded address -> lowercase 0x address."""
    if len(topic) != 66:
        raise DecodeError(f"topic is {len(topic)} chars, expected 66: {topic!r}")
    return "0x" + topic[-40:].lower()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DecodeError(message)


def decode_settlements(
    logs: list[dict[str, Any]],
    tx_senders: dict[str, str] | None,
    *,
    token: str = USDC_BASE,
    decimals: int = USDC_DECIMALS,
) -> list[Settlement]:
    """Pair each `AuthorizationUsed` with its settlement `Transfer` in the same tx.

    `tx_senders` maps transaction hash -> `transaction.from`, because a log does not
    carry the sender. Callers get it from `eth_getBlockByNumber(..., True)`; fetching
    it per transaction instead costs roughly 1,300x more round trips (measured: 2,517
    `eth_getTransactionByHash` calls took 21 minutes).

    Raises DecodeError rather than dropping an undecodable record. A silently dropped
    settlement is a coverage hole that resurfaces later as an unexplained count
    mismatch, which is exactly what the cross-check exists to catch.
    """
    by_tx: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for log in logs:
        tx_hash = log.get("transactionHash")
        if not isinstance(tx_hash, str):
            raise DecodeError(f"log without transactionHash: {log!r}")
        by_tx[tx_hash].append(log)

    settlements: list[Settlement] = []

    for tx_hash, tx_logs in by_tx.items():
        ordered = sorted(tx_logs, key=lambda entry: int(entry["logIndex"], 16))

        authorizations = [
            (position, log)
            for position, log in enumerate(ordered)
            if (log.get("topics") or [None])[0] == AUTHORIZATION_USED_TOPIC
        ]
        # A transaction with no AuthorizationUsed is simply not a settlement. It is
        # skipped, not errored — the caller may hand us every USDC log in a block.
        if not authorizations:
            continue

        sender = (tx_senders or {}).get(tx_hash)
        sender = sender.lower() if isinstance(sender, str) else None

        for position, log in authorizations:
            topics = log["topics"]
            _require(
                len(topics) == 3,
                f"{tx_hash}: AuthorizationUsed has {len(topics)} topics, expected 3",
            )
            authorizer = _topic_to_address(topics[1])
            nonce = topics[2]

            # The amount lives in the Transfer that follows in the same transaction.
            transfer = _next_transfer_from(ordered, position, authorizer, tx_hash)

            settlements.append(
                Settlement(
                    tx_hash=tx_hash,
                    log_index=int(log["logIndex"], 16),
                    block_number=int(log["blockNumber"], 16),
                    token=token.lower(),
                    payer=authorizer,
                    seller=_topic_to_address(transfer["topics"][2]),
                    facilitator_relayer=sender,
                    raw_amount=int(transfer["data"], 16),
                    nonce=nonce,
                    decimals=decimals,
                )
            )

    settlements.sort(key=lambda settlement: (settlement.block_number, settlement.log_index))
    return settlements


def decode_from_calldata(
    tx: dict[str, Any],
    *,
    token: str = USDC_BASE,
    decimals: int = USDC_DECIMALS,
) -> Settlement | None:
    """Decode a settlement straight out of the `transferWithAuthorization` calldata.

    Returns None when the transaction is not an EIP-3009 transfer, so callers can
    fall back to event pairing for other mechanisms rather than mis-decoding them.

    Why this exists: the amount is NOT in the `AuthorizationUsed` event, and pulling
    the paired `Transfer` log means asking for every USDC transfer in the range —
    roughly 59 per block, which blew past the endpoint's 20,000-result cap on a
    400-block window (measured 2026-10-16). The calldata carries from, to, value and
    nonce in one place, and a block fetched with full transactions answers for every
    settlement in it.

    Field order is the EIP-3009 ABI:
        transferWithAuthorization(address from, address to, uint256 value,
            uint256 validAfter, uint256 validBefore, bytes32 nonce,
            uint8 v, bytes32 r, bytes32 s)
    """
    data = tx.get("input")
    if not isinstance(data, str) or not data.startswith(TRANSFER_WITH_AUTHORIZATION_SELECTOR):
        return None

    raw = data[2:]
    body = raw[8:]
    word_count = len(body) // 64
    if word_count < _REQUIRED_WORDS:
        raise DecodeError(
            f"{tx.get('hash')}: transferWithAuthorization calldata has {word_count} words, "
            f"expected {_REQUIRED_WORDS}"
        )

    words = [body[i * 64 : (i + 1) * 64] for i in range(_REQUIRED_WORDS)]
    payer = "0x" + words[0][-40:].lower()
    seller = "0x" + words[1][-40:].lower()
    value = int(words[2], 16)
    nonce = "0x" + words[5]

    sender = tx.get("from")
    if not isinstance(sender, str):
        raise DecodeError(f"{tx.get('hash')}: transaction has no sender")

    tx_hash = tx.get("hash")
    if not isinstance(tx_hash, str):
        raise DecodeError("transaction has no hash")

    receipt_block = tx.get("blockNumber")
    block_number = int(receipt_block, 16) if isinstance(receipt_block, str) else 0

    return Settlement(
        tx_hash=tx_hash,
        # A calldata-decoded settlement has no single originating log, so -1 marks
        # "not derived from a log". The event-paired decoder fills the real index.
        log_index=-1,
        block_number=block_number,
        token=token.lower(),
        payer=payer,
        seller=seller,
        facilitator_relayer=sender.lower(),
        raw_amount=value,
        nonce=nonce,
        decimals=decimals,
    )


def _next_transfer_from(
    ordered: list[dict[str, Any]],
    authorization_position: int,
    authorizer: str,
    tx_hash: str,
) -> dict[str, Any]:
    """Find the settlement Transfer that follows an AuthorizationUsed in the same tx."""
    for candidate in ordered[authorization_position + 1 :]:
        topics = candidate.get("topics") or []
        if len(topics) != 3 or topics[0] != TRANSFER_TOPIC:
            continue
        if _topic_to_address(topics[1]) != authorizer:
            continue
        data = candidate.get("data")
        _require(
            isinstance(data, str) and data.startswith("0x") and len(data) == 66,
            f"{tx_hash}: Transfer has unexpected data {data!r}",
        )
        return candidate

    raise DecodeError(
        f"{tx_hash}: no Transfer from authorizer {authorizer} after its AuthorizationUsed. "
        "The amount cannot be recovered from the event alone, so this record is undecodable."
    )
