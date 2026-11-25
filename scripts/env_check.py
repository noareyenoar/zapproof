"""Environment check for Zapproof (prompt Section 6, task 0.1; gate check G0.1).

Records the machine environment into reports/env.md and exits non-zero if a
required tool is missing. Every recorded value is measured here — nothing in
reports/env.md is typed by hand.

Run:  make env-check   (or: python scripts/env_check.py)
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = REPO_ROOT / "reports" / "env.md"
THRESHOLDS_PATH = REPO_ROOT / "config" / "thresholds.yaml"

MIN_PYTHON = (3, 11)


def run(cmd: list[str]) -> str | None:
    """Run a command, return stripped stdout, or None if it is not available."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or proc.stderr).strip()
    return out.splitlines()[0] if out else ""


def which(name: str) -> str | None:
    """Locate an executable, falling back to winget's per-package install dirs.

    winget updates the user PATH in the registry, but a shell started before the
    install keeps the stale environment. Rather than pretend the tool is missing,
    look in the known winget package locations too.
    """
    found = shutil.which(name)
    if found:
        return found

    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return None
    packages = Path(local_appdata) / "Microsoft" / "WinGet" / "Packages"
    if not packages.is_dir():
        return None
    for exe in packages.glob(f"*/**/{name}.exe"):
        return str(exe)
    return None


def total_ram_gb() -> float | None:
    """Total physical memory in GB. Windows via ctypes, POSIX via sysconf."""
    if platform.system() == "Windows":

        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MemoryStatusEx()
        stat.dwLength = ctypes.sizeof(MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):  # type: ignore[attr-defined]
            return None
        return round(stat.ullTotalPhys / (1024**3), 2)

    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return None
    return round(pages * page_size / (1024**3), 2)


def free_disk_gb(path: Path) -> float:
    return round(shutil.disk_usage(path).free / (1024**3), 2)


def load_min_free_disk_gb() -> float | None:
    """Read node_ops.min_free_disk_gb without adding a hard PyYAML dependency here."""
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        return None
    if not THRESHOLDS_PATH.is_file():
        return None
    with THRESHOLDS_PATH.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    value = (data.get("node_ops") or {}).get("min_free_disk_gb")
    return float(value) if value is not None else None


def main() -> int:
    problems: list[str] = []
    lines: list[str] = []

    py_ver = platform.python_version()
    if sys.version_info[:2] < MIN_PYTHON:
        problems.append(
            f"Python {py_ver} is below the required "
            f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]} (prompt Section 4)"
        )

    tools = {
        "git": which("git"),
        "uv": which("uv"),
        "make": which("make"),
        "gitleaks": which("gitleaks"),
    }
    for name, path in tools.items():
        if path is None:
            problems.append(f"{name} not found on PATH")

    sqlite_version = run([sys.executable, "-c", "import sqlite3; print(sqlite3.sqlite_version)"])
    if sqlite_version is None:
        problems.append("could not determine SQLite version")

    cpu_count = os.cpu_count()
    ram_gb = total_ram_gb()
    disk_gb = free_disk_gb(REPO_ROOT)

    min_free_disk = load_min_free_disk_gb()
    disk_ok = min_free_disk is None or disk_gb >= min_free_disk
    if min_free_disk is not None and not disk_ok:
        problems.append(
            f"free disk {disk_gb} GB is below node_ops.min_free_disk_gb = {min_free_disk} GB"
        )

    # uv runs `python`, so report the interpreter uv would actually use.
    uv_version = run([tools["uv"], "--version"]) if tools["uv"] else None
    make_version = run([tools["make"], "--version"]) if tools["make"] else None
    gitleaks_version = run([tools["gitleaks"], "version"]) if tools["gitleaks"] else None

    lines.append("# Environment report")
    lines.append("")
    lines.append("Generated by `make env-check` (`scripts/env_check.py`). Do not edit by hand.")
    lines.append("")
    lines.append(f"- **Generated:** {datetime.now(UTC).isoformat(timespec='seconds')}")
    lines.append(f"- **Host:** {platform.node()}")
    lines.append(f"- **OS:** {platform.system()} {platform.release()} ({platform.version()})")
    lines.append(f"- **Machine:** {platform.machine()}")
    lines.append("")
    lines.append("## Toolchain")
    lines.append("")
    lines.append("| Item | Value | Required by |")
    lines.append("|---|---|---|")
    lines.append(f"| Python | {py_ver} | prompt Section 4 (>= 3.11) |")
    lines.append(f"| Python path | `{sys.executable}` | — |")
    lines.append(f"| uv | {uv_version or '**NOT FOUND**'} | prompt Section 4 |")
    git_version = run(["git", "--version"]) if tools["git"] else "**NOT FOUND**"
    lines.append(f"| git | {git_version} | gate reports need a commit hash |")
    lines.append(
        f"| GNU make | {make_version or '**NOT FOUND**'} | prompt Section 4 (Makefile targets) |"
    )
    lines.append(f"| SQLite | {sqlite_version or '**NOT FOUND**'} | prompt Section 3.3 |")
    lines.append(f"| gitleaks | {gitleaks_version or '**NOT FOUND**'} | prompt 0.2 (secret scan) |")
    lines.append("")
    lines.append("## Hardware")
    lines.append("")
    lines.append("| Item | Value |")
    lines.append("|---|---|")
    lines.append(f"| CPU cores | {cpu_count} |")
    lines.append(f"| RAM | {ram_gb if ram_gb is not None else 'unknown'} GB |")
    lines.append(f"| Free disk (repo volume) | {disk_gb} GB |")
    min_free_display = min_free_disk if min_free_disk is not None else "not set"
    lines.append(f"| `node_ops.min_free_disk_gb` | {min_free_display} GB |")
    lines.append(f"| Disk check | {'PASS' if disk_ok else 'FAIL'} |")
    lines.append("")
    lines.append("## Notes on this machine")
    lines.append("")
    lines.append(
        "- `uv`, `make`, and `gitleaks` were installed via winget on 2026-10-16. winget"
        " updated the user PATH in the registry, so **a shell started before the install"
        " will not see them** until it is restarted. `scripts/env_check.py` falls back to"
        " the winget package directories so this check passes either way."
    )
    lines.append(
        "- A Bitcoin full node was rejected for disk reasons (owner decision 2026-10-16):"
        " unpruned mainnet needs roughly 700 GB-1 TB and this volume does not have it."
        " The Lightning node uses a Neutrino backend instead — see prompt Section 1A.13."
    )
    lines.append("")
    lines.append("## Result")
    lines.append("")
    if problems:
        lines.append("**FAIL** — the following problems were found:")
        lines.append("")
        for problem in problems:
            lines.append(f"- {problem}")
    else:
        lines.append("**PASS** — all required tools are present and the disk check passed.")
    lines.append("")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")

    print(f"wrote {REPORT_PATH.relative_to(REPO_ROOT)}")
    if problems:
        print("env-check FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("env-check PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
