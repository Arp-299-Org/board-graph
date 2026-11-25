#!/usr/bin/env python3
"""A launchd job the repository installs on the Mac: its plist, written into ~/Library/LaunchAgents and loaded,
and what launchd says of it.

One script has one: scripts/board_graph_serve.py (the graph page, kept running). Its --install calls install()
with its plist's file name and bytes (the project's own file in tools/mac/ when it keeps one, else the one the
script writes from the site settings), and its status calls installed() and running().
"""
from __future__ import annotations

import os
import pathlib
import plistlib
import subprocess
import time

import tool_paths

ROOT = tool_paths.project_root()


def installed(name: str, data: bytes, home: pathlib.Path | None = None) -> str:
    """Whether launchd's copy of the plist `name` holds `data`, the project's: "installed", "differs" or "not
    installed"."""
    copy = (home or pathlib.Path.home()) / "Library" / "LaunchAgents" / name
    try:
        return "installed" if copy.read_bytes() == data else "differs"
    except FileNotFoundError:
        return "not installed"


def install(name: str, data: bytes, root: pathlib.Path = ROOT) -> tuple[int, str]:
    """Write the plist `data` into ~/Library/LaunchAgents as `name` and load it in the user's launchd domain,
    unloading the copy there first, so launchd runs the job once now (RunAtLoad). Refuses a plist whose
    WorkingDirectory is not this checkout. Returns (exit code, one line saying what was done or refused)."""
    job = plistlib.loads(data)
    if pathlib.Path(job["WorkingDirectory"]) != root:
        return 2, (f"refused: the plist runs the job in {job['WorkingDirectory']}, not this checkout ({root}); "
                   "install it from there")
    for key in ("StandardOutPath", "StandardErrorPath"):
        if job.get(key) and job[key] != "/dev/null":
            pathlib.Path(job[key]).parent.mkdir(parents=True, exist_ok=True)
    agents = pathlib.Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    target = agents / name
    target.write_bytes(data)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{job['Label']}"], capture_output=True, check=False)  # if loaded
    for _ in range(10):  # bootout finishes in the background; bootstrap fails until it has
        done = subprocess.run(["launchctl", "bootstrap", domain, str(target)], capture_output=True, text=True,
                              check=False)
        if done.returncode == 0:
            return 0, f"installed {target}: launchd loaded {job['Label']}"
        time.sleep(1)
    return 1, f"refused: launchctl bootstrap failed: {' '.join((done.stderr or done.stdout).split())[:200]}"


def running(label: str) -> dict | None:
    """What launchd says of the job `label`: its state and pid (parse_print), or None when it is not loaded or
    this is not the Mac."""
    try:
        done = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True,
                              check=False)
    except (OSError, AttributeError):  # no launchctl, or no getuid: not the Mac
        return None
    return parse_print(done.stdout) if done.returncode == 0 else None


def parse_print(text: str) -> dict:
    """The job's own lines of `launchctl print`, one tab deep: {"state": "running" or "not running" or what it
    said, "pid": the number, or None while it is not running}."""
    found = {"state": None, "pid": None}
    for line in text.splitlines():
        if line.startswith("\tstate = ") and found["state"] is None:
            found["state"] = line.split("=", 1)[1].strip()
        elif line.startswith("\tpid = ") and found["pid"] is None:
            found["pid"] = int(line.split("=", 1)[1].strip())
    return found
