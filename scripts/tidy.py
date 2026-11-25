#!/usr/bin/env python3
"""Cleanup: once a branch's work is on main, clear what it left behind.

  py -3 scripts/tidy.py           show what would be done
  py -3 scripts/tidy.py --apply   do it

It fetches origin with --prune, then:

- removes each worktree all of whose work is on origin/main, unless the
  worktree holds a file that is not main's (then it names the file and keeps
  it); the removal is forced, since the checks here decide and git alone
  refuses a worktree that holds git submodules;
- deletes the local branches all of whose work is on origin/main
  (`git branch -D`, after the check here, and it says the commit each was at);
- deletes those branches on GitHub;
- fast-forwards the checkout on main to origin/main, unless KiCad has a board
  open there (a `~*.lck` file in a project folder) or local changes are in the
  way;
- brings the KiCad host checkout level the same way, by one run of
  `scripts/mac_keep_pace.py` over SSH (`scripts/mac.py sync --by tidy`; that
  script says when it refuses and how it reloads the Mac's editors);
- then removes the Mac's worktrees and deletes its branches by the same rule,
  also keeping a worktree with a file open in KiCad (a `~*.lck` lock). A Mac
  that does not answer is skipped;
- lists every branch, on GitHub and here, and every worktree on no branch,
  with work not on main (its commit count, the last commit's date and subject,
  the worktree that holds it), so a stalled branch, and any decision that
  lives only on it, is seen.

"On main" means one of two things (landed()). The branch's tip is in
origin/main's history: a pull request merged (a merge commit), or a session's
commits pushed straight to main. Or every commit past main is on main as an
equal patch (`git cherry`): a lead cherry-picked or rebased a helper's commits
onto main, which leaves the helper's branch outside main's history with
nothing main lacks. A merge commit past main must then be what git makes of
its two parents by itself; one resolved by hand may hold a change no patch
shows, so its branch stays.

Every such local branch goes except main and one checked out in a worktree
that stays. Such a worktree, on a branch or on none, goes when it holds no
file that differs from main's (a run's outputs committed from another checkout
are main's, byte for byte, and are not work), nothing happened there for an
hour (a session still working keeps its folder), and, when nothing was ever
committed there (its tip came from a checkout or a reset), it is a day old (a
session may be about to work in a fresh one). The repository's own folder, the
first worktree, never goes. A disposable KiCad copy on the Mac (the site
settings' `disposable_copies` prefix, under /tmp or /private/tmp, one per
critic or check run, which nothing else deletes) goes with its changes once
KiCad does not have it open and nothing has used it for an hour.
"""
from __future__ import annotations

import argparse
import contextlib
import io
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

import mac  # noqa: E402
import no_window  # noqa: E402

ROOT = tool_paths.project_root()
LANDED_AGE = 3600  # seconds a worktree whose work is on main must have sat untouched before it goes
FRESH_AGE = 86400  # seconds a worktree nothing was committed in is kept: a session may be about to work there
STEP_RUN_AGE = 7 * 86400  # seconds a finished, inactive Mac routing run folder is kept before removal
FRESH = ("checkout", "reset", "branch", "clone")  # reflog entries that set HEAD without committing anything
NO_BRANCH = "(no branch)"  # in the Mac's plan: a worktree on no branch, all of whose commits are on main
# The Mac's disposable KiCad copies, detached worktrees: the site settings' prefix, also as macOS resolves /tmp.
_COPIES = mac.SITE.disposable_copies
DISPOSABLE = (f"/private{_COPIES}", _COPIES) if _COPIES.startswith("/tmp/") else (_COPIES,)
STEPS_DIR = mac.SITE.steps
LOCK_DIR = mac.SITE.editor_lock


def _shell_path(word: str) -> str:
    """A word for the Mac's shell: quoted, with a leading ~/ still the Mac user's home."""
    if word.startswith("~/") and not any(c in word for c in '"$`\\'):
        return f'"$HOME/{word[2:]}"'
    return mac.quote(word)


def git(*args: str, cwd: pathlib.Path = ROOT, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        check=False, timeout=timeout, **no_window.KWARGS,
    )


def worktrees(listing: str | None = None, cwd: pathlib.Path = ROOT) -> list[dict[str, str]]:
    """`git worktree list --porcelain`, here or as given; the main checkout comes first."""
    found, current = [], {}
    if listing is None:
        listing = git("worktree", "list", "--porcelain", cwd=cwd).stdout
    for line in listing.splitlines() + [""]:
        if not line:
            if current:
                found.append(current)
            current = {}
        elif line.startswith("worktree "):
            current["path"] = line[len("worktree "):]
        elif line.startswith("branch refs/heads/"):
            current["branch"] = line[len("branch refs/heads/"):]
    return found


def landed(ref: str, cwd: pathlib.Path = ROOT) -> bool:
    """Whether everything `ref` holds is on origin/main: its tip is in main's history, or every commit past main is
    there as an equal patch (cherry-picked or rebased onto main: `git cherry`) and every merge commit past main is
    what git makes of its two parents by itself, with nothing resolved by hand. A ref git cannot read has not."""
    if git("merge-base", "--is-ancestor", ref, "origin/main", cwd=cwd).returncode == 0:
        return True
    cherry = git("cherry", "origin/main", ref, cwd=cwd, timeout=300)
    if cherry.returncode != 0 or any(line.startswith("+") for line in cherry.stdout.splitlines()):
        return False
    for merge in git("rev-list", "--merges", f"origin/main..{ref}", cwd=cwd).stdout.split():
        made = git("merge-tree", "--write-tree", f"{merge}^1", f"{merge}^2", cwd=cwd)
        tree = git("rev-parse", f"{merge}^{{tree}}", cwd=cwd).stdout.strip()
        if made.returncode != 0 or not tree or made.stdout.split()[:1] != [tree]:
            return False
    return True


def local_branches(cwd: pathlib.Path = ROOT) -> list[str]:
    """Every local branch but main."""
    names = git("for-each-ref", "--format=%(refname)", "refs/heads", cwd=cwd).stdout.split()
    return [name[len("refs/heads/"):] for name in names if name != "refs/heads/main"]


def github_branches(cwd: pathlib.Path = ROOT) -> list[str]:
    """Every branch on GitHub but main, as this computer last fetched them."""
    names = git("for-each-ref", "--format=%(refname)", "refs/remotes/origin", cwd=cwd).stdout.split()
    return [name[len("refs/remotes/origin/"):] for name in names
            if name not in ("refs/remotes/origin/main", "refs/remotes/origin/HEAD")]


def merged_branches(cwd: pathlib.Path = ROOT) -> set[str]:
    """Local branches all of whose work is on origin/main (landed()), except main: the ones tidy deletes (one
    checked out in a worktree that stays is skipped)."""
    return {name for name in local_branches(cwd) if landed(f"refs/heads/{name}", cwd)}


def work_in(path: pathlib.Path) -> str | None:
    """The first file in a worktree that is work of its own: changed, new or deleted there, and not what origin/main
    has at that path. None when every such file is main's, byte for byte, as when a run's outputs were committed
    from another checkout."""
    listing = git("status", "--porcelain", "-z", "-uall", "--no-renames", cwd=path).stdout
    for entry in listing.split("\0"):
        name = entry[3:]
        if not name:
            continue
        on_main = git("rev-parse", "-q", "--verify", f"origin/main:{name}", cwd=path).stdout.strip()
        here = git("hash-object", "--", name, cwd=path).stdout.strip() if (path / name).is_file() else ""
        if here != on_main:
            return name
    return None


def reflog(path: pathlib.Path) -> tuple[str, str]:
    """A worktree's HEAD reflog, `%H %ct %gs` a line: its latest entry, and the entry that set the current tip."""
    tip = git("rev-parse", "HEAD", cwd=path).stdout.strip()
    lines = git("reflog", "--format=%H %ct %gs", "HEAD", cwd=path).stdout.splitlines()
    return (lines[0] if lines else ""), next((line for line in lines if line.startswith(tip + " ")), "")


def stays(latest: str, at_tip: str, now: float) -> str | None:
    """Why a clean worktree whose work is on main stays, from its HEAD reflog's latest entry and the entry that set
    its tip (`%H %ct %gs` each): it was used within the hour, or nothing was committed there (its tip came from a
    checkout, or its reflog does not say) and it is under a day old. None when it sits idle and can go."""
    if not latest or not at_tip:
        return "its reflog is empty"
    used = recent(latest, now)
    if used:
        return used
    subject = (at_tip.split(" ", 2) + [""])[2]
    if (not subject or subject.startswith(FRESH)) and now - when(latest) < FRESH_AGE:
        return "nothing was committed there"
    return None


def when(line: str) -> int:
    """The time of a reflog line (`%H %ct %gs`), in seconds; 0 when it does not say."""
    try:
        return int(line.split(" ", 2)[1])
    except (IndexError, ValueError):
        return 0


def recent(latest: str, now: float) -> str | None:
    """Why a worktree used within the hour stays, from its newest reflog line; None once it has sat long enough."""
    if not latest:
        return None
    age = now - when(latest)
    return f"it was used {int(age // 60)} min ago" if age < LANDED_AGE else None


def held(path: pathlib.Path, now: float) -> str | None:
    """Why a worktree whose commits are all on main stays: it holds a file that is not main's, it was used within
    the hour, or nothing was committed there and it is under a day old. None when it can go."""
    work = work_in(path)
    if work:
        return f"it has uncommitted work ({work})"
    return stays(*reflog(path), now)


def not_on_main(cwd: pathlib.Path = ROOT, trees: list[dict[str, str]] | None = None,
                merged: set[str] | None = None) -> list[dict]:
    """What here holds commits main lacks, read with no network: each local branch (`branch`, the worktree at
    `path` that holds it or None, and `github`: "same" when GitHub's branch of that name is at the same commit,
    "differs" when it is not, None when GitHub has none), then each worktree on no branch (`branch` None). Each
    with `ahead`, its commits past main, and `last`, its newest commit's date and subject."""
    trees = worktrees(cwd=cwd) if trees is None else trees
    merged = merged_branches(cwd) if merged is None else merged
    holder = {tree["branch"]: tree["path"] for tree in trees if tree.get("branch")}

    def facts(ref: str, at: pathlib.Path) -> dict:
        return {"ahead": int(git("rev-list", "--count", f"origin/main..{ref}", cwd=at).stdout.strip() or 0),
                "last": git("log", "-1", "--format=%cs %s", ref, cwd=at).stdout.strip()[:110]}

    found = []
    for name in sorted(set(local_branches(cwd)) - merged):
        tip = git("rev-parse", f"refs/heads/{name}", cwd=cwd).stdout.strip()
        theirs = git("rev-parse", "-q", "--verify", f"refs/remotes/origin/{name}", cwd=cwd).stdout.strip()
        found.append({"branch": name, "path": holder.get(name),
                      "github": None if not theirs else "same" if theirs == tip else "differs",
                      **facts(f"refs/heads/{name}", cwd)})
    for tree in trees[1:]:
        if not tree.get("branch") and not landed("HEAD", pathlib.Path(tree["path"])):
            found.append({"branch": None, "path": tree["path"], "github": None,
                          **facts("HEAD", pathlib.Path(tree["path"]))})
    return found


def remove_tree(path: pathlib.Path) -> bool:
    """Delete a folder git left behind: the submodules' files, caches, and on Windows a file with a reserved name
    (one called NUL, which git itself could not delete), reached through the long-path prefix. True once it is gone."""
    target = "\\\\?\\" + str(path.resolve()) if os.name == "nt" else str(path)
    shutil.rmtree(target, ignore_errors=True)
    return not path.exists()


def tidy(apply: bool, keep: tuple = (), root: pathlib.Path = ROOT) -> tuple[int, str | None]:
    """The cleanup, printed as it goes. Returns the exit code and why the Mac was skipped (None when it was not).

    A worktree holding the current directory or a path in `keep` stays: a script that runs tidy keeps its own.
    `root` is the repository; a test names its own."""
    say = (lambda text: print(text)) if apply else (lambda text: print("would " + text))

    if git("fetch", "--prune", "origin", cwd=root, timeout=120).returncode != 0:
        print("git fetch failed; nothing done")
        return 1, None
    merged_local = merged_branches(root)
    github = github_branches(root)
    merged_remote = {name for name in github if landed(f"refs/remotes/origin/{name}", root)}
    inside = [pathlib.Path.cwd().resolve(), *(pathlib.Path(path).resolve() for path in keep)]
    trees = worktrees(cwd=root)
    checked_out = {tree["branch"] for tree in trees[:1] if tree.get("branch")}  # the repository's own folder stays
    now = time.time()

    for tree in trees[1:]:
        branch, path = tree.get("branch"), pathlib.Path(tree["path"])
        if branch is None:
            if not landed("HEAD", path):
                continue  # on no branch, with commits main lacks: listed below
        elif branch not in merged_local:
            continue  # listed below
        if any(here == path.resolve() or path.resolve() in here.parents for here in inside):
            reason = "this run is inside it"
        else:
            reason = held(path, now)
        if reason:
            print(f"kept {path} ({branch or 'no branch'}): {reason}")
            checked_out.add(branch)
            continue
        say(f"remove worktree {path} ({branch or 'no branch'})")
        if apply:
            # The checks above decided; git alone refuses a worktree that holds submodules.
            result = git("worktree", "remove", "--force", str(path), cwd=root)
            if result.returncode != 0 and (path / ".git").exists():
                print(f"  failed: {result.stderr.strip()}")
                checked_out.add(branch)
            elif path.exists():  # git dropped the worktree (its .git file is gone) but left files behind
                print(f"  removed what git left behind at {path}" if remove_tree(path) else f"  files remain at {path}")

    for branch in sorted(merged_local - checked_out):
        tip = git("rev-parse", "--short", f"refs/heads/{branch}", cwd=root).stdout.strip()
        say(f"delete local branch {branch} (was {tip})")
        if apply:
            # landed() decided; `git branch -d` refuses a branch whose commits reached main as patches.
            result = git("branch", "-D", branch, cwd=root)
            if result.returncode != 0:
                print(f"  failed: {result.stderr.strip()}")
    if merged_remote:
        say("delete on GitHub: " + ", ".join(sorted(merged_remote)))
        if apply:
            result = git("push", "origin", "--delete", *sorted(merged_remote), cwd=root, timeout=120)
            if result.returncode != 0:
                print(f"  failed: {result.stderr.strip()}")
    if apply:
        git("worktree", "prune", cwd=root)

    fast_forward_main(trees, apply, say, root)
    skipped = None
    if not mac.LOCAL:  # on the Mac, or on a project's one machine, its checkout is the one above
        try:
            mac.sync(apply, by="tidy")
            tidy_mac(apply, say)
        except mac.MacError as exc:
            print(f"Mac: skipped ({exc})")
            skipped = str(exc)
    for name in sorted(set(github) - merged_remote):
        ahead = git("rev-list", "--count", f"origin/main..origin/{name}", cwd=root).stdout.strip()
        last = git("log", "-1", "--format=%cs %s", f"origin/{name}", cwd=root).stdout.strip()
        print(f"not on main yet: {name}, {ahead} commit(s), last {last[:110]}")
    for line in not_on_main_lines(not_on_main(root, worktrees(cwd=root), merged_local)):
        print(line)
    return 0, skipped


def not_on_main_lines(found: list[dict]) -> list[str]:
    """not_on_main() in words, a line each. A branch GitHub holds at the same commit, in no worktree here, is
    GitHub's line already and gets none."""
    lines = []
    for item in found:
        held_in = f", in {item['path']}" if item["path"] else ""
        if item["branch"] is None:
            lines.append(f"not on main yet, on no branch: {item['path']}, {item['ahead']} commit(s), "
                         f"last {item['last']}")
        elif item["github"] != "same" or item["path"]:
            place = {None: "only here", "same": "here", "differs": "here it differs from GitHub's"}[item["github"]]
            lines.append(f"not on main yet, {place}: {item['branch']}, {item['ahead']} commit(s), "
                         f"last {item['last']}{held_in}")
    return lines


def run(apply: bool = True, keep: tuple = ()) -> dict:
    """tidy() for another script: its exit code, what it printed, and why the Mac was skipped (None when it was not)."""
    said = io.StringIO()
    with contextlib.redirect_stdout(said):
        code, skipped = tidy(apply, keep)
    return {"code": code, "said": said.getvalue(), "mac": skipped}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clear the branches and worktrees whose work is on main, and bring main up to date.")
    parser.add_argument("--apply", action="store_true", help="do it; without this, only show what would be done")
    return tidy(parser.parse_args().apply)[0]


# The Mac's worktrees, its branches whose work is on main, why a worktree must stay, and its worktrees on no branch
# whose commits are on main. landed and work_in are landed() and work_in() above, in the Mac's shell.
MAC_LISTING = r'''cd @CHECKOUT@
git fetch --prune -q origin
landed() {
  git rev-parse -q --verify "$1^{commit}" >/dev/null 2>&1 || return 1
  git merge-base --is-ancestor "$1" origin/main 2>/dev/null && return 0
  past=$(git cherry origin/main "$1" 2>/dev/null) || return 1
  printf '%s\n' "$past" | grep -q '^+' && return 1
  for m in $(git rev-list --merges "origin/main..$1"); do
    [ "$(git merge-tree --write-tree "$m^1" "$m^2" 2>/dev/null | head -1)" = "$(git rev-parse "$m^{tree}")" ] || return 1
  done
  return 0
}
work_in() {
  git -C "$1" status --porcelain -uall --no-renames 2>/dev/null | cut -c4- | while IFS= read -r f; do
    case "$f" in '"'*) echo "$f"; break;; esac
    on_main=$(git -C "$1" rev-parse -q --verify "origin/main:$f" 2>/dev/null)
    here=""
    [ -f "$1/$f" ] && here=$(git -C "$1" hash-object -- "$f" 2>/dev/null)
    [ "$here" = "$on_main" ] || { echo "$f"; break; }
  done
}
git worktree list --porcelain
echo ::merged
for b in $(git for-each-ref --format='%(refname:short)' refs/heads); do
  [ "$b" != main ] && landed "refs/heads/$b" && echo "$b"
done
echo ::kept
git worktree list --porcelain | sed -n 's/^worktree //p' | tail -n +2 | while read -r p; do
  f=$(work_in "$p")
  [ -n "$f" ] && printf '%s\tit has uncommitted work (%s)\n' "$p" "$f"
  lock=$(find "$p" -maxdepth 3 -name '~*.lck' 2>/dev/null | head -1)
  [ -n "$lock" ] && printf '%s\tKiCad has %s open there\n' "$p" "${lock#$p/}"
done
echo ::reflog
git worktree list --porcelain | sed -n 's/^worktree //p' | tail -n +2 | while read -r p; do
  tip=$(git -C "$p" rev-parse HEAD 2>/dev/null)
  echo "$p"
  git -C "$p" reflog --format='%H %ct %gs' HEAD 2>/dev/null | head -1 | grep . || echo
  git -C "$p" reflog --format='%H %ct %gs' HEAD 2>/dev/null | grep -m1 "^$tip " || echo
done
echo ::unmerged
for b in $(git for-each-ref --format='%(refname:short)' refs/heads); do
  [ "$b" != main ] && ! landed "refs/heads/$b" && printf '%s\t%s\t%s\n' "$b" "$(git rev-list --count "origin/main..$b")" \
    "$(git log -1 --format='%cs %s' "$b" | cut -c1-110)"
done
echo ::detached
git worktree list --porcelain | sed -n 's/^worktree //p' | tail -n +2 | while read -r p; do
  git -C "$p" symbolic-ref -q HEAD >/dev/null 2>&1 && continue
  tip=$(git -C "$p" rev-parse HEAD 2>/dev/null)
  if landed "$tip"; then printf '%s\t\n' "$p"; else
    printf '%s\t%s commit(s), last %s\n' "$p" "$(git rev-list --count "origin/main..$tip")" \
      "$(git log -1 --format='%cs %s' "$tip" | cut -c1-110)"
  fi
done
echo ::step_runs
steps_dir=@STEPS@
lock_dir=@LOCK@
printf 'dir\t%s\n' "$steps_dir"
if [ -d "$steps_dir" ]; then
  for d in "$steps_dir"/run-*; do
    [ -d "$d" ] || continue
    base=${d##*/}
    attempt=${base#run-}
    mtime=$(stat -f %m "$d" 2>/dev/null || stat -c %Y "$d" 2>/dev/null || echo 0)
    finished=0
    [ -f "$d/exit" ] && finished=1
    active=0
    if [ -f "$d/pid" ]; then
      p=$(cat "$d/pid" 2>/dev/null)
      [ -n "$p" ] && kill -0 "$p" 2>/dev/null && active=1
    fi
    [ -n "$(find "$d" -maxdepth 3 -name '~*.lck' 2>/dev/null | head -1)" ] && active=1
    [ -e "$lock_dir/routing-run-$attempt" ] && active=1
    printf '%s\t%s\t%s\t%s\n' "$d" "$mtime" "$finished" "$active"
  done
fi
true
'''.replace("@CHECKOUT@", mac.CHECKOUT).replace("@STEPS@", _shell_path(STEPS_DIR)).replace("@LOCK@", _shell_path(LOCK_DIR))


def mac_plan(listing: str, now: float | None = None) -> tuple[list[tuple[str, str | None]], list[str], list[str]]:
    """From MAC_LISTING's output: worktrees to remove (path, and its branch, NO_BRANCH for one on no branch whose
    commits are on main, or None for a disposable copy), branches to delete, and notes."""
    git_listing, _, _ = listing.partition("::step_runs\n")
    trees_text, _, rest = git_listing.partition("::merged\n")
    merged_text, _, rest = rest.partition("::kept\n")
    kept_text, _, rest = rest.partition("::reflog\n")
    reflog_text, _, rest = rest.partition("::unmerged\n")
    unmerged_text, _, detached_text = rest.partition("::detached\n")
    trees, merged = worktrees(trees_text), set(merged_text.split())
    kept = {}
    for line in kept_text.splitlines():
        path, _, reason = line.partition("\t")
        kept.setdefault(path, []).append(reason)
    lines = reflog_text.splitlines()
    reflogs = {lines[i]: (lines[i + 1], lines[i + 2]) for i in range(0, len(lines) - 2, 3)}
    detached = dict(line.split("\t", 1) for line in detached_text.splitlines() if "\t" in line)  # "" when on main
    remove, notes, busy = [], [], {tree.get("branch") for tree in trees[:1]}
    moment = now if now is not None else time.time()
    for tree in trees[1:]:
        branch, path = tree.get("branch"), tree["path"]
        if branch is None and path.startswith(DISPOSABLE):
            # A disposable KiCad copy: its changes are nobody's work, so only an open editor or recent use keeps it.
            reason = (next((why for why in kept.get(path, []) if why.startswith("KiCad")), None)
                      or recent(reflogs.get(path, ("", ""))[0], moment))
            if reason:
                notes.append(f"Mac: kept {path} (a disposable copy): {reason}")
            else:
                remove.append((path, None))
            continue
        if branch is None and detached.get(path) != "":
            ahead = detached.get(path)
            notes.append(f"Mac: not on main yet, on no branch: {path}, {ahead}" if ahead
                         else f"Mac: left alone (no branch): {path}")
            continue
        if branch is not None and branch not in merged:
            busy.add(branch)
            continue
        reason = (kept.get(path) or [None])[0] or stays(*reflogs.get(path, ("", "")), moment)
        if reason:
            notes.append(f"Mac: kept {path} ({branch or 'no branch'}): {reason}")
            busy.add(branch)
        else:
            remove.append((path, branch or NO_BRANCH))
    for line in unmerged_text.splitlines():
        name, _, rest = line.partition("\t")
        ahead, _, last = rest.partition("\t")
        notes.append(f"Mac: not on main yet, only there: {name}, {ahead} commit(s), last {last}")
    return remove, sorted(merged - busy), notes


def _valid_steps_dir(candidate: str, configured: str) -> bool:
    cand = candidate.rstrip("/")
    if not cand.startswith("/") or ".." in pathlib.PurePosixPath(cand).parts:
        return False
    if configured.startswith("~/"):
        return cand.endswith("/" + configured[2:].rstrip("/"))
    return cand == configured.rstrip("/")


def mac_step_runs_plan(listing: str, now: float | None = None, *,
                       steps_dir: str = STEPS_DIR) -> tuple[list[tuple[str, int]], list[str]]:
    """From MAC_LISTING's ::step_runs section: finished inactive Mac run-* folders >= 7 days old and notes."""
    _, sep, step_text = listing.partition("::step_runs\n")
    if not sep:
        return [], []
    moment = now if now is not None else time.time()
    lines = step_text.splitlines()
    resolved_dir = steps_dir.rstrip("/")
    if lines and lines[0].startswith("dir\t"):
        listed_dir = lines[0].split("\t", 1)[1].strip()
        lines = lines[1:]
        if _valid_steps_dir(listed_dir, steps_dir):
            resolved_dir = listed_dir.rstrip("/")
    if not resolved_dir.startswith("/"):
        return [], []
    prefix = resolved_dir + "/run-"
    remove: list[tuple[str, int]] = []
    notes: list[str] = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        path, mtime_s, finished_s, active_s = parts
        if not path.startswith(prefix) or "/" in path[len(prefix):] or ".." in path:
            continue
        try:
            mtime = int(mtime_s)
        except ValueError:
            continue
        finished = finished_s == "1"
        active = active_s == "1"
        age = max(0, int(moment - mtime))
        if active:
            continue
        if not finished:
            if age >= STEP_RUN_AGE:
                notes.append(f"Mac: kept unfinished routing run folder {path} (no exit file)")
            continue
        if age >= STEP_RUN_AGE:
            remove.append((path, age // 86400))
    return remove, notes


def tidy_mac(apply: bool, say) -> None:
    """The Mac's worktrees, branches whose work is on main, and old finished step run folders, by the rule used here.
    Its KiCad stays open, so a worktree or run folder with a file open in KiCad is kept like one with uncommitted work.
    Runs after the Mac's main is brought up to date."""
    listing = mac.bash(MAC_LISTING, timeout=300)
    remove, branches, notes = mac_plan(listing)
    old_runs, run_notes = mac_step_runs_plan(listing)
    for note in [*notes, *run_notes]:
        print(note)
    failed = set()
    for path, branch in remove:
        say(f"remove the Mac's worktree {path} ({branch or 'a disposable copy'})")
        if apply:
            result = mac.ssh("bash -s", mac.PATH + f"cd {mac.CHECKOUT} && git worktree remove --force {mac.quote(path)}")
            if result.returncode != 0:
                print(f"  failed: {(mac.text(result.stderr) + mac.text(result.stdout)).strip()}")
                failed.add(branch)
    for branch in branches:
        if branch in failed:
            continue
        say(f"delete the Mac's branch {branch}")
        if apply:
            # The Mac's landed decided; `git branch -d` refuses a branch whose commits reached main as patches.
            result = mac.ssh("bash -s", mac.PATH + f"cd {mac.CHECKOUT} && git branch -D {mac.quote(branch)}")
            if result.returncode != 0:
                print(f"  failed: {(mac.text(result.stderr) + mac.text(result.stdout)).strip()}")
    if apply and remove:
        mac.bash(f"cd {mac.CHECKOUT} && git worktree prune")
    for path, days in old_runs:
        say(f"remove old Mac routing run folder {path} (finished, {days} day(s) old)")
        if apply:
            result = mac.ssh("bash -s", mac.PATH + f"rm -rf -- {mac.quote(path)}")
            if result.returncode != 0:
                print(f"  failed: {(mac.text(result.stderr) + mac.text(result.stdout)).strip()}")


# The Mac's listing, then where its checkout stands against GitHub's main, then its keep-pace job's last status.
MAC_STATUS = MAC_LISTING + '''echo ::main
echo "$(git symbolic-ref --short -q HEAD || echo detached) $(git rev-list --count HEAD..origin/main) \\
$(git rev-list --count origin/main..HEAD) $(git rev-parse --short HEAD)"
echo ::keep_pace
cat logs/mac-keep-pace/status.json 2>/dev/null || true
echo ::page
''' + (f'BOARD_GRAPH_PROJECT="$PWD" python3 {mac.TOOL_CHECKOUT}/scripts/board_graph_serve.py --status 2>/dev/null || true'
       if mac.TOOL_CHECKOUT else "python3 scripts/board_graph_serve.py --status 2>/dev/null || true") + "\n"


def mac_status() -> dict:
    """Where the Mac's checkout stands, what tidy would remove there, the last run of its keep-pace job
    (scripts/mac_keep_pace.py) and the graph page's status (scripts/board_graph_serve.py --status); it changes
    nothing but a fetch. Raises mac.MacError when the Mac does not answer."""
    listing, _, rest = mac.bash(MAC_STATUS, timeout=300).partition("::main\n")
    line, _, rest = rest.partition("::keep_pace\n")
    kept, _, page = rest.partition("::page\n")
    branch, behind, ahead, head = line.split()
    remove, _, notes = mac_plan(listing)
    return {"branch": branch, "behind": int(behind), "ahead": int(ahead), "head": head,
            "tidy_pending": [{"path": path, "branch": name} for path, name in remove], "notes": notes,
            "keep_pace": json_status(kept, "its status.json"), "page": json_status(page, "its status line")}


def json_status(text: str, what: str) -> dict | None:
    """A status another script wrote as JSON (`what` names it): None when there is none, else the status, or
    {"error": why} when it does not read."""
    if not text.strip():
        return None
    try:
        status = json.loads(text)
    except ValueError as error:
        return {"error": f"{what} did not read ({error})"}
    return status if isinstance(status, dict) else {"error": f"{what} is not an object"}


def fast_forward_main(trees: list[dict[str, str]], apply: bool, say, root: pathlib.Path = ROOT) -> None:
    main_tree = next((t for t in trees if t.get("branch") == "main"), None)
    if main_tree is None:
        print("no checkout on main; nothing to fast-forward")
        return
    main_path = pathlib.Path(main_tree["path"])
    behind = int(git("rev-list", "--count", "main..origin/main", cwd=root).stdout.strip() or 0)
    if behind == 0:
        print(f"main is up to date ({main_path})")
        return
    locks = sorted(main_path.glob("*/~*.lck"))
    if locks:
        print(f"main is {behind} commit(s) behind but KiCad has {locks[0].parent.name} open; not touching it")
        return
    say(f"fast-forward main by {behind} commit(s) ({main_path})")
    if apply:
        result = git("merge", "--ff-only", "origin/main", cwd=main_path)
        if result.returncode != 0:
            print(f"  failed, local changes are in the way: {result.stderr.strip()}")


if __name__ == "__main__":
    sys.exit(main())
