#!/usr/bin/env python3
"""The Routing card's tool: a folder of routing tasks run in the Mac's PCB Editor, the board adopted and the outcome
recorded on the graph, in one command.

  py -3 scripts/routing_run.py TASKS --by agent:NAME [--attempt ID] [--minutes 6] [--model M] [--effort E]
      [--provider claude|codex|gemini] [--review-provider claude|codex|gemini] [--review-model M]
      [--agent CMD] [--mac-tree PATH] [--mac-steps PATH] [--state DB --records FOLDER]

Run on Windows from the board branch's checkout, once each connection is written as a text file in TASKS (taken in
name order, one locked step of scripts/editor_step.py each) and committed. In order:

1. Refuses unless this checkout is on a branch other than main, clean, and level with that branch on GitHub, and TASKS
   is a folder of committed files inside it. A branch behind GitHub's main gets main merged in and pushed; a merge that
   does not go cleanly is undone and refused.
2. Starts the Routing card's attempt with the engine's next (its upkeep included), or takes the attempt --attempt names,
   or the one already open by --by.
3. On the Mac: makes the KiCad Python environment the step runner needs when the main checkout has none
   (scripts/setup_kicad_ipc.ps1), and refuses before anything runs when that fails; brings the branch's worktree (--mac-tree, the site settings' worktree prefix and the last part of the
   branch by default) level, creating it if missing, copies its board folder (the description's folder, without
   reports, production and exports) to a fresh folder run-<attempt> under --mac-steps (the site settings' steps folder
   by default), and launches a detached wrapper there: it takes the shared editor (mkdir of the site settings' editor
   lock, waiting while another holds it), runs the verified branch's step runner, and gives the editor back
   however it ends. The run survives a dropped SSH link; running this command again with the attempt picks it up.
4. Waits, then brings back the runner's step lines and the board file as it left it (a step not kept is already put
   back, so this is the board after the last kept step).
5. Adopts that board only when nothing but tracks, arcs and vias changed against the commit the run started from; zone
   fills are redrawn by every refill and do not count.
6. Stages the reviewed board and evidence in a private worktree. The graph checks and records that revision first.
7. Fast-forwards the active branch and pushes only after the graph accepts the evidence. A retry resumes the
   retained delivery. The run records more_work when all steps were kept and needs_lead otherwise.
8. Tells the Mac's worktree of the branch to pull.

--state and --records point the engine at another database and records folder, for a scratch run; with --state the
engine's upkeep (tidy) is not run. --agent passes a stand-in to the runner, a shell command with the task in $TASK,
for a run that uses no tokens. Exits 0 when more_work was recorded, 2 when needs_lead was, 1 when nothing was.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import math
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
import time
import zipfile

import tool_paths

if __name__ == "__main__":
    tool_paths.take_argument()

import boards  # noqa: E402
import routing_evidence  # noqa: E402
import routing_delivery  # noqa: E402

if __name__ == "__main__":
    boards.take_argument()  # --board <name> chooses the board before the modules below read its description

import board_description  # noqa: E402
import board_graph  # noqa: E402
import board_graph_tool  # noqa: E402
import mac  # noqa: E402
import pad_exits  # noqa: E402
import routing_codex  # noqa: E402
from kicad_sexp import child, normalize_angles, parse, prop  # noqa: E402
from no_window import KWARGS as NO_WINDOW  # noqa: E402
from routing_guard import (  # noqa: E402
    COPPER,
    FILLS,
    HEADER,
    Refused,
    beyond_copper,
    frozen,
    guard,
    is_copper_pour,
    label,
)
from routing_report import (  # noqa: E402
    STEP_FIELDS,
    SUMMARY_FIELDS,
    TOKEN_KINDS,
    _collect_extra_tools,
    _collect_profile,
    checks_span,
    evidence_lines,
    format_profile,
    note,
    outcome,
    results,
    run_state,
    span,
    tokens,
)

TOOL_ROOT = tool_paths.TOOL_ROOT
ROOT = tool_paths.project_root()
NODE = "routing_batch"
DESCRIPTION = board_description.load()
BOARD = DESCRIPTION.rel("pcb")  # the board, relative to the checkout and to a run's folder alike
FOLDER = DESCRIPTION.rel("folder")
LOCK = mac.SITE.editor_lock  # the shared editor's lock on the Mac, from the site settings
STEPS = mac.SITE.steps
# What the run's copy of the board folder leaves out: the reports, production and exports folders inside it.
_EXPORTS = pathlib.PurePosixPath(DESCRIPTION.inside_folder("bom_export") or ".").parent.as_posix()
LEFT_OUT = tuple(dict.fromkeys(part for part in (DESCRIPTION.inside_folder("reports"),
                                                 DESCRIPTION.inside_folder("production"), _EXPORTS)
                               if part and part != "."))
POLL = 30
LOCAL_POLL = 2
QUIET = 20  # polls the Mac may miss in a row before the wait gives up (the run there goes on)


def poll_seconds() -> float:
    """Use fast local polling when running on one machine; keep 30s interval over SSH."""
    return min(POLL, LOCAL_POLL) if mac.ONE_MACHINE else POLL


# Here: git, the engine.

def git_in(folder, *args, check=True, timeout=180) -> str:
    done = subprocess.run(["git", *args], cwd=folder, capture_output=True, text=True, encoding="utf-8",
                          timeout=timeout, **NO_WINDOW)
    if check and done.returncode:
        raise Refused(f"git {' '.join(args)}: {(done.stderr or done.stdout).strip()}")
    return done.stdout


def git(*args, check=True, timeout=180) -> str:
    return git_in(ROOT, *args, check=check, timeout=timeout)


def preflight(tasks: pathlib.Path) -> tuple:
    """(branch, the task folder's path in the checkout) or Refused."""
    branch = git("symbolic-ref", "--short", "-q", "HEAD", check=False).strip()
    if not branch or branch == "main":
        raise Refused("run this from a board branch's checkout, not main and not a detached head")
    if git("status", "--porcelain"):
        raise Refused("the checkout has uncommitted or untracked files: commit the task files and anything else first")
    git("fetch", "-q", "origin")
    if not git("rev-parse", "-q", "--verify", f"origin/{branch}", check=False).strip():
        raise Refused(f"{branch} is not on GitHub: push it first")
    ahead, behind = git("rev-list", "--left-right", "--count", f"HEAD...origin/{branch}").split()
    if ahead != "0" or behind != "0":
        raise Refused(f"{branch} here is {ahead} ahead and {behind} behind GitHub's: push or pull first")
    tasks = tasks.resolve()
    root = ROOT.resolve()
    try:
        rel = tasks.relative_to(root).as_posix()
    except ValueError:
        raise Refused(f"{tasks} is not inside this checkout") from None
    if not tasks.is_dir():
        raise Refused(f"{rel} is not a folder")
    files = sorted(path for path in tasks.iterdir() if path.is_file())
    if not files:
        raise Refused(f"{rel} has no task files")
    tracked = set(git("ls-files", "--", rel).splitlines())
    loose = [path.name for path in files if path.relative_to(root).as_posix() not in tracked]
    if loose:
        raise Refused(f"task files not in git (ignored?): {', '.join(loose)}")
    return branch, rel


def check_tasks(tasks: pathlib.Path, board_path: pathlib.Path | None = None) -> int:
    """Validate every task file in `tasks` against the saved board before starting KiCad or an attempt."""
    tasks = pathlib.Path(tasks)
    if not tasks.is_dir():
        raise Refused(f"{tasks} is not a folder")
    files = sorted(path for path in tasks.iterdir() if path.is_file())
    if not files:
        raise Refused(f"{tasks} has no task files")
    board = pathlib.Path(board_path) if board_path is not None else (ROOT / BOARD)
    if not board.is_file():
        raise Refused(f"board file not found: {board}")
    items = [(path.name, path.read_text(encoding="utf-8")) for path in files]
    try:
        problems = pad_exits.preflight_tasks(board.read_text(encoding="utf-8"), items)
    except ValueError as error:
        raise Refused(f"could not read {board}: {error}") from None
    if problems:
        raise Refused("task preflight failed:\n  " + "\n  ".join(problems))
    return len(files)


def level_with_main(branch: str) -> None:
    """Merge GitHub's main into the branch when it is behind, and push: next refuses a checkout behind main. A merge
    that does not go cleanly is undone."""
    if git("rev-list", "--count", "HEAD..origin/main").strip() != "0":
        merged = subprocess.run(["git", "merge", "--no-edit", "-q", "origin/main"], cwd=ROOT, capture_output=True,
                                text=True, **NO_WINDOW)
        if merged.returncode:
            git("merge", "--abort", check=False)
            raise Refused(f"{branch} is behind GitHub's main and main does not merge in cleanly: "
                          f"{(merged.stderr or merged.stdout).strip()[-300:]}")
        git("push", "-q", "origin", branch)
        print(f"merged GitHub's main into {branch} and pushed", flush=True)


def engine(args):
    graph = board_graph.Graph(args.graph)
    where_from = board_graph.place(args.state, args.records, None, root=ROOT)
    state = board_graph.open_state(where_from, args.records or ROOT / board_graph.RECORDS_IN)
    return graph, state, where_from


def open_attempts(state, by=None) -> list:
    return [row for row in state.attempts(NODE, open_only=True) if by is None or row["actor"] == by]


def start(args, graph, state, where_from, note_text, branch) -> str:
    """The attempt this run is: --attempt, the one already open by --by, or a new one from next, on a branch first
    brought level with GitHub's main."""
    if args.attempt:
        if not any(row["run_id"] == args.attempt for row in open_attempts(state, args.by)):
            raise Refused(f"attempt {args.attempt} is not open for {args.by} on the Routing card")
        return args.attempt
    mine = open_attempts(state, args.by)
    if mine:
        print(f"picking up the attempt already open by {args.by}: {mine[-1]['run_id']}")
        return mine[-1]["run_id"]
    if state.position(graph, "board") != NODE:
        raise Refused(f"the board track stands at {state.position(graph, 'board')}, not the Routing card")
    out, err = io.StringIO(), io.StringIO()
    scratch = where_from["how"] in ("--state", board_graph.STATE_ENV)
    code = board_graph.next_step(
        graph, state, "board", root=ROOT, by=args.by, note=note_text, out=out, err=err,
        upkeep=(lambda *a: True) if scratch else None,
        after_record=lambda item, extra: board_graph.carry(graph, state, item, where_from, extra, root=ROOT),
        after_report=lambda name, message: board_graph.carry_report(where_from, name, message, root=ROOT))
    said = out.getvalue().splitlines()
    print("next: " + "\n  ".join(said[-2:]) + ("\n  (scratch state: the engine's upkeep was not run)" if scratch else ""))
    if err.getvalue():
        print(err.getvalue(), file=sys.stderr, end="")
    mine = open_attempts(state, args.by)
    if code != 2 or not mine:
        raise Refused("next did not start the Routing card (above)")
    return mine[-1]["run_id"]


# The Mac.

def run_folder(args, attempt) -> str:
    return f"{args.mac_steps.rstrip('/')}/run-{attempt}"


def sh(word: str) -> str:
    """A word for the Mac's shell: quoted, with a leading ~/ still the Mac user's home."""
    if word.startswith("~/") and not any(c in word for c in '"$`\\'):
        return f'"$HOME/{word[2:]}"'
    return shlex.quote(word)


def _external_tool_mode() -> bool:
    return tool_paths.is_external() or bool(mac.TOOL_CHECKOUT)


def wrapper(args, attempt, tree, rel, commit, tool_commit=None) -> str:
    """The detached script on the KiCad machine: take the editor, run the steps, give the editor back however it ends."""
    run = run_folder(args, attempt)
    external = _external_tool_mode()
    if external:
        tool_tree = mac.TOOL_CHECKOUT or (TOOL_ROOT.as_posix() if mac.ONE_MACHINE else None)
        if not tool_tree:
            raise Refused("external tool mode requires kicad_machine.tool_checkout in tools/board_graph_site.json")
        default_py = f"{tool_tree}/.venv-kicad-ipc/bin/python"
        runner_args = [f"{tool_tree}/scripts/editor_step.py", f"{run}/{BOARD}", "--tasks", f"{tree}/{rel}",
                       "--minutes", str(args.minutes)]
    else:
        default_py = f"{mac.CHECKOUT}/.venv-kicad-ipc/bin/python"
        runner_args = ["scripts/editor_step.py", f"{run}/{BOARD}", "--tasks", f"{tree}/{rel}",
                       "--minutes", str(args.minutes)]
    # A board chosen here (--board, or BOARD_GRAPH_BOARD) is the board the steps on the KiCad machine read too.
    board = (f"export {board_description.ENV}={sh(boards.relative(os.environ[board_description.ENV], ROOT))}\n"
             if os.environ.get(board_description.ENV) else "")
    cap = getattr(args, "batch_minutes", None)
    budget = f"export BOARD_GRAPH_ROUTE_DEADLINE=$(( $(date +%s) + {cap * 60} ))" if cap else ""
    for flag, value in (("--provider", args.provider), ("--review-provider", getattr(args, "review_provider", None)),
                        ("--model", args.model), ("--effort", args.effort), ("--review-model", args.review_model),
                        ("--agent", args.agent)):
        if value:
            runner_args += [flag, value]
    ipc_line = f'  default_ipc_py={sh(default_py)}\n  ipc_python="${{KICAD_IPC_PYTHON:-$default_ipc_py}}"\n'
    runner_cmd = '"$ipc_python" ' + " ".join(sh(word) for word in runner_args)
    if external:
        dirty_args = tool_paths.dirty_status_shell_args()
        commit_line = (f'  [ "$(git -C "$tool_tree" rev-parse HEAD)" = {sh(tool_commit)} ] || '
                       f'{{ echo "KiCad tool checkout changed while waiting for the editor" >&2; exit 3; }}\n'
                       f'  [ -z "$(git -C "$tool_tree" status --porcelain {dirty_args})" ] || '
                       f'{{ echo "KiCad tool checkout changed while waiting for the editor" >&2; exit 3; }}\n'
                       if tool_commit else "")
        body = (f'  export {tool_paths.ENV}="$PWD"\n'
                f'  [ "$(git rev-parse HEAD)" = {sh(commit)} ] && [ -z "$(git status --porcelain)" ] || '
                f'{{ echo "KiCad worktree changed while waiting for the editor" >&2; exit 3; }}\n'
                f'  tool_tree={sh(tool_tree)}\n'
                f'{commit_line}'
                f'  (cd "$tool_tree" && python3 -B scripts/board_graph_tool.py check) >&2\n'
                f'{ipc_line}'
                f'  {runner_cmd}')
    else:
        body = (f'  [ "$(git rev-parse HEAD)" = {sh(commit)} ] && [ -z "$(git status --porcelain)" ] || '
                f'{{ echo "KiCad worktree changed while waiting for the editor" >&2; exit 3; }}\n'
                f'  python3 -B scripts/board_graph_tool.py check >&2\n'
                f'{ipc_line}'
                f'  {runner_cmd}')
    return f"""#!/bin/sh
run={sh(run)}
lock={sh(LOCK)}
holder="$lock/routing-run-{attempt}"
{budget}
echo waiting > "$run/phase"
until mkdir "$lock" 2>/dev/null; do
  for f in "$lock"/routing-run-*; do
    [ -e "$f" ] || continue
    hpid=$(sed -n 's/^pid=//p' "$f" 2>/dev/null | head -n 1)
    if [ -n "$hpid" ]; then
      if ! kill -0 "$hpid" 2>/dev/null; then
        echo "reclaimed stale lock $f (dead pid $hpid)" >&2
        rm -f "$f"
      fi
    elif ! pgrep -f 'editor_step\\.py|/run\\.sh' >/dev/null 2>&1; then
      echo "reclaimed stale empty lock $f" >&2
      rm -f "$f"
    fi
  done
  rmdir "$lock" 2>/dev/null && continue
  if [ -n "$BOARD_GRAPH_ROUTE_DEADLINE" ] && [ "$(date +%s)" -ge "$BOARD_GRAPH_ROUTE_DEADLINE" ]; then
    echo "Card deadline expired waiting for the editor" > "$run/err.log"
    : > "$run/out.jsonl"
    echo 2 > "$run/exit"; echo done > "$run/phase"; exit 2
  fi
  sleep 1
done
printf 'pid=%s\\nstarted=%s\\n' "$$" "$(date +%s)" > "$holder"
trap 'rm -f "$holder"; rmdir "$lock" 2>/dev/null' EXIT
trap '' HUP
trap 'echo stopped > "$run/phase"; exit 143' INT TERM
echo running > "$run/phase"
export BOARD_GRAPH_STATUS_FILE="$run/status"
{board}(
  set -e
  cd {sh(tree)}
{body}
) > "$run/out.jsonl" 2> "$run/err.log"
echo $? > "$run/exit"
echo done > "$run/phase"
"""


def mac_bash(script: str, timeout=300) -> str:
    try:
        return mac.bash(script, timeout=timeout)
    except mac.MacError as error:
        host = "on this machine" if mac.ONE_MACHINE else "on the Mac"
        raise Refused(f"{host}: {error}") from None


def _external_tool_sync_checks(manifest: dict, tool_commit: str | None, blob: str, manifest_sha: str | None) -> str:
    """Build shell checks keeping the KiCad machine's shared tool checkout in sync with desktop."""
    dirty_args = tool_paths.dirty_status_shell_args()
    if tool_commit:
        return (
            f'[ -z "$(git -C "$tool_tree" status --porcelain {dirty_args})" ] || {{ echo "$tool_tree has uncommitted changes" >&2; exit 3; }}\n'
            f'if [ "$(git -C "$tool_tree" rev-parse HEAD)" != {sh(tool_commit)} ]; then\n'
            f'  git -C "$tool_tree" fetch -q origin\n'
            f'  git -C "$tool_tree" merge -q --ff-only {sh(tool_commit)} 2>/dev/null || git -C "$tool_tree" checkout -q --detach {sh(tool_commit)}\n'
            f'fi\n'
            f'[ "$(git -C "$tool_tree" rev-parse HEAD)" = {sh(tool_commit)} ] || {{ echo "$tool_tree is at $(git -C "$tool_tree" rev-parse HEAD), not {tool_commit} (push desktop board-graph commit first)" >&2; exit 3; }}\n'
            f'[ "$(git -C "$tool_tree" hash-object -- {board_graph_tool.MANIFEST})" = {sh(blob)} ] || {{ echo "Mac and desktop tool manifests differ" >&2; exit 3; }}\n'
        )
    if mac.ONE_MACHINE or not manifest_sha:
        return ""
    tag = f"v{manifest['version']}"
    digest_py = (
        f"import hashlib,sys; "
        f"d=hashlib.sha256(open({board_graph_tool.MANIFEST!r},'rb').read().replace(b'\\r\\n',b'\\n')).hexdigest(); "
        f"sys.exit(0 if d=={manifest_sha!r} else 1)"
    )
    return (
        f'[ -z "$(git -C "$tool_tree" status --porcelain {dirty_args})" ] || {{ echo "$tool_tree has uncommitted changes" >&2; exit 3; }}\n'
        f'if ! (cd "$tool_tree" && python3 -c {sh(digest_py)}); then\n'
        f'  git -C "$tool_tree" fetch -q --tags origin\n'
        f'  git -C "$tool_tree" checkout -q --detach {sh(tag)} 2>/dev/null || git -C "$tool_tree" checkout -q {sh(tag)}\n'
        f'fi\n'
        f'(cd "$tool_tree" && python3 -c {sh(digest_py)}) || {{ echo "$tool_tree manifest differs from installed {tag}" >&2; exit 3; }}\n'
    )


def _external_tool_prepare(branch: str, commit: str, tree: str, lock_guard: str) -> dict:
    """Validate desktop tool state and sync worktree + shared KiCad tool checkout before routing."""
    tool_checkout = mac.TOOL_CHECKOUT or (TOOL_ROOT.as_posix() if mac.ONE_MACHINE else None)
    if not tool_checkout:
        raise Refused("external tool mode requires kicad_machine.tool_checkout in tools/board_graph_site.json")
    manifest = board_graph_tool.load(TOOL_ROOT)
    differences = board_graph_tool.differences(TOOL_ROOT, manifest)
    if differences:
        raise Refused("desktop tool differs from its manifest: " + ", ".join(differences))
    if tool_paths.is_git_checkout(TOOL_ROOT):
        dirty = tool_paths.tool_dirty(TOOL_ROOT)
        if dirty:
            raise Refused("desktop tool checkout has uncommitted changes: " + ", ".join(dirty[:5]))
        tool_commit = tool_paths.tool_commit(TOOL_ROOT)
        if not tool_commit and not mac.ONE_MACHINE:
            raise Refused(f"could not determine git HEAD of desktop tool checkout {TOOL_ROOT}")
        blob = git_in(TOOL_ROOT, "rev-parse", f"{tool_commit}:{board_graph_tool.MANIFEST}").strip() if tool_commit else ""
        manifest_sha = None
    else:
        tool_commit = None
        blob = ""
        manifest_sha = board_graph_tool.digest((TOOL_ROOT / board_graph_tool.MANIFEST).read_bytes())
    tool_git_checks = _external_tool_sync_checks(manifest, tool_commit, blob, manifest_sha)
    mac_bash(f"""set -e
{lock_guard}
tree={sh(tree)}; branch={shlex.quote(branch)}
if [ ! -d "$tree" ]; then
  cd {sh(mac.CHECKOUT)}
  git fetch -q origin "$branch"
  if git rev-parse -q --verify "refs/heads/$branch" >/dev/null; then git worktree add -q "$tree" "$branch"
  else git worktree add -q -b "$branch" "$tree" "origin/$branch"; fi
  echo "made the worktree $tree"
fi
cd "$tree"
[ "$(git symbolic-ref --short -q HEAD)" = "$branch" ] || {{ echo "$tree is not on $branch" >&2; exit 3; }}
[ -z "$(git status --porcelain)" ] || {{ echo "$tree has uncommitted changes" >&2; exit 3; }}
git fetch -q origin "$branch"
git merge -q --ff-only "origin/$branch"
[ "$(git rev-parse HEAD)" = {commit} ] || {{ echo "$tree is at $(git rev-parse HEAD), not {commit}" >&2; exit 3; }}
tool_tree={sh(tool_checkout)}
[ -d "$tool_tree" ] || {{ echo "KiCad tool checkout $tool_tree does not exist" >&2; exit 3; }}
{tool_git_checks}(cd "$tool_tree" && python3 -B scripts/board_graph_tool.py check)
ipc_py="${{KICAD_IPC_PYTHON:-$tool_tree/.venv-kicad-ipc/bin/python}}"
if [ ! -x "$ipc_py" ]; then
  echo "making the KiCad Python environment in $tool_tree (scripts/setup_kicad_ipc.ps1)"
  (cd "$tool_tree" && pwsh -NoProfile -File scripts/setup_kicad_ipc.ps1) >&2 || {{ echo "$tool_tree has no KiCad Python environment (.venv-kicad-ipc) and scripts/setup_kicad_ipc.ps1 could not make it, so the step runner cannot start: nothing was run" >&2; exit 3; }}
fi
""")
    result = {"version": manifest["version"], "tool_commit": tool_commit, "runner_commit": commit, "external_tool": True}
    if tool_commit:
        result["manifest_blob"] = blob
    if manifest_sha:
        result["manifest_sha"] = manifest_sha
    return result


def prepare_mac(branch, commit, tree):
    lock_guard = 'if [ "$(uname -s)" = "Darwin" ]; then [ "$(ioreg -n Root -d1 -a | grep -c CGSSessionScreenIsLocked)" = 0 ] || { echo "the Mac\'s screen is locked" >&2; exit 3; }; fi'
    if not _external_tool_mode():
        manifest = board_graph_tool.load(ROOT)
        differences = board_graph_tool.differences(ROOT, manifest)
        if differences:
            raise Refused("desktop tool differs from its manifest: " + ", ".join(differences))
        blob = git("rev-parse", f"{commit}:{board_graph_tool.MANIFEST}").strip()
        mac_bash(f"""set -e
{lock_guard}
tree={sh(tree)}; branch={shlex.quote(branch)}
if [ ! -d "$tree" ]; then
  cd {sh(mac.CHECKOUT)}
  git fetch -q origin "$branch"
  if git rev-parse -q --verify "refs/heads/$branch" >/dev/null; then git worktree add -q "$tree" "$branch"
  else git worktree add -q -b "$branch" "$tree" "origin/$branch"; fi
  echo "made the worktree $tree"
fi
cd "$tree"
[ "$(git symbolic-ref --short -q HEAD)" = "$branch" ] || {{ echo "$tree is not on $branch" >&2; exit 3; }}
[ -z "$(git status --porcelain)" ] || {{ echo "$tree has uncommitted changes" >&2; exit 3; }}
git fetch -q origin "$branch"
git merge -q --ff-only "origin/$branch"
[ "$(git rev-parse HEAD)" = {commit} ] || {{ echo "$tree is at $(git rev-parse HEAD), not {commit}" >&2; exit 3; }}
[ "$(git hash-object -- {board_graph_tool.MANIFEST})" = {sh(blob)} ] || {{ echo "Mac and desktop tool manifests differ" >&2; exit 3; }}
python3 -B scripts/board_graph_tool.py check
main={sh(mac.CHECKOUT)}
ipc_py="${{KICAD_IPC_PYTHON:-$main/.venv-kicad-ipc/bin/python}}"
if [ ! -x "$ipc_py" ]; then
  echo "making the KiCad Python environment in $main (scripts/setup_kicad_ipc.ps1)"
  (cd "$main" && pwsh -NoProfile -File scripts/setup_kicad_ipc.ps1) >&2 || {{ echo "$main has no KiCad Python environment (.venv-kicad-ipc) and scripts/setup_kicad_ipc.ps1 could not make it, so the step runner cannot start: nothing was run" >&2; exit 3; }}
fi
""")
        return {"version": manifest["version"], "manifest_blob": blob, "runner_commit": commit}
    return _external_tool_prepare(branch, commit, tree, lock_guard)


def launch(args, attempt, branch, commit, tree, rel, tool) -> None:
    run = run_folder(args, attempt)
    excludes = " ".join(f"--exclude /{name}" for name in LEFT_OUT)
    meta = {"attempt": attempt, "branch": branch, "commit": commit, "tasks": rel, "provider": args.provider,
            "review_provider": getattr(args, "review_provider", None) or args.provider,
            "model": args.model, "review_model": args.review_model, "tool": tool}
    mac_bash(f"""set -e
tree={sh(tree)}
run={sh(run)}
[ ! -e "$run" ] || {{ echo "$run already exists" >&2; exit 3; }}
mkdir -p "$run"
rsync -a {excludes} "$tree/{FOLDER}/" "$run/{FOLDER}/"
printf '%s\\n' {shlex.quote(json.dumps(meta))} > "$run/run.json"
cat > "$run/run.sh" <<'WRAPPER'
{wrapper(args, attempt, tree, rel, commit, tool_commit=(tool or {}).get("tool_commit"))}WRAPPER
cd "$run"
/usr/bin/python3 -c 'import subprocess, sys; p = subprocess.Popen(["/bin/sh", "run.sh"], stdin=subprocess.DEVNULL, stdout=open("wrapper.log", "ab"), stderr=subprocess.STDOUT, start_new_session=True); open("pid", "w").write(str(p.pid))'
echo "launched in $run, pid $(cat pid)"
""")


STATUS = """run={run}; lock={lock}
[ -d "$run" ] || {{ echo absent; exit 0; }}
pid=$(cat "$run/pid" 2>/dev/null); alive=0
[ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && alive=1
detail=$(tr '\\n' ' ' < "$run/status" 2>/dev/null | sed 's/  */ /g; s/^ //; s/ $//' || true)
printf 'phase=%s\\nexit=%s\\nalive=%s\\nholder=%s\\nsteps=%s\\ndetail=%s\\n' "$(cat "$run/phase" 2>/dev/null)" \\
  "$(cat "$run/exit" 2>/dev/null)" "$alive" "$(ls "$lock" 2>/dev/null | tr '\\n' ' ')" \\
  "$(grep -c '"task"' "$run/out.jsonl" 2>/dev/null || echo 0)" "$detail"
"""


def status(args, attempt) -> dict:
    return run_state(mac.bash(STATUS.format(run=sh(run_folder(args, attempt)), lock=sh(LOCK)), timeout=60))


def wait(args, attempt) -> dict:
    """Until the run on the Mac has finished or died. A Mac that does not answer is waited out QUIET polls in a row."""
    interval = poll_seconds()
    missed, last = 0, None
    last_print = time.time()
    while True:
        try:
            found = status(args, attempt)
            missed = 0
        except mac.MacError as error:
            missed += 1
            if missed >= QUIET:
                raise Refused(f"the Mac has not answered for {int(missed * interval // 60)} minutes ({error}); the run there "
                              f"goes on: run this again with --attempt {attempt} to pick it up") from None
            time.sleep(interval)
            continue
        detail = found.get("detail", "")
        seen = (found["state"], found.get("steps"), detail)
        now = time.time()
        if seen != last or (now - last_print) >= 60:
            extra = f", held by {found['holder']}" if found["state"] == "waiting" and found.get("holder") else ""
            status_note = f" ({detail})" if detail else ""
            print(f"{time.strftime('%H:%M:%S')} {found['state']}{extra}{status_note}, {found.get('steps', 0)} step(s) done", flush=True)
            last = seen
            last_print = now
        if found["state"] in ("finished", "died", "absent"):
            return found
        time.sleep(interval)


def fetch(path: str) -> bytes:
    done = mac.ssh(f"cat {sh(path)}", timeout=300)
    if done.returncode:
        raise Refused(f"could not read {path} on the Mac: {mac.text(done.stderr).strip()}")
    return done.stdout


def collect_candidates(steps, run, receipt, root=None):
    root = ROOT if root is None else root
    pending = []
    for number, step in enumerate(steps, 1):
        archives = collections.defaultdict(list)
        for entry in [step, *(step.get("warning_reviews") or [])]:
            candidate = entry.get("candidate")
            if candidate:
                archives[candidate.get("archive", "candidate.zip")].append(candidate)
        for archive, refs in archives.items():
            match = re.fullmatch(r"candidate(-round[1-9][0-9]*)?\.zip", archive)
            folder = refs[0]["step_folder"]
            if not match or not re.fullmatch(r"step-[0-9-]+", folder):
                raise Refused(f"Invalid Mac archive path for candidate {number}")
            board_parent = pathlib.PurePosixPath(BOARD).parent
            data = fetch(f"{run}/{board_parent}/{folder}/{archive}")
            digest = hashlib.sha256(data).hexdigest()
            if any(ref["step_folder"] != folder or ref["sha256"] != digest for ref in refs):
                raise Refused(f"Candidate archive {number} differs from the Mac receipt")
            path = receipt.with_name(f"{receipt.stem}-candidate-{number}{match[1] or ''}.zip")
            name = path.relative_to(root).as_posix()
            if path.is_file() and path.read_bytes() != data:
                raise Refused(f"Another archive already exists at {name}")
            pending.append((refs, name, path, data))
    for refs, name, path, data in pending:
        path.write_bytes(data)
        for ref in refs:
            ref["evidence"] = name
    return [name for _, name, _, _ in pending]


def preserve_download(receipt, board, digest, root=None):
    """Keep the unadopted PCB as evidence. Replaying a receipt keeps the same archive."""
    path = receipt.with_name(f"{receipt.stem}-unadopted-{digest}.zip")
    name = path.relative_to(ROOT if root is None else root).as_posix()
    with tempfile.TemporaryDirectory(dir=receipt.parent) as folder:
        staged = pathlib.Path(folder) / "candidate.zip"
        with zipfile.ZipFile(staged, "w") as archive:
            archive.writestr(zipfile.ZipInfo("candidate.kicad_pcb"), board, zipfile.ZIP_DEFLATED)
        if path.exists():
            with zipfile.ZipFile(path) as archive:
                if archive.namelist() != ["candidate.kicad_pcb"] or archive.read("candidate.kicad_pcb") != board:
                    raise Refused(f"Another archive already exists at {name}")
        else:
            staged.replace(path)
    return {"evidence": name, "board_sha256": digest}


# The whole run.

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    boards.add_argument(parser)
    parser.add_argument("tasks", type=pathlib.Path, help="the folder of task files, in this checkout and committed")
    parser.add_argument("--by", help="who runs it: agent:<name>, or owner")
    parser.add_argument("--preflight", action="store_true",
                        help="check every task file against the saved board locally and exit without starting a run")
    parser.add_argument("--attempt", help="pick up this open attempt: a run already launched is found and waited for")
    parser.add_argument("--adopt-from",
                        help="adopt an already-finished Mac run (prior attempt id) after re-verifying every guard, "
                             "evidence hash, cold review receipt, and board/task ancestry without re-launching KiCad")
    parser.add_argument("--retain-only", action="store_true", help="archive a pending delivery locally and release an open attempt to the lead")
    parser.add_argument("--note", default="", help="what the batch is, for the attempt next opens")
    parser.add_argument("--minutes", type=float, default=6, help="operator handoff time and separate review window, per step")
    parser.add_argument("--model")
    parser.add_argument("--provider", choices=("claude", "codex", "gemini"), help="operator (and default independent reviewer) provider "
                        "(use the project setting for a new run or the recorded provider when resuming; gemini runs agy)")
    parser.add_argument("--review-provider", choices=("claude", "codex", "gemini"),
                        help="independent reviewer provider, if different from --provider")
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--review-model")
    parser.add_argument("--agent", help="a stand-in for the agent: a shell command, the task in $TASK")
    parser.add_argument("--mac-tree", help=f"the branch's worktree on the Mac; {mac.SITE.worktree_prefix}<last part "
                                           "of the branch>")
    parser.add_argument("--mac-steps", default=STEPS, help="the folder on the Mac that holds the run's folder")
    parser.add_argument("--state", type=pathlib.Path, help="another graph database, for a scratch run")
    parser.add_argument("--graph", type=pathlib.Path, default=board_graph.GRAPH, help="the originating card's graph")
    parser.add_argument("--records", type=pathlib.Path, help="the records folder that database imports and writes")
    args = parser.parse_args(argv)
    if not args.preflight and not args.by:
        parser.error("--by is required unless --preflight is given")
    if not math.isfinite(args.minutes) or args.minutes <= 0:
        parser.error("--minutes must be a positive finite number")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    try:
        return run(args)
    except (Refused, routing_delivery.DeliveryError) as refusal:
        print(f"routing_run: {refusal}", file=sys.stderr)
        return 1


def _assigned_tasks(commit: str, rel: str) -> dict:
    raw = git("ls-tree", "-z", "--name-only", f"{commit}:{rel}").strip("\0")
    names = [name for name in raw.split("\0") if name] if raw else []
    return {name: git("show", f"{commit}:{rel}/{name}") for name in names}


def _validate_adopt_from(args, state, branch: str, rel: str) -> tuple:
    adopt_from = args.adopt_from
    if open_attempts(state, args.by):
        raise Refused(f"an attempt is already open for {args.by} on the Routing card; finish or close it before --adopt-from")
    try:
        prior_found = status(args, adopt_from)
    except mac.MacError as error:
        raise Refused(f"the Mac did not answer ({error}); cannot inspect prior run {adopt_from}") from None
    if prior_found["state"] != "finished":
        raise Refused(f"prior run {adopt_from} is {prior_found['state']}, not finished")
    prior_folder = run_folder(args, adopt_from)
    try:
        meta = json.loads(fetch(f"{prior_folder}/run.json").decode("utf-8"))
    except (UnicodeError, ValueError):
        raise Refused(f"invalid run.json in {prior_folder}") from None
    if (meta.get("attempt"), meta.get("branch"), meta.get("tasks")) != (adopt_from, branch, rel):
        raise Refused(f"prior Mac run {adopt_from} belongs to a different attempt, branch or task folder")
    prior_commit = meta.get("commit")
    if not isinstance(prior_commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", prior_commit):
        raise Refused(f"invalid commit in {prior_folder}/run.json")
    provider = meta.get("provider", "claude")
    review_provider = meta.get("review_provider") or provider
    if provider not in routing_evidence.PROVIDERS or review_provider not in routing_evidence.PROVIDERS:
        raise Refused(f"prior run {adopt_from} used unsupported provider {provider!r}/{review_provider!r}")
    if getattr(args, "provider", None) and args.provider != provider:
        raise Refused(f"prior run {adopt_from} used {provider}; pass --provider {provider} or omit it")
    if getattr(args, "review_provider", None) and args.review_provider != review_provider:
        raise Refused(f"prior run {adopt_from} used review provider {review_provider}; pass --review-provider {review_provider} or omit it")
    steps, summary = results(fetch(f"{prior_folder}/out.jsonl").decode("utf-8", errors="replace"), strict=True)
    if summary is None or not any(step.get("kept") for step in steps):
        raise Refused(f"prior run {adopt_from} has no completed summary with kept steps")
    board = fetch(f"{prior_folder}/{BOARD}")
    digest = hashlib.sha256(board).hexdigest()
    level_with_main(branch)
    if git("merge-base", prior_commit, "HEAD", check=False).strip() != prior_commit:
        raise Refused(f"prior commit {prior_commit} is not an ancestor of HEAD")
    base = git("show", f"{prior_commit}:{BOARD}")
    if (ROOT / BOARD).read_bytes().replace(b"\r\n", b"\n") != base.replace("\r\n", "\n").encode("utf-8"):
        raise Refused(f"{BOARD} here changed since prior run commit {prior_commit[:8]}")
    if git("ls-tree", "-r", f"{prior_commit}:{rel}") != git("ls-tree", "-r", f"HEAD:{rel}"):
        raise Refused(f"task files in {rel} changed since prior run commit {prior_commit[:8]}")
    assigned = _assigned_tasks(prior_commit, rel)
    allow_pours = any(routing_evidence.parse_task_pours(text) for text in assigned.values())
    problem = routing_evidence.adoption_problem(steps, summary, digest, provider, assigned, review_provider=review_provider)
    if problem:
        raise Refused(f"cannot adopt run {adopt_from}: {problem}")
    changed = guard(base, board.decode("utf-8"), allow_pours=allow_pours)
    if changed:
        raise Refused(f"cannot adopt run {adopt_from}: {'; '.join(changed)}")
    return meta, prior_found, prior_folder, prior_commit, provider, review_provider, steps, summary


def _deliver_run(args, graph, state, where_from, context, branch, rel, tree, attempt, commit, folder,
                 provider, review_provider, tool, found, steps, summary, *, recovered_from=None):
    problem = ""
    if summary is None:
        err = mac.ssh(f"tail -5 {sh(folder + '/err.log')}", timeout=60).stdout.decode("utf-8", errors="replace").strip()
        problem = (f"The runner ended without a summary ({found['state']}"
                   + (f", exit {found['exit']}" if "exit" in found else "") + f"): {err.splitlines()[-1] if err else 'no output'}.")
    base = git("show", f"{commit}:{BOARD}")
    changed = []
    adopted = False
    stage = routing_delivery.workspace(ROOT)
    evidence = (stage / rel).parent / f"run-{attempt[:8]}.jsonl"
    candidates = []
    unadopted = None
    unfinished = []
    try:
        candidates = collect_candidates(steps, folder, evidence, stage)
    except (Refused, mac.MacError) as error:
        problem = f"Could not retrieve the candidate archive at {folder}: {error}"
    allow_pours = False
    if not problem and summary is not None and any(step.get("kept") for step in steps):
        board = fetch(f"{folder}/{BOARD}")
        digest = hashlib.sha256(board).hexdigest()
        assigned = _assigned_tasks(commit, rel)
        allow_pours = any(routing_evidence.parse_task_pours(text) for text in assigned.values())
        problem = routing_evidence.adoption_problem(steps, summary, digest, provider, assigned, review_provider=review_provider)
        if problem:
            problem += ". No copper adopted."
            try:
                unadopted = preserve_download(evidence, board, digest, stage)
                candidates.append(unadopted["evidence"])
            except (Refused, OSError, zipfile.BadZipFile) as error:
                problem += f" Could not preserve the download from {folder}/{BOARD}: {error}"
        elif (ROOT / BOARD).read_bytes().replace(b"\r\n", b"\n") not in (
            base.replace("\r\n", "\n").encode("utf-8"), board.replace(b"\r\n", b"\n")
        ):  # the run's board: adopted by a run before
            changed = ["the board here is neither the one the run started from nor the one it left"]
        else:
            changed = guard(base, board.decode("utf-8"), allow_pours=allow_pours)
        try:
            board_graph.check_routing_terms(board_graph.Graph(args.graph), state, NODE, ROOT, attempt)
        except ValueError as error:
            changed.append(str(error))
        if not changed and not problem:
            kept_names = {step["task"] for step in steps if step["kept"]}
            unfinished = [name for name in assigned if name not in kept_names]
            (stage / BOARD).write_bytes(board)
            adopted = True
    chosen = "needs_lead" if problem else outcome(steps, summary, changed)
    run_tokens = tokens(steps)
    extra = {"attempt": attempt, "branch": branch, "commit": commit, "task_folder": rel, "mac_folder": folder,
             "adopted": adopted, "changed_beyond_copper": changed, "outcome": chosen, "tokens": run_tokens,
             "provider": provider, "review_provider": review_provider, "tool": tool, "pcb": BOARD}
    if recovered_from:
        extra["recovered_from"] = recovered_from
    if unfinished:
        extra["unfinished_tasks"] = unfinished
    if unadopted:
        extra["unadopted_candidate"] = unadopted
    if problem:
        extra["problem"] = problem
    evidence.write_bytes(("\n".join(evidence_lines(steps, summary, extra)) + "\n").encode("utf-8"))
    name = evidence.relative_to(stage).as_posix()
    message = note(steps, summary, changed, attempt, problem, provider, unfinished, allow_pours=allow_pours)
    if recovered_from and adopted:
        message = f"Recovered from verified run {recovered_from[:8]}. " + message
    seal_tokens = 0 if recovered_from else run_tokens
    delivery = routing_delivery.seal(ROOT, stage, context, attempt, chosen, message, seal_tokens,
                                     [name, *candidates], BOARD if adopted else None)
    profile = format_profile(steps, summary)
    if profile:
        print(profile)
    return complete_delivery(args, graph, state, where_from, context, delivery, tree)


def _validate_run_args(args):
    """Validate CLI flag combinations and apply default routing profile settings."""
    try:
        several = boards.unnamed(ROOT)
    except boards.BoardsError as error:
        raise Refused(str(error)) from None
    if several:
        raise Refused(
            f"this repository has {len(several)} boards ({', '.join(several)}); name the one to route "
            f"with {boards.ARGUMENT}"
        )
    adopt_from = getattr(args, "adopt_from", None)
    if adopt_from is not None:
        if not isinstance(adopt_from, str) or not re.fullmatch(r"[0-9A-Za-z_-]{8,64}", adopt_from):
            raise Refused(f"invalid --adopt-from attempt id: {adopt_from!r}")
        if getattr(args, "retain_only", False) or getattr(args, "agent", None) or getattr(args, "attempt", None):
            raise Refused("--adopt-from cannot be combined with --retain-only, --agent, or --attempt")
    profile = DESCRIPTION.routing_agent
    for field in ("model", "review_model", "effort"):
        if not getattr(args, field, None):
            setattr(args, field, profile.get(field))
    if (
        getattr(args, "provider", None) in ("codex", "gemini")
        or getattr(args, "review_provider", None) in ("codex", "gemini")
    ) and getattr(args, "agent", None):
        raise Refused("managed providers (codex/gemini) run real operators/reviewers and cannot be combined with --agent")
    return profile, adopt_from


def _handle_retain_only(args, graph, state, where_from, context, pending, marker) -> int:
    """Retain a pending delivery under a dedicated git ref and close open attempts if needed."""
    if not pending or pending["context"] != context or args.attempt != pending["attempt"]:
        raise Refused("Retaining a delivery requires the matching actor, checkout, database and --attempt")
    ref = "refs/board-graph/retained/" + pending["commit"]
    if any(a["run_id"] == args.attempt for a in open_attempts(state, args.by)):
        reason = f"Unpublished routing candidate retained at {ref}. " + (args.note or "Delivery stopped by the lead.")
        item = board_graph.record(graph, state, NODE, "needs_lead", args.by, reason, root=ROOT, attempt_id=args.attempt)
        sys.stdout.write(board_graph.carry(graph, state, item, where_from, root=ROOT))
    completed = [r for r in state.transitions(node=NODE) if r["inputs"].get("attempt_id") == args.attempt]
    if any(routing_delivery.matches_record(pending, r) or r["outcome"] in ("pass", "more_work") for r in completed):
        raise Refused("The graph already accepted this delivery. Resume publication.")
    routing_delivery.git(ROOT, "update-ref", ref, pending["commit"])
    marker.unlink()
    print(f"Candidate retained at {ref} and {pending['stage']}")
    return 2


def _launch_absent_attempt(args, graph, state, where_from, profile, branch, commit, tree, rel, attempt, tool):
    """Validate routing terms and launch a fresh routing run on the Mac."""
    try:
        board_graph.check_routing_terms(graph, state, NODE, ROOT, attempt)
    except ValueError as error:
        item = board_graph.record(graph, state, NODE, "needs_lead", args.by, str(error), root=ROOT, attempt_id=attempt)
        sys.stdout.write(board_graph.carry(graph, state, item, where_from, root=ROOT))
        raise Refused(str(error)) from None
    args.provider = args.provider or profile.get("provider")
    if not args.provider and not args.agent:
        raise Refused("Select a routing provider with --provider or routing_agent.provider (--provider gemini uses agy)")
    provider = args.provider or "stand-in"
    review_provider = getattr(args, "review_provider", None) or profile.get("review_provider") or provider
    if not args.agent:
        args.review_provider = review_provider
    if args.agent and (args.provider in ("codex", "gemini") or review_provider in ("codex", "gemini") or not args.state):
        raise Refused("A stand-in needs a scratch --state and cannot use managed providers (codex/gemini)")
    if not tool or tool["runner_commit"] != commit:
        tool = prepare_mac(branch, commit, tree)
    args.batch_minutes = graph.stop(NODE).get("minutes")
    launch(args, attempt, branch, commit, tree, rel, tool)
    print(f"launched on the Mac in {run_folder(args, attempt)}")
    return provider, review_provider, tool


def _resume_existing_attempt(args, attempt, branch, rel):
    """Validate and return metadata for an already-started Mac routing run."""
    meta = json.loads(fetch(f"{run_folder(args, attempt)}/run.json").decode("utf-8"))
    provider = meta.get("provider", "claude")
    review_provider = meta.get("review_provider") or provider
    if args.provider and args.provider != provider:
        raise Refused(f"attempt {attempt} already uses {provider}; resume it with that provider")
    if getattr(args, "review_provider", None) and args.review_provider != review_provider:
        raise Refused(f"attempt {attempt} already uses review provider {review_provider}; resume it with that provider")
    if (meta["attempt"], meta["branch"], meta["tasks"]) != (attempt, branch, rel):
        raise Refused("the existing Mac run belongs to a different attempt, branch or task folder")
    return provider, review_provider, meta["commit"], meta.get("tool")


def routing_tree(override: str | None, branch: str) -> str:
    """Use the active checkout in one_machine mode, else the site worktree for `branch`."""
    if override:
        return override
    if mac.ONE_MACHINE:
        return str(ROOT.resolve())
    return mac.SITE.worktree(branch)


def run(args) -> int:
    if getattr(args, "preflight", False):
        count = check_tasks(args.tasks)
        print(f"preflight ok: {count} task(s) checked against {BOARD}")
        return 0
    profile, adopt_from = _validate_run_args(args)
    graph, state, where_from = engine(args)
    marker = routing_delivery.journal(ROOT)
    pending = json.loads(marker.read_bytes()) if marker.exists() else None
    if pending:
        branch = git("branch", "--show-current").strip()
        rel = pathlib.Path(args.tasks).resolve().relative_to(ROOT.resolve()).as_posix()
    else:
        branch, rel = preflight(args.tasks)
    context = dict(
        branch=branch, actor=args.by, pcb=BOARD, tasks=rel,
        database=str(where_from["path"]), records=str(state.records.resolve()),
        graph=str(pathlib.Path(args.graph).resolve()),
    )
    tree = routing_tree(args.mac_tree, branch)
    if getattr(args, "retain_only", False):
        return _handle_retain_only(args, graph, state, where_from, context, pending, marker)
    if pending:
        if args.attempt and args.attempt != pending["attempt"]:
            raise Refused(f"Finish pending delivery {pending['attempt']} first")
        return complete_delivery(args, graph, state, where_from, context, pending, tree)
    if adopt_from:
        meta, prior_found, prior_folder, prior_commit, provider, review_provider, steps, summary = _validate_adopt_from(
            args, state, branch, rel
        )
        attempt = start(args, graph, state, where_from, args.note or f"adopt verified run {adopt_from[:8]} of {rel}", branch)
        print(f"attempt {attempt} on the Routing card (adopting verified run {adopt_from[:8]})")
        return _deliver_run(
            args, graph, state, where_from, context, branch, rel, tree, attempt,
            prior_commit, prior_folder, provider, review_provider, meta.get("tool"),
            prior_found, steps, summary, recovered_from=adopt_from,
        )
    existing = args.attempt or open_attempts(state, args.by)
    tool = None
    if not existing:
        args.provider = args.provider or profile.get("provider")
        if not args.provider and not args.agent:
            raise Refused("Select a routing provider with --provider or routing_agent.provider (--provider gemini uses agy) before starting")
        if not args.agent:
            args.review_provider = getattr(args, "review_provider", None) or profile.get("review_provider") or args.provider
        check_tasks(args.tasks)
        level_with_main(branch)
        tool = prepare_mac(branch, git("rev-parse", "HEAD").strip(), tree)
    attempt = start(args, graph, state, where_from, args.note or f"routing run of {rel}", branch)
    commit = git("rev-parse", "HEAD").strip()
    print(f"attempt {attempt} on the Routing card")
    try:
        found = status(args, attempt)
    except mac.MacError as error:
        raise Refused(
            f"the Mac did not answer ({error}); the attempt stays open: run this again with --attempt {attempt}"
        ) from None
    if found["state"] == "absent":
        provider, review_provider, tool = _launch_absent_attempt(
            args, graph, state, where_from, profile, branch, commit, tree, rel, attempt, tool
        )
    else:
        print(f"found on the Mac: {found['state']}")
        provider, review_provider, commit, tool = _resume_existing_attempt(args, attempt, branch, rel)
    found = wait(args, attempt)
    folder = run_folder(args, attempt)
    steps, summary = (
        results(fetch(f"{folder}/out.jsonl").decode("utf-8", errors="replace"), strict=True)
        if found["state"] != "absent" else ([], None)
    )
    return _deliver_run(
        args, graph, state, where_from, context, branch, rel, tree, attempt,
        commit, folder, provider, review_provider, tool, found, steps, summary,
    )


def complete_delivery(args, graph, state, where_from, context, delivery, tree):
    attempt, chosen = delivery["attempt"], delivery["outcome"]
    def record(stage):
        return board_graph.record(graph, state, NODE, chosen, args.by, delivery["note"], delivery["files"],
                                  root=stage, tokens=delivery["tokens"], attempt_id=attempt, delivering_copper=bool(delivery["pcb"]))

    def record_path(item):
        name = board_graph.record_file_name(item["at"], item["node"], item["outcome"])
        return (state.records / name).resolve().relative_to(ROOT.resolve()).as_posix()

    live = where_from["how"] in ("mac", "ssh")
    try:
        item = routing_delivery.finish(ROOT, delivery, context, state, record, record_path if live else None)
    except (OSError, ValueError, mac.MacError) as error:
        raise Refused(f"Delivery {attempt} retained at {delivery['stage']}: {error}. "
                      f"Resume this task with --attempt {attempt}.") from None
    sys.stdout.write(board_graph.recorded(graph, state, item))
    if live:
        print(board_graph.keep_pull_request(graph, state, item, ROOT, context["branch"]))
    print(f"Published {delivery['files'][0]} on {context['branch']}. {delivery['note']}")
    pulled = mac.ssh(f"cd {tree} && git pull -q --ff-only && git rev-parse --short HEAD", timeout=120)
    print(f"the Mac's {tree}: " + (f"at {mac.text(pulled.stdout).strip()}" if pulled.returncode == 0 else
                                   f"did not pull: {mac.text(pulled.stderr).strip()}"))
    return 0 if chosen == "more_work" else 2


if __name__ == "__main__":
    raise SystemExit(main())
