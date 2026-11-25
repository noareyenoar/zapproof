"""Render the public site artifacts for real URLs.

The templates under `distribution/site/templates/` carry `__BASE_URL__` (the site) and
`__API_URL__` (the hosted API and MCP origin) rather than plausible-looking domains,
so nothing in the repository advertises an endpoint that does not exist. This script
substitutes the owner's URLs at render time and validates the result: XML for the
sitemap, parseable JSON-LD for the landing page, and no token left behind.

The agent skill has one source at `src/repindex/api/static/skill.md`; it is rendered
to `skill.md` beside the site files with the API URL, so the copy an agent downloads
from the site is the same copy the API serves. The Thai skill at
`src/repindex/api/static/skill.th.md` is rendered the same way to `skill.th.md`;
Thai templates live under `templates/th/` and are written to `th/`.

Run:
    uv run python scripts/render_site.py \
      --base-url https://zapproof.web.app \
      --api-url https://zapproof-123.us-central1.run.app \
      --out public/
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "distribution" / "site" / "templates"
STATIC = REPO_ROOT / "distribution" / "site" / "static"
SKILL_SOURCES = (
    (REPO_ROOT / "src" / "repindex" / "api" / "static" / "skill.md", "skill.md"),
    (REPO_ROOT / "src" / "repindex" / "api" / "static" / "skill.th.md", "skill.th.md"),
)
TOKEN = "__BASE_URL__"
API_TOKEN = "__API_URL__"
LAST_MOD = "__LASTMOD__"
JSON_LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)


def _base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise argparse.ArgumentTypeError("base URL must be absolute, e.g. https://zapproof.com")
    return value.rstrip("/")


def render(
    templates: Path,
    out: Path,
    base_url: str,
    api_url: str | None = None,
    lastmod: str | None = None,
) -> list[Path]:
    """Write every template, then the skill, with both tokens replaced.

    `static/` files are copied verbatim: the IndexNow key file must be served at the
    site root under its key-named file, and it contains no placeholder to render.
    """
    api_url = api_url or base_url
    lastmod = lastmod or datetime.now(UTC).date().isoformat()
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for template in sorted(templates.rglob("*")):
        if not template.is_file():
            continue
        text = template.read_text(encoding="utf-8")
        if TOKEN not in text and API_TOKEN not in text:
            raise ValueError(
                f"{template.relative_to(templates)} has neither {TOKEN} nor "
                f"{API_TOKEN} — is it the right template?"
            )
        rendered = (
            text.replace(API_TOKEN, api_url)
            .replace(TOKEN, base_url)
            .replace(LAST_MOD, lastmod)
        )
        for token in (TOKEN, API_TOKEN, LAST_MOD):
            if token in rendered:
                raise ValueError(
                    f"{template.relative_to(templates)} still contains {token} "
                    "after rendering"
                )
        destination = out / template.relative_to(templates)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8", newline="\n")
        written.append(destination)

    if STATIC.is_dir():
        for source in sorted(STATIC.rglob("*")):
            if not source.is_file():
                continue
            destination = out / source.relative_to(STATIC)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
            written.append(destination)

    for skill_source, skill_name in SKILL_SOURCES:
        if not skill_source.is_file():
            raise ValueError(f"skill source not found: {skill_source}")
        skill = skill_source.read_text(encoding="utf-8")
        if API_TOKEN not in skill:
            raise ValueError(f"{skill_source.name} has no {API_TOKEN} token")
        rendered = skill.replace(API_TOKEN, api_url)
        if API_TOKEN in rendered or TOKEN in rendered:
            raise ValueError(f"{skill_name} still contains a placeholder after rendering")
        destination = out / skill_name
        destination.write_text(rendered, encoding="utf-8", newline="\n")
        written.append(destination)
    return written


def validate(paths: list[Path]) -> None:
    """Fail loudly on a sitemap, robots file or JSON-LD block that would not parse."""
    for path in paths:
        if path.name == "sitemap.xml":
            root = ET.parse(path).getroot()
            if not root.tag.endswith("urlset"):
                raise ValueError(f"{path} is not a sitemap urlset")
        elif path.name == "robots.txt":
            if "Sitemap: " not in path.read_text(encoding="utf-8"):
                raise ValueError(f"{path} has no Sitemap line")
        elif path.suffix == ".html":
            blocks = JSON_LD.findall(path.read_text(encoding="utf-8"))
            if not blocks:
                raise ValueError(f"{path} has no JSON-LD block")
            for block in blocks:
                json.loads(block)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True, type=_base_url)
    parser.add_argument(
        "--api-url",
        type=_base_url,
        default=None,
        help="hosted API/MCP origin; defaults to --base-url",
    )
    parser.add_argument("--templates", type=Path, default=TEMPLATES)
    parser.add_argument(
        "--lastmod",
        default=None,
        help="sitemap lastmod date (default: today, UTC)",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    written = render(
        args.templates,
        args.out,
        args.base_url,
        args.api_url or args.base_url,
        lastmod=args.lastmod,
    )
    validate(written)
    for path in written:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
