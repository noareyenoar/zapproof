"""Add a remote endpoint to `server.json` for the official MCP Registry.

`server.json` now carries both a remote and the PyPI package: the first published entry
was remotes-only until PyPI existed, and `--with-package --version` kept the package
block once it did. This script takes the common fields from `server.json`, adds
`remotes`, and can omit the package block for a remote-only entry. The output is a plain
`server.json` the owner can publish and commit.

Run after the Cloud Run deploy and a passing `scripts/smoke_hosted.py`:
    uv run python scripts/render_registry.py --mcp-url https://SERVICE-PROJECT.REGION.run.app/mcp
    uv run python scripts/render_registry.py --mcp-url ... --with-package --version 0.1.1
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = REPO_ROOT / "server.json"
NAME_PATTERN = re.compile(r"^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$")


def build(
    source: Path,
    *,
    mcp_url: str,
    with_package: bool,
    version: str | None = None,
) -> dict[str, object]:
    data: dict[str, object] = json.loads(source.read_text(encoding="utf-8"))

    parsed = urlparse(mcp_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise SystemExit("--mcp-url must be an absolute https URL")
    data["remotes"] = [{"type": "streamable-http", "url": mcp_url}]

    if not with_package:
        data.pop("packages", None)
    if version:
        data["version"] = version
        packages = data.get("packages")
        if isinstance(packages, list):
            for package in packages:
                if isinstance(package, dict):
                    package["version"] = version

    name = str(data.get("name", ""))
    if not NAME_PATTERN.match(name) or name.count("/") != 1:
        raise SystemExit(f"invalid registry name: {name!r}")
    description = str(data.get("description", ""))
    if not 1 <= len(description) <= 100:
        raise SystemExit("description must be 1-100 characters")
    if not str(data.get("version", "")):
        raise SystemExit("version is required")
    if not isinstance(data.get("remotes"), list) or not data["remotes"]:
        raise SystemExit("remotes must be a non-empty list")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mcp-url", required=True)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--out", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--with-package",
        action="store_true",
        help="keep the PyPI packages block (after the package is published)",
    )
    parser.add_argument("--version", default=None)
    args = parser.parse_args(argv)

    data = build(
        args.source,
        mcp_url=args.mcp_url,
        with_package=args.with_package,
        version=args.version,
    )
    args.out.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
