"""The IndexNow ping must point at the key file the site actually serves.

The key is public by design (it is the file name at the site root), so there is no
secret here — but a wrong host, key or path silently fails verification, and search
engines do not report why.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parent.parent


def _indexnow() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "indexnow_ping", REPO / "scripts" / "indexnow_ping.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_key_file_is_named_after_its_key() -> None:
    module = _indexnow()
    path = module.key_file()

    assert 8 <= len(path.stem) <= 128
    assert all(char in "0123456789abcdef" for char in path.stem)
    assert path.read_text(encoding="utf-8").strip() == path.stem


def test_the_payload_uses_the_site_root_key_location() -> None:
    module = _indexnow()

    payload = module.build_payload("https://zapproof.web.app/", "abc123")

    assert payload["host"] == "zapproof.web.app"
    assert payload["key"] == "abc123"
    assert payload["keyLocation"] == "https://zapproof.web.app/abc123.txt"
    assert payload["urlList"][0] == "https://zapproof.web.app/"
    assert "https://zapproof.web.app/agents/" in payload["urlList"]


def test_a_non_https_base_url_is_rejected() -> None:
    module = _indexnow()
    for value in ("http://zapproof.web.app", "zapproof.web.app"):
        try:
            module.build_payload(value, "abc123")
        except SystemExit:
            continue
        raise AssertionError(f"{value} was accepted")
