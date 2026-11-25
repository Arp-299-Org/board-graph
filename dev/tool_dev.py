#!/usr/bin/env python3
"""Repo-local wrapper around scripts/board_graph.py for the tool's own dev/release workflow."""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys

TOOL_ROOT = pathlib.Path(__file__).resolve().parent.parent
ROOT = pathlib.Path(os.environ.get("BOARD_GRAPH_PROJECT") or TOOL_ROOT).resolve()
GRAPH = ROOT / "dev" / "tool_graph.json"
PACK = ROOT / "dev" / "tool_pack.json"
STATE_BASE = ROOT / "logs" / "tool-dev"
RELEASES_DIR = STATE_BASE / "releases"
CURRENT_FILE = STATE_BASE / "current.json"
VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")


def normalize_version(raw: str) -> str:
    match = VERSION_RE.fullmatch(raw.strip())
    if not match:
        raise SystemExit(f"invalid release version {raw!r}: expected X.Y.Z or vX.Y.Z")
    return f"v{match.group(1)}"


def cycle_paths(version: str) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    folder = RELEASES_DIR / normalize_version(version)
    return folder, folder / "state.sqlite3", folder / "records"


def _dev_env() -> dict[str, str]:
    env = dict(os.environ)
    env["BOARD_GRAPH_PACK"] = str(PACK)
    return env


def _run_bg(args: list[str], *, state_path: pathlib.Path, records_path: pathlib.Path, capture: bool = False) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        str(TOOL_ROOT / "scripts" / "board_graph.py"),
        "--graph",
        str(GRAPH),
        "--state",
        str(state_path),
        "--records",
        str(records_path),
        *args,
    ]
    return subprocess.run(cmd, cwd=str(ROOT), env=_dev_env(), capture_output=capture, text=capture)


def _cycle_position(version: str) -> str:
    _, state_path, records_path = cycle_paths(version)
    if not state_path.is_file():
        return "uninitialized"
    done = _run_bg(["where", "--json"], state_path=state_path, records_path=records_path, capture=True)
    if done.returncode != 0:
        return "error"
    data = json.loads(done.stdout)
    tracks = data.get("tracks") or []
    return str(tracks[0].get("position")) if tracks else "unknown"


def active_version() -> str:
    if not CURRENT_FILE.is_file():
        raise SystemExit("no active dev release cycle; start one with: python dev/tool_dev.py cycle vX.Y.Z")
    data = json.loads(CURRENT_FILE.read_text(encoding="utf-8"))
    ver = data.get("version")
    if not isinstance(ver, str) or not ver:
        raise SystemExit(f"corrupt {CURRENT_FILE}: missing 'version'")
    return ver


def cmd_cycle(raw_version: str, *, resume: bool = False, force: bool = False) -> int:
    version = normalize_version(raw_version)
    folder, state_path, records_path = cycle_paths(version)
    if state_path.exists() and not resume:
        raise SystemExit(
            f"release cycle {version} already exists at {folder}; pass --resume to switch to it"
        )
    if CURRENT_FILE.is_file():
        prev = active_version()
        if prev != version:
            pos = _cycle_position(prev)
            if pos not in ("done", "uninitialized") and not force:
                raise SystemExit(
                    f"active cycle {prev} stands at '{pos}', not 'done'; finish it or pass --force to switch"
                )
    records_path.mkdir(parents=True, exist_ok=True)
    CURRENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CURRENT_FILE.write_text(json.dumps({"version": version}, indent=2) + "\n", encoding="utf-8")
    done = _run_bg(["where"], state_path=state_path, records_path=records_path)
    return done.returncode


def cmd_cycles() -> int:
    active = active_version() if CURRENT_FILE.is_file() else None
    if not RELEASES_DIR.is_dir():
        print("No release cycles started under logs/tool-dev/releases/")
        return 0
    for child in sorted(RELEASES_DIR.iterdir()):
        if child.is_dir():
            pos = _cycle_position(child.name)
            marker = "*" if child.name == active else " "
            print(f"{marker} {child.name}: {pos}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(
            "usage:\n"
            "  python dev/tool_dev.py cycle vX.Y.Z [--resume] [--force]\n"
            "  python dev/tool_dev.py cycles\n"
            "  python dev/tool_dev.py [--release vX.Y.Z] <where|check|brief|approvals|next|record|settle|graph> ..."
        )
        return 0
    if args[0] == "cycle":
        if len(args) < 2:
            raise SystemExit("usage: python dev/tool_dev.py cycle vX.Y.Z [--resume] [--force]")
        rest = set(args[2:])
        unknown = rest - {"--resume", "--force"}
        if unknown:
            raise SystemExit(f"unknown flags for cycle: {', '.join(sorted(unknown))}")
        return cmd_cycle(args[1], resume="--resume" in rest, force="--force" in rest)
    if args[0] == "cycles":
        return cmd_cycles()

    release = None
    if args[0] == "--release":
        if len(args) < 3:
            raise SystemExit("usage: python dev/tool_dev.py --release vX.Y.Z <command> ...")
        release = normalize_version(args[1])
        args = args[2:]
    else:
        release = active_version()

    _, state_path, records_path = cycle_paths(release)
    records_path.mkdir(parents=True, exist_ok=True)
    return _run_bg(args, state_path=state_path, records_path=records_path).returncode


if __name__ == "__main__":
    raise SystemExit(main())
