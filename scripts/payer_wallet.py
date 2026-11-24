"""Create and inspect the dedicated hot wallet the preflight agent pays from.

Safety rules baked in:

- The private key is written to a file outside the repository, with owner-only
  permissions where the OS supports it, and is never printed.
- The wallet is an ordinary EOA. EIP-3009 authorizations are signed by the token
  holder, so a smart-contract wallet is not a drop-in replacement here.
- The agent needs USDC on Base, not ETH: the x402 facilitator submits the transfer.
  Keep a tiny ETH balance only if you also make direct transfers.

Run:
    uv run python scripts/payer_wallet.py create
    uv run python scripts/payer_wallet.py status
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from eth_account import Account

DEFAULT_KEY_FILE = Path.home() / ".zapproof" / "preflight-wallet.key"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
RPCS = ("https://base-rpc.publicnode.com", "https://mainnet.base.org")
USDC_DECIMALS = 6


def _rpc(method: str, params: list[object]) -> object:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    last: Exception | None = None
    for url in RPCS:
        try:
            request = urllib.request.Request(
                url,
                data=payload.encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                body = json.load(response)
            if "result" in body:
                return body["result"]
            last = RuntimeError(str(body.get("error", "rpc error")))
        except Exception as exc:  # noqa: BLE001 - try the next provider
            last = exc
    raise RuntimeError(f"all RPC providers failed: {last}")


def _restrict(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    if sys.platform == "win32":
        user = getpass.getuser()
        with contextlib.suppress(OSError):
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                check=False,
                capture_output=True,
            )


def create(path: Path) -> int:
    if path.exists():
        account = Account.from_key(path.read_text(encoding="utf-8").strip())
        print(json.dumps({"address": account.address, "created": False, "key_file": str(path)}))
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    account = Account.create()
    path.write_text(account.key.hex(), encoding="utf-8")
    _restrict(path)
    print(
        json.dumps(
            {
                "address": account.address,
                "created": True,
                "key_file": str(path),
                "network": "Base mainnet (eip155:8453)",
                "asset": f"USDC {BASE_USDC}",
                "next": "Send USDC on Base to this address, then run: status",
            },
            indent=2,
        )
    )
    return 0


def _balance(address: str, token: str | None) -> int:
    if token is None:
        result = _rpc("eth_getBalance", [address, "latest"])
    else:
        data = "0x70a08231" + address.lower().removeprefix("0x").rjust(64, "0")
        result = _rpc("eth_call", [{"to": token, "data": data}, "latest"])
    return int(str(result), 16)


def status(path: Path) -> int:
    if not path.exists():
        print(json.dumps({"error": "wallet_not_created", "key_file": str(path)}))
        return 1
    account = Account.from_key(path.read_text(encoding="utf-8").strip())
    usdc_raw = _balance(account.address, BASE_USDC)
    eth_raw = _balance(account.address, None)
    print(
        json.dumps(
            {
                "address": account.address,
                "usdc": usdc_raw / 10**USDC_DECIMALS,
                "eth": eth_raw / 10**18,
                "network": "Base mainnet (eip155:8453)",
                "funded": usdc_raw > 0,
            },
            indent=2,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("create", "status", "address"))
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    args = parser.parse_args()
    if args.command == "create":
        return create(args.key_file)
    if args.command == "address":
        if not args.key_file.exists():
            print(json.dumps({"error": "wallet_not_created"}))
            return 1
        account = Account.from_key(args.key_file.read_text(encoding="utf-8").strip())
        print(json.dumps({"address": account.address}))
        return 0
    return status(args.key_file)


if __name__ == "__main__":
    raise SystemExit(main())
