#!/usr/bin/env python3
"""Separate the board-graph tool checkout (TOOL_ROOT) from the project checkout (PROJECT_ROOT).

When BOARD_GRAPH_PROJECT is unset (or matches TOOL_ROOT), scripts run in vendored mode where
TOOL_ROOT == PROJECT_ROOT, preserving exact backwards compatibility for existing repositories.
When BOARD_GRAPH_PROJECT (or --project <dir>) names another directory, tool assets come from
TOOL_ROOT while project files, graphs, boards, records, and reports come from that directory.

Standard library only; imports under Python 3.9.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import pathlib
import shlex
import subprocess
import sys

from no_window import KWARGS as NO_WINDOW

TOOL_ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV = "BOARD_GRAPH_PROJECT"
ARGUMENT = "--project"
MANIFEST = "tools/board_graph_tool.json"
PROJECT_MARKERS = (
    "tools/board_graph.json",
    "tools/board_graph_site.json",
    "tools/board_graph_boards.json",
    "tools/board_graph_pack.json",
    "board/board.json",
)


def project_root(environ=None) -> pathlib.Path:
    """Return the active project root: BOARD_GRAPH_PROJECT when set, else TOOL_ROOT."""
    env = os.environ if environ is None else environ
    value = env.get(ENV)
    if value:
        return pathlib.Path(value).expanduser().resolve()
    return TOOL_ROOT


def is_external(environ=None) -> bool:
    """True when BOARD_GRAPH_PROJECT points at a directory distinct from TOOL_ROOT."""
    env = os.environ if environ is None else environ
    value = env.get(ENV)
    if not value:
        return False
    return pathlib.Path(value).expanduser().resolve() != TOOL_ROOT.resolve()


def take_argument(argv=None, environ=None) -> pathlib.Path | None:
    """Extract `--project <dir>` or `--project=<dir>` before modules read project settings.

    Removes all `--project` occurrences (before `--`) from `argv` in place, verifies the target
    is an existing directory, sets BOARD_GRAPH_PROJECT in `environ` (default os.environ), and
    returns the resolved Path (or None if `--project` was not supplied).
    """
    target = sys.argv if argv is None else argv
    start = 1 if argv is None else 0
    selected = None
    kept = list(target[:start])
    i = start
    while i < len(target):
        word = target[i]
        if word == "--":
            kept.extend(target[i:])
            break
        if word == ARGUMENT:
            if i + 1 >= len(target):
                print(f"{ARGUMENT} requires a project directory path", file=sys.stderr)
                raise SystemExit(2)
            selected = target[i + 1]
            i += 2
            continue
        if word.startswith(ARGUMENT + "="):
            selected = word.split("=", 1)[1]
            i += 1
            continue
        kept.append(word)
        i += 1
    target[:] = kept
    if selected is None:
        return None
    if not selected.strip():
        print(f"{ARGUMENT} requires a non-empty project directory path", file=sys.stderr)
        raise SystemExit(2)
    resolved = pathlib.Path(selected).expanduser().resolve()
    if not resolved.is_dir():
        print(f"project directory does not exist: {resolved}", file=sys.stderr)
        raise SystemExit(2)
    env = os.environ if environ is None else environ
    env[ENV] = str(resolved)
    return resolved


def find_project_root(start=None, environ=None) -> pathlib.Path:
    """Locate the project directory from BOARD_GRAPH_PROJECT or by walking upward from `start`."""
    env = os.environ if environ is None else environ
    value = env.get(ENV)
    if value:
        resolved = pathlib.Path(value).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"{ENV} directory does not exist: {resolved}")
        return resolved
    origin = pathlib.Path(start if start is not None else os.getcwd()).expanduser().resolve()
    if origin.is_file():
        origin = origin.parent
    for cur in (origin, *origin.parents):
        if any((cur / marker).exists() for marker in PROJECT_MARKERS):
            return cur
        if (cur / ".git").exists():
            break
    raise ValueError(f"no board-graph project found from {origin}")


def tool_file(rel: str) -> pathlib.Path:
    """Return a path inside the shared board-graph tool checkout."""
    return TOOL_ROOT / rel


@functools.lru_cache(maxsize=1)
def _manifest_files() -> frozenset[str]:
    manifest_path = TOOL_ROOT / MANIFEST
    if not manifest_path.is_file():
        return frozenset()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    files = data.get("files")
    if isinstance(files, dict):
        return frozenset(files.keys())
    return frozenset()


def shared_tool_file(rel: str, environ=None) -> pathlib.Path | None:
    """In external mode, return TOOL_ROOT/<rel> only when <rel> is a shared tool file in the manifest.

    Project starter files (like docs/PRODUCT_TRUTH.md, AGENTS.md, board/DECISIONS.md) are never
    in `manifest['files']`, so a missing project doc never silently falls back to the tool's template.
    """
    if not is_external(environ):
        return None
    normalized = pathlib.PurePosixPath(rel.replace("\\", "/")).as_posix()
    if normalized not in _manifest_files():
        return None
    candidate = (TOOL_ROOT / normalized).resolve()
    if candidate.is_relative_to(TOOL_ROOT.resolve()) and candidate.is_file():
        return candidate
    return None


def project_script(rel: str, root=None) -> pathlib.Path:
    """Prefer a script inside the project checkout if present, else fall back to TOOL_ROOT."""
    project = pathlib.Path(root) if root is not None else project_root()
    candidate = project / rel
    if candidate.is_file():
        return candidate
    return TOOL_ROOT / rel


def is_git_checkout(tool_root: pathlib.Path = TOOL_ROOT) -> bool:
    """True when `tool_root` is a git checkout (not a packaged wheel data directory)."""
    return (pathlib.Path(tool_root) / ".git").exists()


def tool_commit(tool_root: pathlib.Path = TOOL_ROOT) -> str | None:
    """Return the 40-character git HEAD SHA of `tool_root`, or None if unavailable."""
    if not is_git_checkout(tool_root):
        return None
    try:
        done = subprocess.run(
            ["git", "-C", str(tool_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
            **NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    sha = done.stdout.strip()
    if done.returncode != 0 or len(sha) != 40:
        return None
    return sha


DIRTY_EXCLUDES = (
    ":(exclude)*.kicad_prl",
    ":(exclude)*.lck",
    ":(exclude)~*.lck",
    ":(exclude)*-backups/",
    ":(exclude)fp-info-cache",
)


def dirty_status_shell_args() -> str:
    """Return quoted git pathspec arguments excluding ephemeral KiCad sidecars from status checks."""
    return " ".join(shlex.quote(arg) for arg in ("--", *DIRTY_EXCLUDES))


def tool_dirty(tool_root: pathlib.Path = TOOL_ROOT) -> list[str]:
    """Return non-empty `git status --porcelain` lines in `tool_root`, excluding ephemeral KiCad sidecars."""
    if not is_git_checkout(tool_root):
        return []
    try:
        done = subprocess.run(
            ["git", "-C", str(tool_root), "status", "--porcelain", "--", *DIRTY_EXCLUDES],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
            **NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ["git status failed"]
    if done.returncode != 0:
        return [(done.stderr or done.stdout or "git status failed").strip()]
    return [line for line in done.stdout.splitlines() if line.strip()]


def tool_fingerprint(root: pathlib.Path, environ=None) -> str | None:
    """Hash recorded under `tools/board_graph_tool.json` on card attempts.

    In vendored checkouts, hashes `<root>/tools/board_graph_tool.json` (or returns None if absent).
    In external tool mode without a local manifest, hashes TOOL_ROOT's manifest and git HEAD SHA.
    """
    local = pathlib.Path(root) / MANIFEST
    if local.is_file():
        return hashlib.sha256(local.read_bytes()).hexdigest()
    if not is_external(environ):
        return None
    manifest = TOOL_ROOT / MANIFEST
    commit = tool_commit(TOOL_ROOT)
    if not manifest.is_file() and not commit:
        return None
    digest = hashlib.sha256()
    if manifest.is_file():
        digest.update(manifest.read_bytes())
    if commit:
        digest.update(commit.encode("ascii"))
    return digest.hexdigest()
