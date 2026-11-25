"""The payer wallet helper prints addresses, never keys, and survives re-runs."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parent.parent


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "payer_wallet", REPO / "scripts" / "payer_wallet.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_create_writes_key_and_prints_only_address(tmp_path, capsys) -> None:
    module = _script()
    key_file = tmp_path / "wallet.key"
    assert module.create(key_file) == 0
    printed = capsys.readouterr().out
    payload = json.loads(printed)
    assert payload["created"] is True
    assert payload["address"].startswith("0x")
    key = key_file.read_text(encoding="utf-8").strip()
    assert key not in printed
    assert len(key) == 64


def test_create_is_idempotent(tmp_path, capsys) -> None:
    module = _script()
    key_file = tmp_path / "wallet.key"
    module.create(key_file)
    first = key_file.read_text(encoding="utf-8")
    capsys.readouterr()
    module.create(key_file)
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] is False
    assert key_file.read_text(encoding="utf-8") == first


def test_address_reads_the_same_wallet(tmp_path, capsys) -> None:
    module = _script()
    key_file = tmp_path / "wallet.key"
    module.create(key_file)
    created = json.loads(capsys.readouterr().out)["address"]
    from eth_account import Account

    address = Account.from_key(key_file.read_text(encoding="utf-8").strip()).address
    assert address == created
