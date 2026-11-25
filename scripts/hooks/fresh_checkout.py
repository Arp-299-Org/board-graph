#!/usr/bin/env python3
"""Stop an edit to a file that changed on GitHub's main since this checkout.

Claude Code runs this before every Edit, Write and NotebookEdit
(.claude/settings.json), which name the file in `tool_input.file_path` or
`notebook_path`; Codex runs it before every apply_patch (.codex/hooks.json),
whose `tool_input.command` is the patch, naming its files in `*** Update
File:`, `*** Add File:`, `*** Delete File:` and `*** Move to:` lines relative
to the session's cwd. It reads the hook's JSON on stdin, fetches origin at
most every two minutes, and blocks the edit (exit code 2, the reasons on
stderr) when origin/main has commits touching a named file that HEAD does not
have: an edit there would be made to a stale copy. Anything it cannot judge
(a file outside a repository, no origin/main, no network) is let through.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import no_window  # noqa: E402

FETCH_EVERY_SECONDS = 120


def git(cwd: pathlib.Path, *args: str, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        check=False, timeout=timeout, **no_window.KWARGS,
    )


PATCH_FILES = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.MULTILINE)


def targets(event: dict) -> list[pathlib.Path]:
    """The files the edit will touch: the one Claude Code names, or every file Codex's patch names."""
    tool_input = event.get("tool_input") or {}
    named = tool_input.get("file_path") or tool_input.get("notebook_path")
    if named:
        return [pathlib.Path(named)]
    base = pathlib.Path(event.get("cwd") or ".")
    return [base / (match.group(1) or match.group(2)).strip()
            for match in PATCH_FILES.finditer(tool_input.get("command") or "")]


def stale(path: pathlib.Path, fetched: set[pathlib.Path]) -> str | None:
    """Why editing `path` here would edit a stale copy, or None. Fetches each repository once per run."""
    path = path.resolve()
    folder = path.parent
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent  # a new file in a new folder
    top = git(folder, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return None
    root = pathlib.Path(top.stdout.strip())
    if git(root, "rev-parse", "--verify", "--quiet", "origin/main").returncode != 0:
        return None

    if root not in fetched:
        fetched.add(root)
        fetch_head = root / git(root, "rev-parse", "--git-path", "FETCH_HEAD").stdout.strip()
        if not fetch_head.exists() or time.time() - fetch_head.stat().st_mtime > FETCH_EVERY_SECONDS:
            try:
                git(root, "fetch", "--quiet", "origin", "main", timeout=20)
            except subprocess.TimeoutExpired:
                return None

    relative = path.relative_to(root).as_posix()
    behind = git(root, "log", "--format=%h %s", "HEAD..origin/main", "--", relative).stdout.splitlines()
    if not behind:
        return None
    return (f"{relative} changed on GitHub's main in {len(behind)} commit(s) this checkout does not have "
            f"(latest: {behind[0]}).")


def main() -> int:
    event = json.load(sys.stdin)
    fetched: set[pathlib.Path] = set()
    reasons = [reason for path in targets(event) if (reason := stale(path, fetched))]
    if not reasons:
        return 0
    print("\n".join(reasons) + "\nEditing it here would edit a stale copy. Bring the checkout up to date first "
          "(on main: git merge --ff-only origin/main; on a branch: merge origin/main), then edit.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
