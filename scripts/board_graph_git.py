"""Git, GitHub pull request, branch record, and merge check operations for board_graph."""
from __future__ import annotations

import sys
import hashlib
import json
import pathlib
import shlex
import subprocess
import tempfile
from datetime import datetime, timezone

def _bg():
    mod = sys.modules.get("board_graph")
    if mod is None:
        import board_graph as mod
    return mod


def run_git(root, *args, data=None, timeout=120):
    """git in `root`, its output as bytes. Raises ValueError with git's own words when it fails."""
    try:
        result = subprocess.run(["git", *args], cwd=str(root), input=data, capture_output=True, timeout=timeout,
                                check=False, **_bg().NO_WINDOW_KWARGS)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"git {' '.join(args)}: {error}")
    if result.returncode != 0:
        said = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
        raise ValueError(f"git {' '.join(args)}: {said or f'exit {result.returncode}'}")
    return result.stdout


def git_text(root, *args, **options):
    return _bg().run_git(root, *args, **options).decode("utf-8", "replace")


def mac_checkout():
    """The Mac's main checkout as a path on the Mac (mac.CHECKOUT, with ~ the Mac user's home)."""
    return pathlib.Path(_bg().mac.CHECKOUT).expanduser()


def hashes_at(root, commit, paths):
    """hash_path's hash of each path as git stores it at `commit`, by path. Missing there: None.

    It hashes the blobs' own bytes, with no line-ending or LFS conversion, so the PC and the Mac agree on it.
    For the board's files (LF on every machine) it equals hash_path of the checked-out file.
    """
    wanted = {path: pathlib.PurePath(path).as_posix().rstrip("/") for path in paths}
    if not wanted:
        return {}
    entries = []
    for entry in _bg().run_git(root, "ls-tree", "-r", "-z", commit, "--", *sorted(set(wanted.values()))).split(b"\0"):
        meta, _, name = entry.partition(b"\t")
        if entry and meta.split()[1] == b"blob":
            entries.append((name.decode("utf-8"), meta.split()[2].decode("ascii")))
    contents = {}
    if entries:
        blobs = _bg().run_git(root, "cat-file", "--batch", data="".join(oid + "\n" for _, oid in entries).encode("ascii"))
        at = 0
        for name, _ in entries:
            end = blobs.index(b"\n", at)
            size = int(blobs[at:end].split()[2])
            contents[name] = blobs[end + 1:end + 1 + size]
            at = end + 2 + size
    found = {}
    for path, name in wanted.items():
        if name in contents:
            found[path] = _bg().sha256(contents[name])
            continue
        members = sorted((pathlib.PurePosixPath(member[len(name) + 1:]), data) for member, data in contents.items()
                         if member.startswith(name + "/"))
        if not members:
            found[path] = None
            continue
        digest = hashlib.sha256()
        for member, data in members:
            digest.update(member.as_posix().encode("utf-8") + b"\0")
            digest.update(_bg().sha256(data).encode("ascii") + b"\n")
        found[path] = digest.hexdigest()
    return found


def hash_at(root, commit, path):
    return _bg().hashes_at(root, commit, [path])[path]


def short(digest):
    return digest[:8] if digest else "missing"


def pull_request_view(number, root=_bg().ROOT):
    """What GitHub says of pull request `number` (gh pr view). Raises ValueError when gh cannot say."""
    try:
        result = subprocess.run(["gh", "pr", "view", str(number), "--json", _bg().PULL_REQUEST_FIELDS], cwd=str(root),
                                capture_output=True, timeout=60, check=False, **_bg().NO_WINDOW_KWARGS)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"gh pr view {number}: {error}")
    if result.returncode != 0:
        raise ValueError(f"gh pr view {number}: {result.stderr.decode('utf-8', 'replace').strip()}")
    return json.loads(result.stdout)


def checks(rollup):
    """A pull request's checks in a few words: passing, failing (names), running (names) or no checks."""
    if not rollup:
        return "no checks"
    failing, running = [], []
    for check in rollup:
        name = check.get("name") or check.get("context") or "a check"
        result = (check.get("conclusion") or check.get("state") or "").upper()
        status = (check.get("status") or "COMPLETED").upper()
        if status != "COMPLETED" or result in ("", "PENDING", "EXPECTED"):
            running.append(name)
        elif result not in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            failing.append(name)
    if failing:
        return f"failing ({', '.join(failing)})"
    if running:
        return f"running ({', '.join(running)})"
    return "passing"


def gh(root, *args, timeout=60):
    """GitHub's command line in `root`: its stdout, or ValueError with what it said."""
    try:
        result = subprocess.run(["gh", *args], cwd=str(root), capture_output=True, timeout=timeout, check=False,
                                **_bg().NO_WINDOW_KWARGS)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"gh {' '.join(args[:2])}: {error}")
    if result.returncode != 0:
        raise ValueError(f"gh {' '.join(args[:2])}: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout.decode("utf-8", "replace")


def branch_records(root, branch, folders=None):
    """The records committed on `branch` and not on origin/main, oldest first, as read from the files at HEAD:
    every listed board's, each with the name of its board (None in a repository with one board). `folders`
    gives {a records folder: its board's name} in place of the repository's own."""
    if folders is None:
        try:
            listed = _bg().boards.read(root)
        except _bg().boards.BoardsError:
            listed = {}
        folders = ({board.records: board.name for board in listed.values()} if len(listed) > 1 else
                   {_bg().RECORDS_IN: None})
    try:
        names = _bg().git_text(root, "log", "--reverse", "--format=", "--name-only", "--diff-filter=A",
                         f"origin/main..{branch}", "--", *folders).split()
    except ValueError:
        return []
    found = []
    for name in dict.fromkeys(names):
        path, folder = pathlib.Path(root) / name, pathlib.PurePosixPath(name).parent.as_posix()
        if path.is_file() and folder in folders:
            try:
                found.append(dict(json.loads(path.read_text(encoding="utf-8")), board=folders[folder]))
            except ValueError:
                continue
    return found


def pull_request_body(graph, state, root, branch, head):
    """What the branch's pull request says: what the branch holds (the graph's records on it) and CI's checks on
    its head as the graph holds them."""
    lines = [f"Branch `{branch}`, kept by `record` (scripts/board_graph.py): every record made on it is below, newest "
             "last, and the reviews' findings are in the records that cite them.", "", "## What the branch holds", ""]
    records = _bg().branch_records(root, branch)
    for item in records:
        label = graph.nodes[item["node"]]["label"] if item["node"] in graph.nodes else item["node"]
        board = f"{item['board']}: " if item.get("board") else ""  # with several boards, which one the record is of
        lines.append(f"- {item['at'][:16]}Z {board}{label}, {item['outcome']} by {item['actor']}: {item['note']}")
    if not records:
        lines.append("- no record on this branch yet")
    lines += ["", "## Checks", ""]
    row = state.checks_for(head) if head else None
    lines.append(f"CI's board checks on {head[:8]}: {_bg().checks_words(row)}" if row
                 else f"CI's board checks on {head[:8] if head else 'the head'}: no result on the graph yet")
    lines += ["", "Marked ready only by the record that asks the owner to merge, once CI's board checks have passed on "
                  "the commit it asks about; a pass alone leaves it a draft. `next` checks the merge."]
    return "\n".join(lines) + "\n"


def merge_ask_readiness(state, item):
    """Whether `item` may mark its pull request ready, and why not: (True, None) only when it asks the owner to merge
    (needs_owner with a pull request) and CI's board checks, as the graph holds them, passed on the commit it asks
    about. That commit is the pull request's head the record fetched and stored (inputs.pull_request.head), not the
    branch's head after the push: the record's own commit is new and has no CI result yet, and it adds only the
    record file and its reports. (None, None) when `item` asks for no merge."""
    merge = item.get("inputs", {}).get("pull_request")
    if item["outcome"] != "needs_owner" or not merge:
        return None, None
    commit = merge.get("head")
    if not commit:
        return False, "the merge ask names no commit"
    row = state.checks_for(commit)
    if not row:
        return False, f"CI's board checks on {commit[:8]}: no result on the graph yet"
    if row["passed"] is not True:
        return False, f"CI's board checks on {commit[:8]}: {_bg().checks_words(row)}"
    return True, None


def keep_pull_request(graph, state, item, root, branch, github=None):
    """After a record's push on `branch`: the branch's pull request, opened as a draft when there is none, its
    description made current, and marked ready only when `item` asks the owner to merge and CI's board checks have
    passed on the commit it asks about (merge_ask_readiness); a pass alone leaves it a draft. Returns one line; nothing
    raises, what failed is said, and the branch is pushed either way."""
    github = _bg().gh if github is None else github
    root = pathlib.Path(root)
    try:
        head = _bg().git_text(root, "rev-parse", "HEAD").strip()
    except ValueError:
        head = None
    body = _bg().pull_request_body(graph, state, root, branch, head)
    label = graph.nodes[item["node"]]["label"]
    ready, why_not = _bg().merge_ask_readiness(state, item)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(body)
        body_file = handle.name
    try:
        rows = json.loads(github(root, "pr", "list", "--head", branch, "--state", "open", "--json", "number,isDraft,url"))
        if not rows:
            url = github(root, "pr", "create", "--draft", "--base", "main", "--head", branch, "--title",
                         f"{label}: {branch}", "--body-file", body_file).strip().splitlines()[-1]
            number = url.rstrip("/").rsplit("/", 1)[-1]
            said = f"pull request {number} opened as a draft for {branch}: {url}"
            draft = True
        else:
            number, draft = rows[0]["number"], rows[0].get("isDraft", False)
            github(root, "pr", "edit", str(number), "--body-file", body_file)
            said = f"pull request {number} describes the branch as of this record"
        if ready and draft:
            github(root, "pr", "ready", str(number))
            said += "; marked ready for the owner"
        elif ready is False and draft:
            said += (f"; left a draft, not marked ready: {why_not}; record the merge ask again once CI has passed "
                     f"to mark it ready")
    except ValueError as error:
        said = f"the pull request step failed ({error}); the branch is pushed, and the next record tries again"
    finally:
        pathlib.Path(body_file).unlink(missing_ok=True)
    return said


def fetch_pull_request(root, number, head):
    """Fetch pull request `number`'s head from GitHub and check it is the commit GitHub named."""
    _bg().run_git(root, "fetch", "-q", "origin", f"refs/pull/{number}/head", timeout=180)
    fetched = _bg().git_text(root, "rev-parse", "FETCH_HEAD").strip()
    if fetched != head:
        raise ValueError(f"GitHub names {head[:8]} as pull request {number}'s head, but fetching it gave "
                         f"{fetched[:8]}: it moved while being read; record again")


def checkout_problems(graph, root):
    """Why the checkouts next depends on are not GitHub's main, as sentences; none when they are.

    Main here, the branch itself wherever it is checked out (or while a rebase holds it), must be
    origin/main exactly; the checkout next reads, when it is another one, must hold everything on
    origin/main. Neither may hold an uncommitted change to a file a node owns.
    """
    here = pathlib.Path(root).resolve()
    main_path = _bg().main_checkout(root)
    problems = []
    if _bg().main_head(root):
        label = f"main here ({main_path or 'no checkout is on it'})"
        behind, ahead = _bg().counts(root, "main")
        if behind and ahead:
            problems.append(f"{label} has diverged from GitHub's main: {behind} commit(s) behind and {ahead} ahead")
        elif behind:
            problems.append(f"{label} is {behind} commit(s) behind GitHub's main and was not fast-forwarded")
        elif ahead:
            problems.append(f"{label} has {ahead} commit(s) GitHub's main does not: push them first")
    checkouts = [(f"main here ({main_path})", main_path)] if main_path else []
    if main_path != here:
        label = f"this checkout ({here})"
        behind, _ = _bg().counts(here, "HEAD")
        if behind:
            problems.append(f"{label} is {behind} commit(s) behind GitHub's main and next reads its files: rebase it "
                            f"on origin/main")
        checkouts.append((label, here))
    for label, path in checkouts:
        dirty = _bg().dirty_owned(graph, path)
        if dirty:
            problems.append(f"{label} has uncommitted changes to files a node owns: {', '.join(dirty)}")
    return problems


def main_checkout(root, trees=None):
    """The checkout here that has main checked out, resolved, or None. `trees`: tidy.worktrees() already read."""
    if trees is None:
        trees = _bg().tidy.worktrees(_bg().git_text(root, "worktree", "list", "--porcelain"))
    tree = next((tree for tree in trees if tree.get("branch") == "main"), None)
    return pathlib.Path(tree["path"]).resolve() if tree else None


def main_head(root):
    """The branch main here, its commit abbreviated, or None when there is no such branch."""
    try:
        return _bg().git_text(root, "rev-parse", "--verify", "-q", "--short", "refs/heads/main").strip()
    except ValueError:
        return None


def dirty_owned(graph, path):
    return _bg().status(path, graph.owned())[1]


def lfs_content(root):
    """The checkout's git-LFS files as files, not pointers. A fresh worktree holds pointers until `git lfs pull`,
    and 3D models read by the Models card and board audits are often LFS files; missing pulls cause false missing-model
    findings. Returns (problems, said): the pull it ran, or why LFS
    could not be read, and a problem when pointers remain or the pull failed."""
    try:
        listed = _bg().git_text(root, "lfs", "ls-files")
    except ValueError as error:
        return [], f"git lfs did not answer ({error}); LFS files were not checked\n"
    pointers = [line.split(" ", 2)[2] for line in listed.splitlines() if " - " in line]
    if not pointers:
        return [], ""
    try:
        _bg().run_git(root, "lfs", "pull", timeout=600)
    except ValueError as error:
        return [f"{len(pointers)} git-LFS file(s) are pointers here and git lfs pull failed ({error})"], ""
    left = [line.split(" ", 2)[2] for line in _bg().git_text(root, "lfs", "ls-files").splitlines() if " - " in line]
    if left:
        return [f"{len(left)} git-LFS file(s) are still pointers after git lfs pull: {', '.join(left[:3])}"], ""
    return [], f"git lfs pull fetched {len(pointers)} file(s) that were pointers here\n"


def bring_current(graph, state, root, node, why, out, err, tidy_run=None):
    """The upkeep before next runs anything: tidy's apply, then the checks of checkout_problems(), then the
    checkout's git-LFS files as files (lfs_content).

    Returns True when next may go on. The run is one row in the upkeep table, written before tidy starts,
    with tidy's own words. A Mac tidy could not bring current is a note, never a refusal: every script
    next runs reads the checkout on this computer.
    """
    run_id = state.start_upkeep(node, why, _bg().now())
    try:
        result = (tidy_run or _bg().run_tidy)(root)
    except Exception as error:
        result = {"code": 1, "said": f"tidy stopped: {type(error).__name__}: {error}\n", "mac": None}
    said = result["said"]
    if result["code"]:
        problems = ["tidy did not finish, so main here cannot be made current"]
    else:
        try:
            graph.refresh()
        except (OSError, ValueError, KeyError, TypeError) as error:
            problems = [f"the graph could not be refreshed after tidy: {error}"]
        else:
            try:
                problems = _bg().checkout_problems(graph, root)
            except ValueError as error:
                problems = [f"the checkouts could not be read: {error}"]
        if not problems:
            lfs_problems, lfs_said = _bg().lfs_content(root)
            problems += lfs_problems
            said += lfs_said
    if problems:
        tidy_words = [line.strip() for line in said.splitlines()
                      if line.startswith(("main is", "  failed", "git fetch failed", "tidy stopped"))]
        if tidy_words:
            problems.append("tidy said: " + "; ".join(tidy_words))
    state.finish_upkeep(run_id, _bg().now(), "refused" if problems else "current", said, result["mac"], problems)
    out.write("Upkeep (tidy --apply):\n" + "".join(f"  {line}\n" for line in said.splitlines()))
    if result["mac"] is not None:
        out.write("The Mac was not brought current; next goes on, because every script next runs reads this "
                  "computer's checkout.\n")
    if problems:
        err.write(f"{node}: next runs nothing, because the checkouts are not current: " + "; ".join(problems) + "\n")
        return False
    return True


def local_upkeep(graph, state, root, node, why, out, err, tidy_run=None):
    """Lightweight local repo upkeep for non-PCB packs with upkeep='local' (no tidy.py or Mac sync)."""
    root = pathlib.Path(root)
    run_id = state.start_upkeep(node, why, _bg().now())
    problems = []
    said = ""
    try:
        git_dir = pathlib.Path(_bg().git_text(root, "rev-parse", "--git-dir").strip())
        if not git_dir.is_absolute():
            git_dir = (root / git_dir).resolve()
        for marker, label in (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"), ("rebase-apply", "rebase")):
            if (git_dir / marker).exists():
                problems.append(f"a git {label} is in progress here")
        head = _bg().git_text(root, "rev-parse", "--short", "HEAD").strip()
        said = f"local HEAD {head}"
        graph.refresh()
    except (OSError, ValueError, KeyError, TypeError) as error:
        problems.append(str(error))
    state.finish_upkeep(run_id, _bg().now(), "refused" if problems else "current", said, None, problems)
    if problems:
        err.write(f"{node}: local upkeep refused: " + "; ".join(problems) + "\n")
        return False
    out.write(f"Upkeep (local): {said}\n")
    return True


def _carry_merge_review(graph, state, node, question, merge, root):
    """Return a verified carried review dict for merge_check, or None if not reusable."""
    proof = question.get("review")
    scoped = (graph.data.get("approval_mode") == "enforce" and question["then"] == "pass"
              and _bg().board_approvals.tracked(graph.nodes[node]))
    mode = graph.data.get("approval_mode", "off")
    if proof and (scoped or (mode == "observe" and graph.nodes[node]["who"] != _bg().OWNER)):
        current = _bg().approval_review(graph, state, root).scope(node)
        if (proof["scope"] != current or proof["scope"] != _bg().review_at_revision(graph, state, node, root, merge)
                or _bg().hashes_at(root, merge, proof["evidence"]) != proof["evidence"]
                or any(_bg().hash_path(root / path) != value for path, value in proof["evidence"].items())):
            return None
        return dict(proof, asked_at=question["asked_at"], merge_commit=merge)
    return None


def _fresh_merge_review(graph, state, node, number, question, name, root, out, after, reason):
    """Record a fresh review request after merge verification mismatch or refusal."""
    owner = graph.nodes[node]["who"] == _bg().OWNER
    outcome = "needs_owner" if owner else "needs_lead"
    note = (f"PR {number} merged. Fresh review of {node} needed: {reason}. "
            f"Start this card with next --by {'owner' if owner else 'agent:<reviewer>'} and review the merged files.")
    item = _bg().record(graph, state, node, outcome, "script:merge_check", note, [name], root=root,
                        expected_question=question["asked_at"])
    out.write(_bg().recorded(graph, state, item))
    out.write(after(item, [name]))
    return 0, item, None


def merge_check(graph, state, track, question, root, out, err, upkeep, tidy_run, github, after=lambda item, extra: ""):
    """next at a node whose last transition asked the owner to merge a pull request: (exit code, the transition
    recorded or None, why it stopped in a few words or None), as run_node returns.

    Open: waiting, exit 2. Merged: the upkeep, then main's owned files against the hashes the question
    named at the pull request's head; equal records the outcome that follows (exit 0, or 1 if record
    refuses), different records nothing and lists each file (exit 1). Closed unmerged: exit 1.
    """
    node, number = question["node"], question["number"]
    try:
        info = github(number)
    except ValueError as error:
        err.write(f"{node}: could not ask GitHub whether pull request {number} merged: {error}\n")
        return 1, None, "GitHub did not say whether the pull request merged"
    if info["state"] == "OPEN":
        out.write(_bg().render_track(graph, state, track))
        out.write(f"waiting for the owner: merge pull request {number}\n")
        return 2, None, None
    if info["state"] != "MERGED" or not info.get("mergeCommit"):
        err.write(f"{node}: pull request {number} was closed without a merge, so no outcome follows from it; "
                  f"record what the node needs with record\n")
        return 1, None, "the pull request was closed without a merge"
    merge = info["mergeCommit"]["oid"]
    if not upkeep(graph, state, root, node, f"pull request {number} merged", out, err, tidy_run):
        return 1, None, "the checkouts are not current"
    expected = question["expected"]
    started = datetime.now(timezone.utc)
    report = {"node": node, "action": "merge_check", "started_at": started.isoformat(), "inputs": expected,
              "pull_request": number, "merge_commit": merge, "merged_at": info.get("mergedAt"),
              "asked_at_head": question["head"], "then": question["then"]}
    report["run_id"] = state.start_attempt(node, "merge_check", expected, report["started_at"],
                                           checkout=str(pathlib.Path(root).resolve()))
    main_hashes, differ, here = {}, [], []
    try:
        main_hashes = _bg().hashes_at(root, "origin/main", sorted(expected))
        here = _bg().git_text(root, "diff", "--name-only", "origin/main", "HEAD", "--", *expected).split() if expected else []
        differ = [path for path in sorted(expected) if main_hashes[path] != expected[path]]
        report.update(outcome=question["then"] if not differ and not here else "mismatch", main=main_hashes,
                      differ=differ, this_checkout_differs=here)
    except ValueError as error:
        report.update(outcome="error", error=str(error))
    report["finished_at"] = _bg().now()
    name = _bg().report_path(node, report["action"])
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    state.finish_attempt(report["run_id"], report["finished_at"], report["outcome"], name, _bg().hash_path(path),
                         _bg().kept_report(report))
    if report["outcome"] == "error":
        err.write(f"{node}: the merge check could not read main's files, so nothing is recorded: "
                  f"{report['error']}. Report: {name}\n")
        return 1, None, "the merge check could not read main's files"
    scoped = (graph.data.get("approval_mode") == "enforce" and question["then"] == "pass"
              and _bg().board_approvals.tracked(graph.nodes[node]))
    if report["outcome"] == "mismatch":
        if scoped:
            return _fresh_merge_review(graph, state, node, number, question, name, root, out, after,
                                      "files differ from the reviewed PR head: " + ", ".join(differ or here))
        err.write(f"{node}: pull request {number} merged as {merge[:8]}, but main's files are not the ones the "
                  f"question named at {question['head'][:8]}, so nothing is recorded and a person looks. "
                  f"Report: {name}\n")
        for owned in differ:
            err.write(f"  {owned}: expected {_bg().short(expected[owned])}, main has {_bg().short(main_hashes[owned])}\n")
        for owned in here:
            err.write(f"  {owned}: this checkout's committed file differs from main's\n")
        return 1, None, "main's files are not the ones the question named"
    files = ", ".join(f"{owned} {_bg().short(digest)}" for owned, digest in sorted(expected.items()))
    note = (f"merge check: pull request {number} merged as {merge[:8]} ({info.get('mergedAt')}), and main's owned "
            f"files are the ones named at its head {question['head'][:8]}: {files or 'the node owns none'}")
    proof = _carry_merge_review(graph, state, node, question, merge, root)
    if scoped and not proof:
        return _fresh_merge_review(graph, state, node, number, question, name, root, out, after,
                                  "the original review, its inputs or its cited evidence could not be matched")
    actor = proof["actor"] if proof else "script:merge_check"
    if proof:
        note += f". Carried the review by {actor} at {proof['head'][:8]}: {proof['note']}"
    try:
        item = _bg().record(graph, state, node, question["then"], actor, note,
                            [name, *question.get("evidence", [])], root=root, merged_review=proof,
                            expected_question=question["asked_at"] if scoped else None)
    except ValueError as error:
        if scoped:
            return _fresh_merge_review(graph, state, node, number, question, name, root, out, after, str(error))
        err.write(f"{node}: pull request {number} merged and main's files match, but record refused "
                  f"{question['then']}: {error}. Report: {name}\n")
        return 1, None, "record refused the outcome the merge leads to"
    out.write(_bg().recorded(graph, state, item))
    out.write(after(item, [name]))
    out.write(f"Report: {name}\n\n")
    return 0, item, None


def shell_quote(value):
    value = str(value)
    return "'" + value.replace("'", "''") + "'" if _bg().os.name == "nt" else shlex.quote(value)


def head_here(root):
    """The commit the checkout at `root` is on, in full, or None where there is no git or no commit."""
    try:
        return _bg().git_text(root, "rev-parse", "HEAD").strip()
    except ValueError:
        return None


def on_a_branch(node):
    """Whether the card's owned files go on a branch and a pull request: the board's design files (the schematic,
    the board, libraries, models, exports, production) do; docs, data and code go straight to main."""
    return any(any(path == folder or path.startswith(folder.rstrip("/") + "/") for folder in _bg().DESIGN_FOLDERS)
               and not path.endswith((".md", ".json")) for path in node["owns"])


def main_state(graph, root, path, head):
    """The branch main here (at commit `head`, checked out at `path` or nowhere) against origin/main, and the
    uncommitted changes to owned files in its checkout."""
    item = {"path": str(path) if path else None, "branch": "main", "this": False, "head": head}
    try:
        behind, ahead = _bg().counts(root, "main")
        item.update(behind=behind, ahead=ahead, dirty_owned=_bg().dirty_owned(graph, path) if path else [])
    except ValueError as error:
        item["error"] = str(error)
    return item


def checkout_here(graph, root):
    """The checkout at `root` as it is now, with no fetch: its branch and head, behind and ahead of origin/main
    as this computer last fetched it, and its uncommitted changes to owned files. One git call, or two when its
    branch does not track origin/main."""
    here = pathlib.Path(root).resolve()
    item = {"path": str(here), "branch": None, "this": True}
    try:
        info, dirty = _bg().status(here, graph.owned(), branch=True)
        if info.get("branch.head") not in (None, "(detached)"):
            item["branch"] = info["branch.head"]
        if info.get("branch.upstream") == "origin/main" and info.get("branch.ab"):
            ahead, behind = (int(part[1:]) for part in info["branch.ab"].split())
        else:
            behind, ahead = _bg().counts(here, "HEAD")
        item.update(head=info.get("branch.oid", "")[:8], oid=info.get("branch.oid") or None, behind=behind,
                    ahead=ahead, dirty_owned=dirty)
    except ValueError as error:
        item["error"] = str(error)
    return item


def open_pull_requests(root=_bg().ROOT):
    """GitHub's open pull requests (gh pr list). Raises ValueError when gh cannot say."""
    try:
        result = subprocess.run(["gh", "pr", "list", "--state", "open", "--limit", "50", "--json", _bg().PULL_REQUEST_LIST],
                                cwd=str(root), capture_output=True, timeout=60, check=False, **_bg().NO_WINDOW_KWARGS)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"gh pr list: {error}")
    if result.returncode != 0:
        said = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
        raise ValueError(f"gh pr list: {said or f'exit {result.returncode}'}")
    return json.loads(result.stdout)


def naming_record(transitions, number):
    """The newest transition naming pull request `number` and how: a merge question in its inputs first,
    else its note's words "pull request <number>". (None, None) when no record names it."""
    for row in reversed(transitions):
        asked = row["inputs"].get("pull_request")
        if isinstance(asked, dict) and asked.get("number") == number:
            return row, "inputs"
    for row in reversed(transitions):
        if number in {int(found) for found in _bg().NAMES_PULL_REQUEST.findall(row["note"])}:
            return row, "note"
    return None, None


def records_on_branch(graph, state, root, branch):
    """The records a branch holds that main lacks, oldest first, as git last fetched both (origin/<branch> against
    origin/main, no network), and the labels the branch's own graph gives its cards (it may hold a card main's
    graph lacks). ([], {}) when git cannot say: no such branch, or a records folder outside the checkout."""
    root = pathlib.Path(root).resolve()
    try:
        folder = pathlib.Path(state.records).resolve().relative_to(root).as_posix()
        names = _bg().git_text(root, "diff", "--name-only", "--diff-filter=A", f"origin/main...origin/{branch}", "--",
                         folder).split()
        rows = [json.loads(_bg().git_text(root, "show", f"origin/{branch}:{name}")) for name in names
                if name.endswith(".json")]
    except (ValueError, OSError, AttributeError, TypeError):
        return [], {}
    rows = sorted((row for row in rows if isinstance(row, dict) and {"at", "track", "node", "outcome"} <= set(row)),
                  key=lambda row: _bg().parse_time(row["at"]))
    labels = {}
    try:
        theirs = json.loads(_bg().git_text(root, "show", f"origin/{branch}:"
                                     f"{graph.path.resolve().relative_to(root).as_posix()}"))
        labels = {node["id"]: node.get("label", node["id"]) for node in theirs["nodes"]}
    except (ValueError, OSError, KeyError, TypeError):
        pass
    return rows, labels


def branch_progress(graph, rows, labels=None):
    """Where a branch's own records put each track they touch: per track, the card it stands at on the branch
    (`position`, DONE when it finished there), the cards that passed there (`passed`) and its newest record
    (`last`). Main's graph does not move for any of this: it is what merging the branch would bring."""
    labels = labels or {}

    def label(key):
        return _bg().DONE if key == _bg().DONE else labels.get(key) or graph.nodes.get(key, {}).get("label") or key

    found = {}
    for row in rows:
        track = graph.tracks.get(row["track"])
        entry = found.setdefault(row["track"], {
            "track": row["track"], "track_label": (track or {}).get("label", row["track"]),
            "ordered": bool(track and track["ordered"]), "position": None, "passed": []})
        moved = row["outcome"] not in graph.stay and row.get("target")
        if row["outcome"] == "pass" and row["node"] not in entry["passed"]:
            entry["passed"].append(row["node"])
        if row["outcome"] == "returned" and row.get("target") in entry["passed"]:
            entry["passed"] = entry["passed"][:entry["passed"].index(row["target"])]
        entry["position"] = (row["target"] if moved else row["node"]) if entry["ordered"] else None
        entry["position_label"] = label(entry["position"]) if entry["position"] else None
        entry["last"] = {"node": row["node"], "label": label(row["node"]), "outcome": row["outcome"],
                         "at": row["at"], "actor": row.get("actor")}
    return list(found.values())


def tied_pull_requests(transitions, found, on_branch=None):
    """GitHub's open pull requests (`found`, as gh lists them), by number, each tied to the node whose record
    names it (naming_record). `on_branch(name)`, when given, says where the pull request's branch stands by its
    own records, the ones main lacks (branch_progress): main's page shows work that is not merged yet."""
    tied = []
    for item in sorted(found, key=lambda pr: pr["number"]):
        row, named_in = _bg().naming_record(transitions, item["number"])
        tied.append({
            "number": item["number"], "title": item.get("title"), "branch": item.get("headRefName"),
            "draft": bool(item.get("isDraft")), "url": item.get("url"), "updated_at": item.get("updatedAt"),
            "checks": _bg().checks(item.get("statusCheckRollup")),
            "node": row["node"] if row else None, "track": row["track"] if row else None,
            "record": row["record_name"] if row else None, "named_in": named_in,
            "on_branch": on_branch(item["headRefName"]) if on_branch and item.get("headRefName") else [],
        })
    return tied
