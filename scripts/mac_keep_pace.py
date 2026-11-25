#!/usr/bin/env python3
"""Keep the Mac's main checkout level with GitHub's main: one run each time main changes, started by whoever
changed it. Nothing polls.

  python3 scripts/mac_keep_pace.py --by push   one run: fetch, fast-forward main unless something is in the way
  python3 scripts/mac_keep_pace.py --dry-run   what a run would do: it fetches, and changes and logs nothing

Who starts a run: a session on the PC, right after its push to main, over SSH (`scripts/mac.py sync`; tidy runs
the same with --by tidy), and the `sync` workflow (.github/workflows/sync.yml), which GitHub starts on every push
to main and the Mac's runner runs here with --by workflow, so a merge made on GitHub itself reaches the Mac too.
Each says what it did or why not, so a push that was not followed is a line, never silence.

It runs on the Mac only, in the checkout it sits in. A run fetches origin, then fast-forwards main to GitHub's
main, unless something is in the way; then it changes no file and says what:

- the checkout is on another branch, or main holds commits GitHub's main lacks;
- a rebase, merge, cherry-pick, revert or bisect is in progress, or another git command holds the index;
- a tracked file has uncommitted changes (untracked files are git's to protect: it will not overwrite them);
- the commits coming in change a file KiCad has open (a `~<name>.lck` beside it while KiCad runs), and this
  run cannot see that KiCad holds no unsaved edits: macOS gives window titles only to a process with Screen
  Recording, which SSH has on the Mac and the runner's python3 does not. That fast-forward waits for a run over
  SSH, `scripts/mac.py sync`, which the pusher and tidy make.

After a fast-forward that changed a board's cards (`tools/board_graph.json`, another listed board's cards file,
or the list of boards, scripts/boards.py), view.json is rewritten from the database, every board's
(`scripts/board_graph_view.py snapshot --quick`, which tells the page server); after any other fast-forward the
page server is told directly (`scripts/board_graph_serve.py --notify`), so an open page reloads itself when
index.html changed and reads view.json again otherwise.

Everything a run does after its fast-forward runs in a fresh process in the checkout, so it is the new commit's
code: this process loaded its modules from the commit before, and a new commit's module imported beside them
can need what they lack (the page server once failed so, on a `mac` module from before the settings file).
Nothing is imported after the fast-forward.

The board's files and project come from its description (scripts/board_description.py), and with several
boards listed, each board's from its own. After a fast-forward
that changed a board's PCB file while KiCad has it open, it reloads the PCB Editor
with `scripts/kicad_reload_board.py` through the guarded runner, which reloads only when nothing is unsaved and
then checks the editor against the file. A changed schematic open in the Schematic Editor gets
File > Revert when exactly one Schematic Editor window shows it and nothing in KiCad is unsaved; that revert is
not checked, so look at it. A reload refused or failed stays pending, and each run that can read the titles
tries it again, until it succeeds or KiCad no longer has the file open.

Every run but a dry one appends one line to `logs/mac-keep-pace/keep-pace.log` (the time, who started it, the
result, main's head, what it found and did) and writes `logs/mac-keep-pace/status.json`, which
`scripts/board_graph.py where --full` reads over SSH. The results: `level`, `forwarded`, `held` (something is
in the way; nothing changed) and `failed`.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

import tool_paths

if __name__ == "__main__":
    tool_paths.take_argument()

import board_description  # noqa: E402
import boards  # noqa: E402
import mac  # noqa: E402

TOOL_ROOT = tool_paths.TOOL_ROOT
ROOT = tool_paths.project_root()
LOGS = pathlib.Path("logs") / "mac-keep-pace"
GRAPH = "tools/board_graph.json"  # a fast-forward that changes it rewrites view.json from the database
LOG_LIMIT = 1_000_000  # bytes, about nine days of lines; past it the log moves to keep-pace.log.1
LOCK_WAIT = 150  # seconds a run waits for another run to finish
# The board's files and project, from its description (scripts/board_description.py).
DESCRIPTION = board_description.load()
BOARD = DESCRIPTION.rel("pcb")
SCHEMATIC = DESCRIPTION.rel("schematic")
PROJECT = DESCRIPTION.project_folder  # the folder kicad_run.ps1 and kicad_reload_board.py take as the project
SCHEMATIC_TITLE = f"{DESCRIPTION.stem} — Schematic Editor"  # KiCad's title for the saved schematic, its em dash
# A KiCad that can hold a file open: the project manager or an editor, not kicad-cli or KiCad's Python (CI runs both).
KICAD_EDITORS = "^/Applications/KiCad/KiCad[.]app/Contents/(MacOS/kicad( |$)|Applications/[^/]+[.]app/Contents/MacOS/)"
IN_PROGRESS = (("rebase-merge", "a rebase"), ("rebase-apply", "a rebase"), ("MERGE_HEAD", "a merge"),
               ("CHERRY_PICK_HEAD", "a cherry-pick"), ("REVERT_HEAD", "a revert"), ("BISECT_LOG", "a bisect"),
               ("index.lock", "another git command (it holds index.lock)"))


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def one_line(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def listed(paths: list[str], most: int = 3) -> str:
    shown = ", ".join(paths[:most])
    return shown + (f" and {len(paths) - most} more" if len(paths) > most else "")


class Checkout:
    """The checkout and the Mac around it, as a run reads and changes them. The tests stand in for it."""

    def __init__(self, root: pathlib.Path = ROOT):
        self.root = pathlib.Path(root)

    def git(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, check=False,
                              env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))

    def read(self, *args: str) -> str:
        done = self.git(*args)
        if done.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {one_line(done.stderr or done.stdout) or done.returncode}")
        return done.stdout

    def fetch(self) -> str | None:
        """None, or why the fetch failed."""
        try:
            done = self.git("fetch", "--prune", "-q", "origin", timeout=120)
        except subprocess.TimeoutExpired:
            return "it did not finish within 120 s"
        return None if done.returncode == 0 else one_line(done.stderr or done.stdout) or f"exit {done.returncode}"

    def standing(self) -> dict:
        """Main against GitHub's main as last fetched: branch (None when detached), head, target (origin/main's
        commit), ahead, behind, the git operation in progress (None), tracked files with uncommitted changes, and
        the files the commits coming in change."""
        branch = self.git("symbolic-ref", "--short", "-q", "HEAD").stdout.strip() or None
        head = self.read("rev-parse", "HEAD").strip()
        target = self.read("rev-parse", "origin/main").strip()
        ahead, behind = (int(n) for n in self.read("rev-list", "--left-right", "--count", f"{head}...{target}").split())
        git_dir = pathlib.Path(self.read("rev-parse", "--absolute-git-dir").strip())
        busy = next((what for name, what in IN_PROGRESS if (git_dir / name).exists()), None)
        dirty, entries = [], self.read("--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=no").split("\0")
        while entries:
            entry = entries.pop(0)
            if entry:
                dirty.append(entry[3:])
                if entry[0] in "RC" and entries:
                    entries.pop(0)  # a rename's or copy's source path
        incoming = [p for p in self.read("diff", "--name-only", "-z", head, target).split("\0") if p] if behind else []
        return {"branch": branch, "head": head, "target": target, "ahead": ahead, "behind": behind, "busy": busy,
                "dirty": dirty, "incoming": incoming}

    def kicad_running(self) -> bool:
        return subprocess.run(["pgrep", "-f", KICAD_EDITORS], capture_output=True, check=False).returncode == 0

    def open_in_kicad(self, paths: list[str]) -> list[str]:
        """The paths KiCad has open: a `~<name>.lck` beside the file while KiCad's project manager or an editor
        runs (a lock left when none runs is stale)."""
        locked = [p for p in paths if (self.root / p).with_name(f"~{pathlib.PurePosixPath(p).name}.lck").exists()]
        return locked if locked and self.kicad_running() else []

    def titles(self) -> list[str]:
        """The title of every KiCad window, minimized ones too: "" for each when this run has no Screen Recording."""
        return [w["title"] for w in mac.windows(every=True) if w["app"] in mac.EDITORS]

    def fast_forward(self, target: str) -> str | None:
        """None, or why `git merge --ff-only` failed."""
        try:
            done = self.git("merge", "--ff-only", "-q", target, timeout=600)
        except subprocess.TimeoutExpired:
            return "it did not finish within 600 s"
        return None if done.returncode == 0 else one_line(done.stderr or done.stdout) or f"exit {done.returncode}"

    def _tool_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        if tool_paths.is_external() or pathlib.Path(self.root).resolve() != TOOL_ROOT.resolve():
            env[tool_paths.ENV] = str(pathlib.Path(self.root).resolve())
        if extra:
            env.update(extra)
        return env

    def refresh_view(self) -> str | None:
        """None, or why view.json was not rewritten from the database: scripts/board_graph_view.py's quick snapshot,
        run in this checkout so it reads this graph and the Mac's database, and tells the page server itself."""
        script = tool_paths.project_script("scripts/board_graph_view.py", self.root)
        try:
            done = subprocess.run([sys.executable, str(script), "snapshot", "--quick"],
                                  cwd=self.root, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=120, check=False, env=self._tool_env())
        except (OSError, subprocess.TimeoutExpired) as error:
            return one_line(str(error))
        return None if done.returncode == 0 else one_line(done.stderr or done.stdout) or f"exit {done.returncode}"

    def tell_page(self) -> str | None:
        """None, or why the page server was not told that main changed: scripts/board_graph_serve.py --notify, run
        in a fresh process in this checkout, so it is the fast-forwarded commit's code with that commit's modules
        (an import here would load it beside the modules this process loaded from the commit before)."""
        script = tool_paths.project_script("scripts/board_graph_serve.py", self.root)
        try:
            done = subprocess.run([sys.executable, str(script), "--notify"], cwd=self.root,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                                  check=False, env=self._tool_env())
        except (OSError, subprocess.TimeoutExpired) as error:
            return one_line(str(error))
        return None if done.returncode == 0 else one_line(done.stdout or done.stderr) or f"exit {done.returncode}"

    def reload_board(self, description: str | None = None) -> tuple[bool, str]:
        """(reloaded, what scripts/kicad_reload_board.py said), run through the guarded runner as the KiCad skill
        says. It reloads only when the editor has no unsaved edits, and checks the editor against the file.
        `description` names another listed board's description; without it, this process's own board."""
        pwsh = shutil.which("pwsh") or str(pathlib.Path.home() / ".local" / "bin" / "pwsh")
        project = board_description.load(description, self.root).project_folder if description else PROJECT
        runner = tool_paths.project_script("scripts/kicad_run.ps1", self.root)
        extra = {board_description.ENV: description} if description else None
        try:
            done = subprocess.run([pwsh, "-NoProfile", "-File", str(runner), "-Mode", "Live", "-Project",
                                   project, "-Script", "scripts/kicad_reload_board.py", "-ScriptArgs",
                                   f"--project={project}"],
                                  cwd=self.root, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=300, check=False, env=self._tool_env(extra))
        except (OSError, subprocess.TimeoutExpired) as error:
            return False, one_line(f"the runner did not run: {error}")
        lines = (done.stdout + "\n" + done.stderr).strip().splitlines()
        said = next((line for line in reversed(lines) if line.startswith("status=")), None)
        said = said or (lines[-1] if lines else f"exit {done.returncode}")
        return done.returncode == 0 and said.startswith("status=reloaded"), one_line(said)

    def revert_schematic(self, description: str | None = None) -> tuple[bool, str]:
        """(reverted, what was done): revert_schematic_here() run by this script with --revert-schematic in a fresh
        process in this checkout, so after a fast-forward it is that commit's code. `description` names another
        listed board's description, which that process then reads as its board."""
        script = tool_paths.project_script("scripts/mac_keep_pace.py", self.root)
        extra = {board_description.ENV: description} if description else None
        try:
            done = subprocess.run([sys.executable, str(script), "--revert-schematic"], cwd=self.root,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                                  check=False, env=self._tool_env(extra))
        except (OSError, subprocess.TimeoutExpired) as error:
            return False, one_line(f"the revert did not run: {error}")
        try:
            reverted, words = json.loads(done.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError, TypeError):
            return False, one_line(f"the revert said nothing it could be read by: {done.stderr or done.stdout}"
                                   or f"exit {done.returncode}")
        return bool(reverted), str(words)


def revert_schematic_here() -> tuple[bool, str]:
    """(reverted, what was done): File > Revert in the one Schematic Editor window that shows the board's project,
    when nothing in KiCad is unsaved. KiCad says nothing back, so the result is not checked."""
    rows = [w for w in mac.windows(every=True) if w["app"] in mac.EDITORS]
    unsaved = [w["title"] for w in rows if w["title"].startswith("*")]
    if unsaved:
        return False, f"KiCad has unsaved edits ({unsaved[0]})"
    editors = {str(w["pid"]) for w in rows if w["app"] == "Schematic Editor" and w["title"] == SCHEMATIC_TITLE}
    if len(editors) != 1:
        return False, f"{len(editors)} Schematic Editor windows show {DESCRIPTION.stem}, so none was reverted"
    mac.act(editors.pop(), ['click menu item "Revert" of menu "File" of menu bar item "File" of menu bar 1',
                            "delay 1", "key code 36"])  # Return answers "Revert ... to last saved version?" Yes
    return True, "reverted through File > Revert, not checked against the file: look at it"


def every_board(root: pathlib.Path = ROOT):
    """(the files whose change means the views are rewritten, the reloaders): every listed board's cards file and
    the list itself; and for each board's PCB and schematic, the editor that shows it, the Checkout method that
    reloads it there and the board's description for that method (None for this process's own board, the only
    one in a repository with one board). A list or a description that cannot be read leaves this board alone."""
    graphs = {GRAPH, boards.LIST}
    reloaders = {BOARD: ("the PCB Editor", "reload_board", None),
                 SCHEMATIC: ("the Schematic Editor", "revert_schematic", None)}
    try:
        listed = boards.every(root)
    except boards.BoardsError:
        listed = []
    for board in listed:
        graphs.add(board.graph)
        try:
            description = board_description.load(board.description, root)
        except board_description.DescriptionError:
            continue
        if description.rel("pcb") == BOARD:
            continue
        reloaders.setdefault(description.rel("pcb"), ("the PCB Editor", "reload_board", board.description))
        reloaders.setdefault(description.rel("schematic"),
                             ("the Schematic Editor", "revert_schematic", board.description))
    return graphs, reloaders


GRAPHS, RELOADERS = every_board()


def keep_pace(machine, by: str, dry_run: bool = False, previous: dict | None = None, at: str | None = None) -> dict:
    """One run, as the status it leaves: at, by (push, workflow, tidy or hand), result, said, head, branch, behind,
    ahead, and pending, the reloads left for a run that can read KiCad's titles."""
    at = at or now()
    previous = previous or {}
    pending = [dict(item) for item in previous.get("pending") or [] if item.get("file") in RELOADERS]
    status = {"at": at, "by": by, "result": None, "said": "", "head": None, "branch": None, "behind": None,
              "ahead": None, "pending": pending}
    said = []

    def end(result: str, words: str) -> dict:
        said.insert(0, words)
        status.update(result=result, said="; ".join(said))
        return status

    fetch_error = machine.fetch()
    s = machine.standing()
    status.update(head=s["head"][:8], branch=s["branch"], behind=s["behind"], ahead=s["ahead"])
    titles_read = None  # read once, only when a file KiCad has open is involved

    def titles():
        nonlocal titles_read
        if titles_read is None:
            titles_read = machine.titles()
        return titles_read

    try_pending = not dry_run
    if fetch_error:
        result = ("failed", f"git fetch failed ({fetch_error}); main is {s['behind']} commit(s) behind GitHub's main "
                            "as last fetched")
    elif s["branch"] != "main":
        result = ("held", f"the checkout is on {s['branch'] or 'no branch'}, not main: left alone")
    elif s["ahead"]:
        result = ("held", f"main holds {s['ahead']} commit(s) GitHub's main lacks"
                          + (f" and is {s['behind']} behind" if s["behind"] else "") + ": left alone")
    elif not s["behind"]:
        result = ("level", "main is level with GitHub's main")
    elif s["busy"]:
        result = ("held", f"main is {s['behind']} commit(s) behind, but {s['busy']} is in progress: left alone")
    elif s["dirty"]:
        result = ("held", f"main is {s['behind']} commit(s) behind, but tracked files have uncommitted changes "
                          f"({listed(s['dirty'])}): left alone")
    else:
        result = None
        open_files = machine.open_in_kicad(s["incoming"])
        behind = f"main is {s['behind']} commit(s) behind, and they change {listed(open_files)}, which KiCad has open"
        if open_files and not any(titles()):
            why = ("this process has no Screen Recording" if by == "workflow"
                   else "has this session lost Screen Recording?")
            result = ("held", behind + f"; this run cannot read KiCad's window titles ({why}), so it cannot tell "
                      "whether KiCad holds unsaved edits: left alone"
                      + (" for a run over SSH (scripts/mac.py sync, which the pusher and tidy run)"
                         if by == "workflow" else ""))
        elif open_files and any(title.startswith("*") for title in titles()):
            unsaved = next(title for title in titles() if title.startswith("*"))
            result = ("held", behind + f", and KiCad has unsaved edits ({unsaved}): left alone")
        elif dry_run:
            reloads = [f"{RELOADERS[f][0]} for {f}" for f in open_files if f in RELOADERS]
            result = ("would_forward", f"would fast-forward main by {s['behind']} commit(s), {s['head'][:8]} to "
                               f"{s['target'][:8]}" + (", then reload " + " and ".join(reloads) if reloads else ""))
        else:
            merge_error = machine.fast_forward(s["target"])
            if merge_error:
                result = ("failed", f"git merge --ff-only failed ({merge_error})")
            else:
                status.update(head=s["target"][:8], behind=0)
                result = ("forwarded", f"fast-forwarded main by {s['behind']} commit(s), {s['head'][:8]} to "
                                       f"{s['target'][:8]}")
                # The page: a changed graph means view.json is rewritten (the snapshot tells the page itself);
                # otherwise the page is told directly, so it reloads itself when index.html changed.
                refreshed = False
                if GRAPHS.intersection(s["incoming"]):
                    error = machine.refresh_view()
                    refreshed = error is None
                    said.append("view.json rewritten from the database (the graph changed)" if refreshed
                                else f"view.json was not rewritten ({error})")
                if not refreshed:
                    error = machine.tell_page()
                    said.append("the page was told" if error is None else f"the page was not told ({error})")
                for path in open_files:
                    if path not in RELOADERS:
                        said.append(f"{path} changed and KiCad has it open: reload it there")
                    elif all(item["file"] != path for item in pending):
                        pending.append({"file": path, "since": at, "why": "changed by this fast-forward"})

    # Reloads: tried while KiCad has the file open and this run can read its titles, dropped once it does not.
    if try_pending:
        for item in list(pending):
            what, method, description = RELOADERS[item["file"]]
            if not machine.open_in_kicad([item["file"]]):
                pending.remove(item)
                said.append(f"{item['file']}: no reload needed, KiCad no longer has it open")
            elif not any(titles()):
                said.append(f"reload of {item['file']} in {what} pending since {item['since']} ({item['why']}); "
                            "this run cannot read KiCad's titles")
            else:
                done, words = getattr(machine, method)(*([description] if description else []))
                if done:
                    pending.remove(item)
                    said.append(f"reloaded {item['file']} in {what} ({words})")
                else:
                    item["why"] = words
                    said.append(f"reload of {item['file']} in {what} refused or failed ({words}): pending")
    return end(*result)


def broken(by: str, previous: dict, why: str) -> dict:
    """The status of a run that could not finish: failed, saying why, with what the run before it knew."""
    return {"at": now(), "by": by, "result": "failed", "said": why, "head": previous.get("head"),
            "branch": previous.get("branch"), "behind": None, "ahead": None, "pending": previous.get("pending") or []}


def line(status: dict) -> str:
    return f"{status['result']} {status['head'] or '?'}: {status['said']}"


def read_status(logs: pathlib.Path) -> dict:
    try:
        status = json.loads((logs / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return status if isinstance(status, dict) else {}


def ensure_excluded(root: pathlib.Path, entry: str = f"{LOGS.as_posix()}/") -> bool:
    """Add `entry` to the repo's local `.git/info/exclude` if `root` is its top-level checkout and not already listed."""
    root = pathlib.Path(root)
    checkout = Checkout(root)
    top = checkout.git("rev-parse", "--show-toplevel")
    if top.returncode != 0 or not top.stdout.strip():
        return False
    try:
        if pathlib.Path(top.stdout.strip()).resolve() != root.resolve():
            return False
        git_path = checkout.git("rev-parse", "--git-path", "info/exclude")
        if git_path.returncode != 0 or not git_path.stdout.strip():
            return False
        exclude = pathlib.Path(git_path.stdout.strip())
        if not exclude.is_absolute():
            exclude = root / exclude
        existing = exclude.read_text(encoding="utf-8", errors="replace") if exclude.is_file() else ""
        if any(line.strip() in (entry, entry.rstrip("/")) for line in existing.splitlines()):
            return False
        exclude.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        exclude.write_text(existing + prefix + entry + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def write(logs: pathlib.Path, status: dict) -> None:
    """One line on the log (moved to keep-pace.log.1 past LOG_LIMIT) and the status, replaced whole."""
    if logs.name == LOGS.name and logs.parent.name == LOGS.parent.name:
        ensure_excluded(logs.parent.parent)
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / "keep-pace.log"
    with contextlib.suppress(OSError):
        if log.stat().st_size > LOG_LIMIT:
            log.replace(logs / "keep-pace.log.1")
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"{status['at']} {status['by']} {line(status)}\n")
    part = logs / f"status.json.{os.getpid()}.tmp"
    part.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    part.replace(logs / "status.json")


@contextlib.contextmanager
def one_at_a_time(path: pathlib.Path, wait: float):
    """Hold the lock file; raises TimeoutError when another run holds it past `wait` seconds."""
    import fcntl  # macOS and Linux only: the tests on the owner's PC never get here

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError from None
                time.sleep(0.5)
        yield


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Keep the Mac's main level with GitHub's main: one run, now.")
    parser.add_argument("--by", default="hand", help="who started this run: push (the pusher's scripts/mac.py sync), "
                                                     "workflow (the sync workflow on the Mac's runner), tidy or hand")
    parser.add_argument("--dry-run", action="store_true", help="say what a run would do; change and log nothing")
    parser.add_argument("--revert-schematic", action="store_true",
                        help="a run's own step, in a fresh process: revert the Schematic Editor and print "
                             "[reverted, what was done] as one JSON line")
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        print("refused: this script keeps the Mac's checkout; on the owner's PC tidy keeps main")
        return 2
    if args.revert_schematic:
        print(json.dumps(list(revert_schematic_here())))
        return 0
    machine, logs = Checkout(ROOT), ROOT / LOGS
    if args.dry_run:
        print(line(keep_pace(machine, args.by, dry_run=True, previous=read_status(logs))))
        return 0
    ensure_excluded(ROOT)
    try:
        with one_at_a_time(logs / "run.lock", LOCK_WAIT):
            previous = read_status(logs)
            try:
                status = keep_pace(machine, args.by, previous=previous)
            except Exception as error:  # a run that breaks still leaves its line and its status
                status = broken(args.by, previous, one_line(f"{type(error).__name__}: {error}"))
    except TimeoutError:
        status = broken(args.by, read_status(logs), f"another run held {LOGS / 'run.lock'} for {LOCK_WAIT} s")
    write(logs, status)
    print(line(status))
    return 1 if status["result"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
