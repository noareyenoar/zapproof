"""Discovery artifacts must be publishable before anything is published.

The registry caps `description` at 100 characters and matches searches against the
server *name*, so a name without the category keyword is unfindable regardless of
copy quality. The site templates carry a token instead of a domain so no artifact
advertises an endpoint that does not answer. These are the failures that only show
up after a submission, when the listing is already public.
"""

from __future__ import annotations

import importlib.util
import json
import re
import tomllib
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parent.parent
SERVER_JSON = REPO / "server.json"
INSTALL = REPO / "llms-install.md"
TOKEN = "__BASE_URL__"

BANNED = (
    "scam",
    "fraud",
    "fake",
    "criminal",
    "wash trader",
    "wash-trading",
    "safe",
    "verified",
    "guaranteed",
    "trust score",
)


def _project() -> dict[str, object]:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    project: dict[str, object] = data["project"]
    return project


def _renderer() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "render_site", REPO / "scripts" / "render_site.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _banned_hits(text: str) -> list[str]:
    return [
        term
        for term in BANNED
        if re.search(rf"\b{re.escape(term)}\w*\b", text, re.IGNORECASE)
    ]


def test_server_json_is_valid_for_the_registry() -> None:
    """Either shape is publishable: a remote URL alone, or the PyPI package."""
    server = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
    project = _project()

    assert server["$schema"].endswith("/2025-12-11/server.schema.json")
    assert re.fullmatch(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+", server["name"])
    assert server["name"].startswith("io.github.noareyenoar/")
    # Registry search matches the server name only. Without the keyword literal, no
    # amount of description copy makes the server findable.
    assert "x402" in server["name"]
    assert 1 <= len(server["description"]) <= 100
    assert server["version"] == project["version"]

    remotes = server.get("remotes")
    packages = server.get("packages") or []
    assert remotes or packages, "the registry entry must resolve to something"

    for remote in remotes or []:
        assert remote["type"] in ("streamable-http", "sse")
        assert str(remote["url"]).startswith("https://")

    for package in packages:
        assert package["registryType"] == "pypi"
        assert package["identifier"] == project["name"]
        assert package["version"] == project["version"]
        assert package["transport"] == {"type": "stdio"}


def test_server_json_never_pins_a_moving_version() -> None:
    server = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
    for package in server.get("packages") or []:
        assert package["version"] != "latest"


def test_install_doc_names_the_tool_and_its_three_refusals() -> None:
    text = INSTALL.read_text(encoding="utf-8")

    assert "check_counterparty" in text
    assert "uvx" in text
    assert "not a low score" in text
    assert "rail_not_indexed" in text
    assert "not independently validated" in text


def test_site_templates_render_with_a_real_domain_and_validate(tmp_path: Path) -> None:
    renderer = _renderer()
    out = tmp_path / "site"

    written = renderer.render(REPO / "distribution" / "site" / "templates", out, "https://zp.test")
    renderer.validate(written)

    names = {path.relative_to(out).as_posix() for path in written}
    assert {
        "index.html",
        "llms.txt",
        "llms.th.txt",
        "robots.txt",
        "sitemap.xml",
        "skill.md",
        "skill.th.md",
        "th/index.html",
    } <= names
    for path in written:
        assert TOKEN not in path.read_text(encoding="utf-8")
    assert "https://zp.test/sitemap.xml" in (out / "robots.txt").read_text(encoding="utf-8")
    assert "https://zp.test/th/" in (out / "sitemap.xml").read_text(encoding="utf-8")
    assert 'hreflang="th"' in (out / "index.html").read_text(encoding="utf-8")
    assert 'hreflang="en"' in (out / "th" / "index.html").read_text(encoding="utf-8")


def test_served_copy_uses_no_banned_assertion(tmp_path: Path) -> None:
    """Nothing an agent reads may accuse an entity or promise a verdict."""
    renderer = _renderer()
    out = tmp_path / "site"
    renderer.render(REPO / "distribution" / "site" / "templates", out, "https://zp.test")

    rendered = sorted(path for path in out.rglob("*") if path.is_file())
    names = ["server.json", "llms-install.md"] + [
        path.relative_to(out).as_posix() for path in rendered
    ]
    texts = [SERVER_JSON.read_text(encoding="utf-8"), INSTALL.read_text(encoding="utf-8")]
    texts += [path.read_text(encoding="utf-8") for path in rendered]

    found = {
        name: terms
        for name, text in zip(names, texts, strict=True)
        if (terms := _banned_hits(text))
    }
    assert found == {}


def test_thai_copy_keeps_the_machine_contract_and_the_unknown_rule() -> None:
    """A translation may explain the contract; it may not rename its fields or soften it."""
    skill = (REPO / "src" / "repindex" / "api" / "static" / "skill.th.md").read_text(
        encoding="utf-8"
    )
    page = (REPO / "distribution" / "site" / "templates" / "th" / "index.html").read_text(
        encoding="utf-8"
    )

    for field in ("found: false", "insufficient_evidence", "rail_not_indexed", "amount"):
        assert field in skill
    assert "คะแนนศูนย์" in skill
    assert 'ไม่ใช่ "แย่"' in skill
    assert 'lang="th"' in page
    assert "__API_URL__/mcp" in page
    assert "__BASE_URL__/th/" in page


def _registry() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "render_registry", REPO / "scripts" / "render_registry.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_remote_only_registry_entry_needs_no_package() -> None:
    """The registry accepts `remotes` alone, so the hosted endpoint can ship first."""
    data = _registry().build(
        SERVER_JSON,
        mcp_url="https://zapproof-abc-uc.a.run.app/mcp",
        with_package=False,
    )

    assert data["remotes"] == [
        {"type": "streamable-http", "url": "https://zapproof-abc-uc.a.run.app/mcp"}
    ]
    assert "packages" not in data
    assert "x402" in data["name"]
    assert 1 <= len(data["description"]) <= 100


def test_the_package_entry_is_kept_and_versioned_once_it_is_published(
    tmp_path: Path,
) -> None:
    # Supply the package block explicitly so the test does not depend on the
    # committed entry's package fields.
    source = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
    source["packages"] = [
        {
            "registryType": "pypi",
            "identifier": "zapproof-mcp",
            "version": "0.1.0",
            "runtimeHint": "uvx",
            "transport": {"type": "stdio"},
        }
    ]
    path = tmp_path / "server.json"
    path.write_text(json.dumps(source), encoding="utf-8")

    data = _registry().build(
        path,
        mcp_url="https://zapproof-abc-uc.a.run.app/mcp",
        with_package=True,
        version="0.1.1",
    )

    assert data["version"] == "0.1.1"
    assert data["packages"][0]["version"] == "0.1.1"
    assert data["packages"][0]["identifier"] == "zapproof-mcp"


def test_the_registry_entry_refuses_a_non_https_remote() -> None:
    registry = _registry()
    for url in ("http://zapproof.run.app/mcp", "zapproof.run.app/mcp"):
        try:
            registry.build(SERVER_JSON, mcp_url=url, with_package=False)
        except SystemExit as exc:
            assert "https" in str(exc)
        else:  # pragma: no cover - a passing build here is the failure
            raise AssertionError(f"{url} was accepted")
