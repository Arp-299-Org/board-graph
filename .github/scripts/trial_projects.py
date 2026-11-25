#!/usr/bin/env python3
"""Try this checkout's version of the tool in each project that uses it, before it is released.

For each project folder named, a scratch copy is made and never the project itself is touched. Every check the
project has is run on the copy as it is, then the copy takes this checkout's committed tool files (`update`, as
the project will run it; a project with a hand copy and no pin gets each tool file it already has), and every
check is run again. A check that passed before and fails after is a break this version would bring, and so is a
check this version adds that passes here and fails there. A check that fails both times does not fail because of
this version (often the machine: no KiCad, no Mac), and is listed with the last line of what it said.

The checks: the pin (`board_graph_tool.py check`), the graph (`board_graph.py check`, and `where` for every
graph), the docs (`docs_check.py`, where a project has it) and every test file under scripts/, scripts/hooks/ and
tools/kicad/.

  python3 .github/scripts/trial_projects.py <project folder> [<project folder> ...]
      [--ref HEAD]     the commit of this checkout to try
      [--only PATTERN] run only the checks whose name contains PATTERN
      [--keep]         leave the scratch copies, and say where they are

Exit 0 when no project breaks, 1 when one does, 2 when a project could not be tried at all.
Standard library only, Python 3.9.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

TOOL = pathlib.Path(__file__).resolve().parents[2]
MANIFEST = "tools/board_graph_tool.json"
TEST_FOLDERS = ("scripts", "scripts/hooks", "tools/kicad")
LEFT_OUT = shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules", ".venv", "venv")
TIMEOUT = 1800  # seconds one check may take


def environment():
    """This process's environment without the names that point a script at another board, pack or database."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("BOARD_GRAPH_") and key != "GIT_DIR"}
    env.update(PYTHONDONTWRITEBYTECODE="1", GIT_TERMINAL_PROMPT="0")
    return env


def checks(root):
    """The checks `root` has, as {name: command}: each a path under the root and its arguments."""
    root = pathlib.Path(root)
    found = {}
    for name, command in (("the pin", ["scripts/board_graph_tool.py", "check"]),
                          ("the graph", ["scripts/board_graph.py", "check"]),
                          ("where", ["scripts/board_graph.py", "where"]),
                          ("the docs", ["scripts/docs_check.py"])):
        if (root / command[0]).is_file() and (name != "the pin" or (root / MANIFEST).is_file()):
            found[name] = command
    for folder in TEST_FOLDERS:
        for test in sorted((root / folder).glob("test_*.py")):
            found[f"{folder}/{test.name}"] = [f"{folder}/{test.name}"]
    return found


def run(root, command):
    """One check in `root`: (whether it passed, the end of what it said)."""
    try:
        done = subprocess.run([sys.executable, *command], cwd=str(root), capture_output=True, env=environment(),
                              timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"no answer in {TIMEOUT} seconds"
    said = (done.stdout + done.stderr).decode("utf-8", "replace").strip().splitlines()
    return done.returncode == 0, "\n".join(said[-12:])


def run_all(root, only=None):
    return {name: run(root, command) for name, command in checks(root).items() if not only or only in name}


def git(folder, *args):
    done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True)
    if done.returncode:
        raise RuntimeError(f"git {' '.join(args)} in {folder}: {done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout


def take(copy, ref):
    """Give the scratch copy this checkout's tool files at `ref`: (how, what was said)."""
    if (copy / MANIFEST).is_file():
        done = subprocess.run([sys.executable, "scripts/board_graph_tool.py", "update", "--from", str(TOOL),
                               "--ref", ref], cwd=str(copy), capture_output=True, env=environment())
        said = (done.stdout + done.stderr).decode("utf-8", "replace").strip()
        if done.returncode:
            raise RuntimeError(f"update refused: {said.splitlines()[-1] if said else 'no words'}")
        return "update", said.splitlines()[0] if said else ""
    # A hand copy with no pin: every tool file the project already has is replaced by this version's.
    listed = json.loads(git(TOOL, "show", f"{ref}:{MANIFEST}").decode("utf-8"))["files"]
    replaced = 0
    for path in sorted(listed):
        if (copy / path).is_file():
            (copy / path).write_bytes(git(TOOL, "show", f"{ref}:{path}"))
            replaced += 1
    if not replaced:
        raise RuntimeError("it holds none of the tool's files")
    return "hand copy", f"no pin here: {replaced} tool files it already had were replaced"


def trial(project, ref, only=None, keep=False, out=sys.stdout):
    """Try `ref` in a scratch copy of `project`: the names of the checks it breaks."""
    project = pathlib.Path(project).resolve()
    scratch = pathlib.Path(tempfile.mkdtemp(prefix=f"trial-{project.name}-"))
    copy = scratch / project.name
    try:
        shutil.copytree(project, copy, symlinks=True, ignore=LEFT_OUT)
        before = run_all(copy, only)
        how, said = take(copy, ref)
        out.write(f"{project.name}: {said}\n")
        after = run_all(copy, only)
        here = {}  # a check this version adds, as it runs in this checkout: what it should do there
        broken, same, fixed = [], [], []
        for name, (passed, words) in after.items():
            if passed:
                if name in before and not before[name][0]:
                    fixed.append(name)
                continue
            if name in before:
                was = before[name][0]
            else:
                command = checks(TOOL).get(name)
                here[name] = run(TOOL, command)[0] if command else False
                was = here[name]
            (broken if was else same).append(name)
            if was:
                out.write(f"  BROKEN {name}: passed before, fails with this version\n")
                out.write("".join(f"    {line}\n" for line in words.splitlines()))
        gone = sorted(name for name in before if name not in after and before[name][0]
                      and not name.endswith(".py"))
        for name in gone:
            broken.append(name)
            out.write(f"  BROKEN {name}: ran before, is not there with this version\n")
        passed = sum(1 for ok, _ in after.values() if ok)
        out.write(f"  {passed} of {len(after)} checks pass with this version; {len(broken)} broken"
                  + (f"; {len(fixed)} that failed before now pass ({', '.join(fixed)})" if fixed else "")
                  + (f"; {len(same)} fail before and after, so not because of this version ({', '.join(same)})"
                     if same else "") + "\n")
        for name in same:
            last = (after[name][1].splitlines() or ["it said nothing"])[-1]
            out.write(f"  fails before and after: {name}: {last.strip()[:300]}\n")
        if keep:
            out.write(f"  the scratch copy is kept: {copy}\n")
        return broken
    finally:
        if not keep:
            shutil.rmtree(scratch, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("projects", nargs="+", metavar="PROJECT", help="a project's folder")
    parser.add_argument("--ref", default="HEAD", help="the commit of this checkout to try; HEAD by default")
    parser.add_argument("--only", metavar="PATTERN", help="run only the checks whose name contains this")
    parser.add_argument("--keep", action="store_true", help="leave the scratch copies")
    args = parser.parse_args(argv)
    code = 0
    for project in args.projects:
        if not pathlib.Path(project).is_dir():
            print(f"{project}: no such folder, so it was not tried")
            code = max(code, 2)
            continue
        try:
            broken = trial(project, args.ref, args.only, args.keep)
        except (RuntimeError, OSError) as error:
            print(f"{pathlib.Path(project).name}: could not be tried: {error}")
            code = max(code, 2)
            continue
        if broken:
            code = max(code, 1)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
