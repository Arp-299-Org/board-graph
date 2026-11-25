#!/usr/bin/env python3
"""Run the board checks on this checkout's commit and keep the result on the graph, without GitHub Actions.

The Board checks card passes only on a validate run's result that the graph holds for the commit. A project with
a workflow on its KiCad machine's runner gets that from CI: the workflow runs scripts/validate_board.py and then
`board_graph.py checks`. A project without one runs this instead, on a machine that has KiCad and PowerShell 7
and reaches the graph's database (the KiCad machine itself), in a checkout of the commit to check:

  python3 scripts/run_board_checks.py [--board <name>] [--base <ref>] [--out <folder>]
                                      [--board-description <path>]

In a repository with several boards (scripts/boards.py), --board names the one to check: its description goes to
the checks and its result to its own database, and the reports go to a folder with its name.

It does what the workflow does, in the same order, and holds the checkout to what a workflow's fresh checkout
gives:

  1. It refuses a checkout that is not exactly its commit: a changed file, or a file git neither tracks nor
     ignores. The result is kept under the commit's name, so it has to be that commit that was checked.
  2. It runs scripts/validate_board.py, whose reports go to --out: by default a folder outside the checkout,
     board-graph-checks/<commit> in the system's temporary folder, so the run leaves the checkout as it was.
  3. It looks at the checkout again and keeps nothing when the run changed it.
  4. It keeps the run's summary.json on the graph, as `board_graph.py checks` does for a workflow: the run is
     named local-<time>, its event is "local". A run that died before its summary is kept as a run with no
     checks, which is a finding at the card, never a pass.

Exit 0 when every check passed and the result was kept, 1 when a check failed and the result was kept, 2 when it
was refused or the result could not be kept. `--state` names another database, for a test or a scratch run.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

SCRIPTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import tool_paths  # noqa: E402

if __name__ == "__main__":
    tool_paths.take_argument()

TOOL_ROOT = tool_paths.TOOL_ROOT
ROOT = tool_paths.project_root()
import boards  # noqa: E402
from no_window import KWARGS as NO_WINDOW  # noqa: E402

EVENT = "local"  # what `where` and the page show beside the run, where a workflow's run says push or pull_request
IGNORED_UNTRACKED_PREFIXES = ("logs/mac-keep-pace/",)


def _untracked_path(line: str) -> str | None:
    """Return the repo-relative POSIX path for an untracked (`?? `) porcelain line, or None."""
    if len(line) < 4 or line[:3] != "?? ":
        return None
    raw = line[3:].strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        raw = raw[1:-1]
    return raw.replace("\\", "/")


def git(root, *args):
    """git's output in `root`, or RuntimeError with what it said."""
    done = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, check=False, **NO_WINDOW)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(done.stderr or done.stdout).strip()}")
    return done.stdout


def standing(root):
    """(the commit checked out, each file that keeps the checkout from being exactly that commit): a changed
    file, or one git neither tracks nor ignores, as `git status --porcelain` lists them."""
    head = git(root, "rev-parse", "HEAD").strip()
    dirty = []
    for line in git(root, "status", "--porcelain", "--untracked-files=all").splitlines():
        if not line.strip():
            continue
        untracked = _untracked_path(line)
        if untracked is not None and any(untracked.startswith(prefix) for prefix in IGNORED_UNTRACKED_PREFIXES):
            continue
        dirty.append(line)
    return head, dirty


def run_id(at=None):
    """The name a local run is kept under: local-<UTC time to the second>."""
    return f"local-{(at or datetime.now(timezone.utc)).strftime('%Y%m%dT%H%M%SZ')}"


def launch(command, cwd):
    """Run one of the two scripts with no window of its own (no_window.py) and print what it said when it ends."""
    env = None
    if tool_paths.is_external() or pathlib.Path(cwd).resolve() != TOOL_ROOT.resolve():
        env = dict(os.environ)
        env[tool_paths.ENV] = str(pathlib.Path(cwd).resolve())
    done = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False, env=env, **NO_WINDOW)
    sys.stdout.write(done.stdout)
    sys.stderr.write(done.stderr)
    return done


def run(root=ROOT, base=None, out=None, description=None, state=None, call=launch, say=print, at=None, board=None):
    """The four steps the module describes, in `root`. `call` runs validate_board.py and `board_graph.py checks`
    (a test passes its own); returns the exit code. `board` names a listed board: the checks read its description
    and the result is kept in its database."""
    root = pathlib.Path(root)
    try:
        several = boards.unnamed(root)
        chosen = boards.find(board, root) if board else None
    except boards.BoardsError as error:
        say(f"refused: {error}")
        return 2
    if several and not chosen:
        say(f"refused: this repository has {len(several)} boards ({', '.join(several)}); name the one to check "
            f"with {boards.ARGUMENT}")
        return 2
    if chosen:
        description = description or chosen.description
    try:
        head, dirty = standing(root)
    except RuntimeError as error:
        say(f"refused: this checkout's commit could not be read ({error})")
        return 2
    if dirty:
        say(f"refused: this checkout is not exactly {head[:8]}, so a result kept under that commit would not be "
            f"of it. Commit, remove or ignore these, then run again:")
        for line in dirty:
            say(f"  {line}")
        return 2
    out = (pathlib.Path(out) if out else pathlib.Path(tempfile.gettempdir()) / "board-graph-checks"
           / (f"{head[:12]}-{chosen.name}" if chosen else head[:12]))
    summary = out / "summary.json"
    summary.unlink(missing_ok=True)  # only this run's summary is kept
    validate_script = (tool_paths.project_script("scripts/validate_board.py", root)
                       if tool_paths.is_external() else root / "scripts" / "validate_board.py")
    command = [sys.executable, str(validate_script), "--out", str(out)]
    command += ["--base", base] if base else []
    command += ["--board-description", str(description)] if description else []
    checked = call(command, cwd=str(root))
    try:
        head_after, dirty = standing(root)
    except RuntimeError as error:
        say(f"nothing kept: the checkout could not be read after the run ({error})")
        return 2
    if head_after != head or dirty:
        say(f"nothing kept: the checkout changed during the run (it was {head[:8]}, it is {head_after[:8]}"
            + (f" with {len(dirty)} changed or untracked file(s): {', '.join(line[3:] for line in dirty[:5])}"
               if dirty else "") + "), so the result is not of one commit. The reports are in " + str(out))
        return 2
    graph_script = (tool_paths.project_script("scripts/board_graph.py", root)
                    if tool_paths.is_external() else root / "scripts" / "board_graph.py")
    keep = [sys.executable, str(graph_script)]
    keep += [boards.ARGUMENT, chosen.name] if chosen else []
    keep += ["--state", str(state)] if state else []
    keep += ["checks", "--commit", head, "--run", run_id(at), "--event", EVENT, "--summary", str(summary)]
    kept = call(keep, cwd=str(root))
    if kept.returncode != 0:
        say(f"the result was not kept on the graph (board_graph.py checks exit {kept.returncode}); the reports are "
            f"in {out}")
        return 2
    say(f"Reports: {out}")
    return 0 if checked.returncode == 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--base", help="check only what changed since this ref, as a pull request's run does "
                                       "(e.g. origin/main); a run that skips the checks is not a pass at the card")
    parser.add_argument("--out", type=pathlib.Path, help="where the reports go; by default a folder outside the "
                                                         "checkout, in the system's temporary folder")
    boards.add_argument(parser)
    parser.add_argument("--board-description", metavar="PATH", help="the board's description, passed to the checks")
    parser.add_argument("--state", type=pathlib.Path, help="another database than the graph's, for a test or a "
                                                           "scratch run")
    args = parser.parse_args(argv)
    return run(base=args.base, out=args.out, description=args.board_description, state=args.state, board=args.board)


if __name__ == "__main__":
    sys.exit(main())
