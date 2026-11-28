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
HEAL_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def normalize_version(raw: str, mode: str = "release") -> str:
    if mode == "heal":
        slug = raw.strip()
        if not HEAL_SLUG_RE.fullmatch(slug):
            raise SystemExit(f"invalid heal slug {raw!r}: expected [A-Za-z0-9._-]+")
        return slug
    match = VERSION_RE.fullmatch(raw.strip())
    if not match:
        raise SystemExit(f"invalid release version {raw!r}: expected X.Y.Z or vX.Y.Z")
    return f"v{match.group(1)}"


def cycle_paths(version: str, mode: str | None = None) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    slug = normalize_version(version, mode=mode or ("release" if VERSION_RE.fullmatch(version.strip()) else "heal"))
    folder = RELEASES_DIR / slug
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


def cmd_cycle(
    raw_version: str,
    *,
    mode: str = "release",
    from_card: str | None = None,
    board_project: str | None = None,
    resume: bool = False,
    force: bool = False,
) -> int:
    if mode not in ("release", "heal"):
        raise SystemExit(f"invalid mode {mode!r}: expected release or heal")
    version = normalize_version(raw_version, mode=mode)
    folder, state_path, records_path = cycle_paths(version, mode=mode)
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
    meta: dict[str, object] = {"version": version, "mode": mode}
    if from_card:
        meta["from_card"] = from_card
    if board_project:
        meta["board_project"] = board_project
    CURRENT_FILE.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
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
            raise SystemExit("usage: python dev/tool_dev.py cycle <vX.Y.Z|slug> [--mode release|heal] [--from-card CARD] [--board-project DIR] [--resume] [--force]")
        mode, from_card, board_project, resume, force = "release", None, None, False, False
        i = 2
        while i < len(args):
            tok = args[i]
            if tok == "--resume":
                resume = True
                i += 1
            elif tok == "--force":
                force = True
                i += 1
            elif tok in ("--mode", "--from-card", "--board-project") and i + 1 < len(args):
                if tok == "--mode":
                    mode = args[i + 1]
                elif tok == "--from-card":
                    from_card = args[i + 1]
                else:
                    board_project = args[i + 1]
                i += 2
            else:
                raise SystemExit(f"unknown argument for cycle: {tok}")
        return cmd_cycle(args[1], mode=mode, from_card=from_card, board_project=board_project, resume=resume, force=force)
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
