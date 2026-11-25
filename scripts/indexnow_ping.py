"""Tell IndexNow (Bing and friends) that the site pages changed.

The key file lives at `distribution/site/static/<key>.txt` and is copied to the site
root by `scripts/render_site.py`; its file name IS the key and its content must match.
IndexNow only needs the key file to be reachable — no account is required.

Run after a site deploy:

    uv run python scripts/indexnow_ping.py --base-url https://zapproof.web.app
    uv run python scripts/indexnow_ping.py --base-url https://zapproof.web.app --dry-run

The base URL must be the public site (Firebase Hosting), not the API origin.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC = REPO_ROOT / "distribution" / "site" / "static"
ENDPOINT = "https://api.indexnow.org/indexnow"
PATHS = ("/", "/th/", "/agents/", "/agents/th/", "/payments-vs-trust/")


def key_file(static: Path = STATIC) -> Path:
    """Return the single `<key>.txt` file; more than one would make the key ambiguous."""
    candidates = sorted(path for path in static.glob("*.txt") if path.is_file())
    if len(candidates) != 1:
        raise SystemExit(f"expected exactly one IndexNow key file in {static}")
    return candidates[0]


def build_payload(base_url: str, key: str, paths: tuple[str, ...] = PATHS) -> dict:
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise SystemExit("--base-url must be an absolute https URL")
    base = f"https://{parsed.netloc}"
    return {
        "host": parsed.netloc,
        "key": key,
        "keyLocation": f"{base}/{key}.txt",
        "urlList": [f"{base}{path}" for path in paths],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--key-file", type=Path, default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the payload instead of sending it",
    )
    args = parser.parse_args(argv)

    key_path = args.key_file or key_file()
    key = key_path.stem
    if key_path.read_text(encoding="utf-8").strip() != key:
        raise SystemExit(f"{key_path} content must equal its file name (the key)")
    payload = build_payload(args.base_url, key)
    body = json.dumps(payload).encode("utf-8")

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"IndexNow request failed: {type(exc).__name__}", file=sys.stderr)
        return 1

    # IndexNow returns 200 (accepted) or 202 (accepted, key pending verification).
    print(f"IndexNow responded {status} for {len(payload['urlList'])} URLs")
    return 0 if status in (200, 202) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
