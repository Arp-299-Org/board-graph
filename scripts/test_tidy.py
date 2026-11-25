"""Tidy's rule, on real repositories made for each test: a branch or worktree has landed when its tip is in main's
history or every commit past main is there as an equal patch; a landed worktree goes unless it holds a file that
is not main's. And the Mac's cleanup plan: landed worktrees go, unless they hold work or a file open in KiCad; a
disposable KiCad copy goes once KiCad does not have it open and nothing has used it for an hour."""
import contextlib
import io
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

# Fixture machine settings, read at import: its worktree and disposable-copy prefixes.
os.environ["BOARD_GRAPH_SITE"] = str(pathlib.Path(__file__).resolve().parent / "fixtures" / "board_graph_site.json")
import tidy  # noqa: E402
from tidy import mac_plan  # noqa: E402

LISTING = """worktree /Users/builder/demo-board
HEAD 1111111111111111111111111111111111111111
branch refs/heads/main

worktree /Users/builder/demo-right-side
HEAD 2222222222222222222222222222222222222222
branch refs/heads/route-right-side

worktree /Users/builder/demo-part-models
HEAD 3333333333333333333333333333333333333333
branch refs/heads/part-models

worktree /Users/builder/demo-board-flow
HEAD 4444444444444444444444444444444444444444
branch refs/heads/board-flow-test

worktree /Users/builder/demo-display
HEAD 5555555555555555555555555555555555555555
branch refs/heads/route-display

worktree /Users/builder/legacy
HEAD 6666666666666666666666666666666666666666
detached

worktree /Users/builder/demo-fresh
HEAD 1111111111111111111111111111111111111111
branch refs/heads/fresh-start

worktree /private/tmp/demo-review-86c6
HEAD 1111111111111111111111111111111111111111
detached

worktree /private/tmp/demo-live-dade
HEAD 1111111111111111111111111111111111111111
detached

::merged
fresh-start
part-models
route-display
route-right-side
sd-bus
::kept
/Users/builder/demo-display\tKiCad has board/~demo.kicad_pcb.lck open there
/Users/builder/demo-board-flow\tit has uncommitted work
/private/tmp/demo-review-86c6\tit has uncommitted work
/private/tmp/demo-live-dade\tit has uncommitted work
/private/tmp/demo-live-dade\tKiCad has board/~demo.kicad_pcb.lck open there
::reflog
/Users/builder/demo-right-side
2222222222222222222222222222222222222222 1793900000 commit: Route the right side
2222222222222222222222222222222222222222 1793900000 commit: Route the right side
/Users/builder/demo-part-models
3333333333333333333333333333333333333333 1793900000 rebase (finish): returning to refs/heads/part-models
3333333333333333333333333333333333333333 1793900000 rebase (finish): returning to refs/heads/part-models
/Users/builder/demo-board-flow
4444444444444444444444444444444444444444 1793900000 commit: flow: stopped
4444444444444444444444444444444444444444 1793900000 commit: flow: stopped
/Users/builder/demo-display
5555555555555555555555555555555555555555 1793900000 commit: Route the display
5555555555555555555555555555555555555555 1793900000 commit: Route the display
/Users/builder/legacy
6666666666666666666666666666666666666666 1793900000 checkout: moving from main to 6666666666666666666666666666666666666666
6666666666666666666666666666666666666666 1793900000 checkout: moving from main to 6666666666666666666666666666666666666666
/Users/builder/demo-fresh
1111111111111111111111111111111111111111 1793900000 checkout: moving from main to fresh-start
1111111111111111111111111111111111111111 1793900000 checkout: moving from main to fresh-start
/private/tmp/demo-review-86c6
1111111111111111111111111111111111111111 1793900000 checkout: moving from main to 1111111111111111111111111111111111111111
1111111111111111111111111111111111111111 1793900000 checkout: moving from main to 1111111111111111111111111111111111111111
/private/tmp/demo-live-dade
1111111111111111111111111111111111111111 1793900000 checkout: moving from main to 1111111111111111111111111111111111111111
1111111111111111111111111111111111111111 1793900000 checkout: moving from main to 1111111111111111111111111111111111111111
::unmerged
board-flow-test\t20\t2026-10-29 flow board-20261029-162340: stopped: the stop time came
"""
LATER = 1793900000 + 7200  # two hours after every reflog entry above


class MacPlanTests(unittest.TestCase):
    def test_merged_clean_worktrees_go_and_busy_ones_stay(self):
        remove, branches, notes = mac_plan(LISTING, LATER)
        # A disposable copy goes with its uncommitted changes once it has sat an hour; one open in KiCad stays.
        self.assertEqual([("/Users/builder/demo-right-side", "route-right-side"),
                          ("/Users/builder/demo-part-models", "part-models"),
                          ("/private/tmp/demo-review-86c6", None)], remove)
        # A merged branch with no worktree goes too; one whose worktree stays, stays.
        self.assertEqual(["part-models", "route-right-side", "sd-bus"], branches)
        self.assertEqual(["Mac: kept /Users/builder/demo-display (route-display): KiCad has board/~demo.kicad_pcb.lck open there",
                          "Mac: left alone (no branch): /Users/builder/legacy",
                          "Mac: kept /Users/builder/demo-fresh (fresh-start): nothing was committed there",
                          "Mac: kept /private/tmp/demo-live-dade (a disposable copy): KiCad has board/~demo.kicad_pcb.lck open there",
                          "Mac: not on main yet, only there: board-flow-test, 20 commit(s), last 2026-10-29 flow "
                          "board-20261029-162340: stopped: the stop time came"], notes)

    def test_unmerged_branch_is_new_work(self):
        remove, branches, _ = mac_plan(LISTING.replace("route-right-side\nsd-bus\n", ""), LATER)
        self.assertEqual([("/Users/builder/demo-part-models", "part-models"), ("/private/tmp/demo-review-86c6", None)], remove)
        self.assertEqual(["part-models"], branches)

    def test_a_worktree_used_within_the_hour_stays(self):
        remove, branches, notes = mac_plan(LISTING, 1793900000 + 720)
        self.assertEqual([], remove)
        self.assertIn("Mac: kept /Users/builder/demo-right-side (route-right-side): it was used 12 min ago", notes)
        self.assertIn("Mac: kept /private/tmp/demo-review-86c6 (a disposable copy): it was used 12 min ago", notes)
        self.assertEqual(["sd-bus"], branches)

    def test_a_worktree_nothing_was_committed_in_goes_after_a_day(self):
        remove, branches, notes = mac_plan(LISTING, 1793900000 + 2 * 86400)
        self.assertIn(("/Users/builder/demo-fresh", "fresh-start"), remove)
        self.assertIn("fresh-start", branches)
        self.assertNotIn("Mac: kept /Users/builder/demo-fresh (fresh-start): nothing was committed there", notes)

    def test_a_worktree_on_no_branch_goes_once_its_commits_are_on_main(self):
        # The listing's last part says of each worktree on no branch whether main has its commits (nothing after
        # the tab) or what it holds that main lacks. One the listing does not name is left alone, as before.
        said = LISTING + "::detached\n/Users/builder/legacy\t\n"
        remove, branches, notes = mac_plan(said, LATER)
        self.assertNotIn(("/Users/builder/legacy", tidy.NO_BRANCH), remove)
        self.assertIn("Mac: kept /Users/builder/legacy (no branch): nothing was committed there", notes)
        remove, branches, notes = mac_plan(said, 1793900000 + 2 * 86400)
        self.assertIn(("/Users/builder/legacy", tidy.NO_BRANCH), remove)
        self.assertNotIn(tidy.NO_BRANCH, branches)
        held = said.replace("::reflog\n", "/Users/builder/legacy\tit has uncommitted work (pictures/a.png)\n::reflog\n")
        remove, _, notes = mac_plan(held, 1793900000 + 2 * 86400)
        self.assertNotIn(("/Users/builder/legacy", tidy.NO_BRANCH), remove)
        self.assertIn("Mac: kept /Users/builder/legacy (no branch): it has uncommitted work (pictures/a.png)", notes)
        ahead = LISTING + "::detached\n/Users/builder/legacy\t6 commit(s), last 2026-11-10 Placement probe\n"
        remove, _, notes = mac_plan(ahead, 1793900000 + 2 * 86400)
        self.assertNotIn(("/Users/builder/legacy", tidy.NO_BRANCH), remove)
        self.assertIn("Mac: not on main yet, on no branch: /Users/builder/legacy, 6 commit(s), last 2026-11-10 "
                      "Placement probe", notes)

    def test_the_listing_asks_the_mac_by_the_rule_used_here(self):
        for asked in ("git cherry origin/main", "git merge-tree --write-tree", "hash-object", "echo ::detached",
                      "cd " + tidy.mac.CHECKOUT):
            self.assertIn(asked, tidy.MAC_LISTING)


class StaysTests(unittest.TestCase):
    """stays(): why a merged, clean worktree is kept, from two reflog lines."""

    def test_landed_fresh_recent_and_empty(self):
        commit = "abc 1793900000 commit: Measure the models"
        checkout = "abc 1793900000 checkout: moving from main to claude/x"
        self.assertIsNone(tidy.stays(commit, commit, LATER))
        self.assertIsNone(tidy.stays(checkout, commit, LATER), "a later checkout does not make the tip's commit fresh")
        self.assertEqual(tidy.stays(commit, checkout, LATER), "nothing was committed there")
        self.assertEqual(tidy.stays(commit, "abc 1793900000 reset: moving to HEAD", LATER), "nothing was committed there")
        self.assertEqual(tidy.stays(commit, commit, 1793900000 + 59 * 60), "it was used 59 min ago")
        self.assertEqual(tidy.stays("", "", LATER), "its reflog is empty")
        # A fresh one is kept for a day, not for ever; a tip whose reflog line has no words is read as fresh.
        self.assertIsNone(tidy.stays(commit, checkout, 1793900000 + tidy.FRESH_AGE + 1))
        for wordless in ("abc 1793900000 ", "abc 1793900000"):
            self.assertEqual(tidy.stays(wordless, wordless, LATER), "nothing was committed there")
            self.assertIsNone(tidy.stays(wordless, wordless, 1793900000 + tidy.FRESH_AGE + 1))


def run_git(where, *args):
    result = tidy.git(*args, cwd=where)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} in {where}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def commit(where, files, subject):
    """Write `files` (name to text, bytes as given) in a checkout and commit them; the commit's hash."""
    for name, text in files.items():
        path = pathlib.Path(where) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
    run_git(where, "add", "-A")
    run_git(where, "commit", "-q", "-m", subject)
    return run_git(where, "rev-parse", "HEAD")


class Work:
    """A GitHub stand-in (a bare origin) with one commit on main, and a clone of it: the repository tidy runs in."""

    def __init__(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.directory.name).resolve()
        self.origin, self.clone = self.base / "origin.git", self.base / "clone"
        run_git(self.base, "init", "-q", "--bare", "-b", "main", str(self.origin))
        run_git(self.base, "clone", "-q", str(self.origin), str(self.clone))
        for key, value in (("user.name", "test"), ("user.email", "test@example.invalid"),
                           ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
            run_git(self.clone, "config", key, value)
        commit(self.clone, {"docs/first.md": "first\n"}, "first")
        run_git(self.clone, "push", "-q", "-u", "origin", "main")

    def worktree(self, name, *more):
        """A worktree beside the clone on a new branch `name`; `more` goes to `git worktree add` instead."""
        path = self.base / name
        run_git(self.clone, "worktree", "add", "-q", *(more or ("-b", name)), str(path))
        return path

    def onto_main(self, *commits):
        """Cherry-pick commits onto main in the clone and push it: the same patches, as new commits."""
        run_git(self.clone, "cherry-pick", *commits)
        run_git(self.clone, "push", "-q", "origin", "main")

    def stop(self):
        self.directory.cleanup()


class LandedTests(unittest.TestCase):
    """landed(), work_in() and tidy() itself, on a repository made for the test."""

    def setUp(self):
        self.work = Work()
        self.addCleanup(self.work.stop)

    def test_a_branch_whose_commits_were_cherry_picked_onto_main_has_landed(self):
        clone, side = self.work.clone, self.work.worktree("side")
        first = commit(side, {"docs/a.md": "a\n"}, "a")
        second = commit(side, {"docs/b.md": "b\n"}, "b")
        commit(clone, {"docs/main.md": "main moved on\n"}, "main moved on")
        self.assertFalse(tidy.landed("refs/heads/side", clone))
        self.work.onto_main(first, second)
        # Main holds the two patches as commits of its own, so the branch's tip is not in main's history.
        self.assertNotEqual(tidy.git("merge-base", "--is-ancestor", "side", "origin/main", cwd=clone).returncode, 0)
        self.assertTrue(tidy.landed("refs/heads/side", clone))
        self.assertTrue(tidy.landed("HEAD", side))
        self.assertEqual(tidy.merged_branches(clone), {"side"})
        self.assertEqual(tidy.not_on_main(clone), [])
        commit(side, {"docs/c.md": "c\n"}, "c, which main lacks")
        self.assertFalse(tidy.landed("refs/heads/side", clone))
        self.assertEqual(tidy.merged_branches(clone), set())
        [item] = tidy.not_on_main(clone)
        self.assertEqual((item["branch"], item["ahead"], item["github"], pathlib.Path(item["path"]).name),
                         ("side", 3, None, "side"))
        self.assertTrue(item["last"].endswith(" c, which main lacks"))
        self.assertFalse(tidy.landed("refs/heads/no-such-branch", clone))

    def test_a_merge_git_made_alone_has_landed_and_one_resolved_by_hand_has_not(self):
        clone = self.work.clone
        plain, hand = self.work.worktree("plain"), self.work.worktree("hand")
        plain_work = commit(plain, {"docs/plain.md": "plain\n"}, "plain's work")
        hand_work = commit(hand, {"docs/hand.md": "hand\n"}, "hand's work")
        commit(clone, {"docs/main.md": "main moved on\n"}, "main moved on")
        run_git(clone, "push", "-q", "origin", "main")
        run_git(plain, "merge", "-q", "--no-edit", "origin/main")
        run_git(hand, "merge", "-q", "--no-commit", "--no-ff", "origin/main")
        commit(hand, {"docs/only-in-the-merge.md": "a change no patch shows\n"}, "Merge main, with more")
        self.work.onto_main(plain_work, hand_work)
        self.assertTrue(tidy.landed("refs/heads/plain", clone))
        self.assertFalse(tidy.landed("refs/heads/hand", clone))
        self.assertEqual(tidy.merged_branches(clone), {"plain"})

    def test_a_file_that_is_mains_byte_for_byte_is_not_work(self):
        clone, side = self.work.clone, self.work.worktree("side")
        commit(clone, {"out/report.txt": "the result\n"}, "the run's report, committed from another checkout")
        run_git(clone, "push", "-q", "origin", "main")
        self.assertIsNone(tidy.work_in(side))
        (side / "out").mkdir()
        (side / "out" / "report.txt").write_bytes(b"the result\n")
        self.assertIsNone(tidy.work_in(side), "new here, and main has the same bytes at that path")
        (side / "out" / "report.txt").write_bytes(b"another result\n")
        self.assertEqual(tidy.work_in(side), "out/report.txt")
        (side / "out" / "report.txt").write_bytes(b"the result\n")
        (side / "out" / "notes.txt").write_bytes(b"only here\n")
        self.assertEqual(tidy.work_in(side), "out/notes.txt")
        (side / "out" / "notes.txt").unlink()
        (side / "docs" / "first.md").write_bytes(b"first, edited\n")
        self.assertEqual(tidy.work_in(side), "docs/first.md")
        (side / "docs" / "first.md").unlink()
        self.assertEqual(tidy.work_in(side), "docs/first.md", "deleted here, and main still has it")

    def tidy(self, apply, later=0.0):
        """tidy() in the clone with no Mac, `later` seconds from now; what it printed."""
        said = io.StringIO()
        moment = time.time() + later
        with mock.patch.object(tidy.mac, "sync", lambda *args, **kwargs: None), \
                mock.patch.object(tidy, "tidy_mac", lambda *args, **kwargs: None), \
                mock.patch.object(tidy.time, "time", lambda: moment), contextlib.redirect_stdout(said):
            code, skipped = tidy.tidy(apply, root=self.work.clone)
        self.assertEqual((code, skipped), (0, None))
        return said.getvalue()

    def test_tidy_clears_what_landed_as_patches_and_keeps_what_holds_work(self):
        clone = self.work.clone
        picked, holds, still_open = (self.work.worktree(name) for name in ("picked", "holds", "open"))
        picked_work = commit(picked, {"docs/picked.md": "picked\n"}, "picked's work")
        holds_work = commit(holds, {"docs/holds.md": "holds\n"}, "holds's work")
        commit(still_open, {"docs/open.md": "open\n"}, "open's work, which main lacks")
        run_git(picked, "push", "-q", "-u", "origin", "picked")
        run_git(still_open, "push", "-q", "-u", "origin", "open")
        self.work.onto_main(picked_work, holds_work)
        (holds / "notes.txt").write_bytes(b"not on main\n")
        loose = self.work.worktree("loose", "--detach")
        run_git(loose, "checkout", "-q", "--detach", "origin/main~1")  # on no branch, at a commit main has
        ahead = self.work.worktree("ahead", "--detach")
        run_git(ahead, "checkout", "-q", "--detach", "origin/main")
        commit(ahead, {"docs/ahead.md": "ahead\n"}, "on no branch, and main lacks it")
        two_days = 2 * 86400

        said = self.tidy(apply=False, later=two_days)
        for line in (f"would remove worktree {picked} (picked)", f"would remove worktree {loose} (no branch)",
                     "would delete local branch picked (was ", "would delete on GitHub: picked\n"):
            self.assertIn(line, said)
        self.assertTrue(picked.exists() and loose.exists(), "a run without --apply changes nothing")
        self.assertIn("picked", run_git(clone, "branch", "--list", "picked"))

        said = self.tidy(apply=True)
        self.assertIn(f"kept {picked} (picked): it was used 0 min ago", said)
        self.assertTrue(picked.exists(), "used within the hour")

        said = self.tidy(apply=True, later=two_days)
        self.assertFalse(picked.exists())
        self.assertFalse(loose.exists())
        self.assertEqual(run_git(clone, "branch", "--list", "picked"), "")
        self.assertEqual(run_git(clone, "ls-remote", "--heads", "origin", "picked"), "")
        self.assertIn(f"kept {holds} (holds): it has uncommitted work (notes.txt)", said)
        self.assertTrue((holds / "notes.txt").exists())
        self.assertIn("holds", run_git(clone, "branch", "--list", "holds"))
        self.assertTrue(still_open.exists() and ahead.exists() and clone.exists())
        self.assertNotEqual(run_git(clone, "ls-remote", "--heads", "origin", "open"), "")
        self.assertIn("not on main yet: open, 1 commit(s), last ", said)
        self.assertRegex(said, r"not on main yet, here: open, 1 commit\(s\), last \S+ open's work, which main lacks, "
                               r"in .+open\n")
        self.assertRegex(said, r"not on main yet, on no branch: .+ahead, 1 commit\(s\), last \S+ on no branch, and "
                               r"main lacks it\n")
        self.assertNotIn("holds, ", said.split("not on main yet", 1)[1], "holds has landed: only its file keeps it")


class RunTests(unittest.TestCase):
    """run(): tidy for another script, its words captured and a skipped Mac named."""

    def test_run_returns_what_tidy_said_and_why_the_mac_was_skipped(self):
        def fake(apply, keep=()):
            print("main is up to date (C:/demo)")
            print("Mac: skipped (ssh failed: no route to host)")
            return 0, "ssh failed: no route to host"

        with mock.patch.object(tidy, "tidy", fake):
            result = tidy.run(keep=("C:/demo/worktree",))
        self.assertEqual(result, {"code": 0, "mac": "ssh failed: no route to host",
                                  "said": "main is up to date (C:/demo)\nMac: skipped (ssh failed: no route to host)\n"})

    def test_mac_status_reads_the_listing_and_the_main_line(self):
        with mock.patch.object(tidy.mac, "bash", lambda script, timeout=120: LISTING + "::main\nmain 3 0 abc1234\n"):
            status = tidy.mac_status()
        self.assertEqual((status["branch"], status["behind"], status["ahead"], status["head"]), ("main", 3, 0, "abc1234"))
        # Read now, more than a day after the listing's reflog entries: the worktree nothing was committed in goes too.
        self.assertEqual(status["tidy_pending"], [{"path": "/Users/builder/demo-right-side", "branch": "route-right-side"},
                                                  {"path": "/Users/builder/demo-part-models", "branch": "part-models"},
                                                  {"path": "/Users/builder/demo-fresh", "branch": "fresh-start"},
                                                  {"path": "/private/tmp/demo-review-86c6", "branch": None}])
        self.assertEqual(len(status["notes"]), 4)
        self.assertIsNone(status["keep_pace"], "no status.json on the Mac: the job has not run")
        self.assertIsNone(status["page"], "the Mac's main predates the page's server, or it said nothing")

    def test_mac_status_brings_the_keep_pace_jobs_last_status_and_the_pages(self):
        kept = '{"at": "2026-11-06T02:00:00Z", "by": "launchd", "result": "level"}\n'
        page = '{"address": "http://kicad-host:8000", "job": "installed", "launchd": {"state": "running", "pid": 7}, "answers": "yes"}\n'
        with mock.patch.object(tidy.mac, "bash", lambda script, timeout=120:
                               LISTING + "::main\nmain 0 0 abc1234\n::keep_pace\n" + kept + "::page\n" + page):
            status = tidy.mac_status()
        self.assertEqual((status["head"], status["keep_pace"]["result"], status["keep_pace"]["by"]),
                         ("abc1234", "level", "launchd"))
        self.assertEqual((status["page"]["address"], status["page"]["answers"], status["page"]["launchd"]["pid"]),
                         ("http://kicad-host:8000", "yes", 7))
        self.assertTrue(tidy.json_status("{half", "its status.json")["error"].startswith("its status.json did not read ("))
        self.assertEqual(tidy.json_status("[]", "its status line"), {"error": "its status line is not an object"})
        self.assertIn("cat logs/mac-keep-pace/status.json", tidy.MAC_STATUS)
        self.assertIn("python3 scripts/board_graph_serve.py --status", tidy.MAC_STATUS)

    def test_mac_step_runs_plan_and_backwards_compat_with_mac_plan(self):
        now = 1_800_000_000
        old_ts = now - 8 * 86400
        fresh_ts = now - 2 * 86400
        steps_dir = "/Users/builder/demo-steps"
        listing = (
            LISTING
            + "::step_runs\n"
            + f"dir\t{steps_dir}\n"
            + f"{steps_dir}/run-olddone\t{old_ts}\t1\t0\n"
            + f"{steps_dir}/run-oldactive\t{old_ts}\t1\t1\n"
            + f"{steps_dir}/run-oldunfinished\t{old_ts}\t0\t0\n"
            + f"{steps_dir}/run-fresh\t{fresh_ts}\t1\t0\n"
            + f"{steps_dir}/run-escape/nested\t{old_ts}\t1\t0\n"
            + f"/tmp/other/run-outside\t{old_ts}\t1\t0\n"
        )
        # mac_plan ignores ::step_runs and keeps its exact 3-tuple return value.
        remove_wt, branches, wt_notes = mac_plan(listing, 1793900000 + 2 * 86400)
        self.assertTrue(remove_wt and isinstance(branches, list) and isinstance(wt_notes, list))
        self.assertNotIn("run-olddone", " ".join(wt_notes))

        old_runs, run_notes = tidy.mac_step_runs_plan(listing, now=now)
        self.assertEqual(old_runs, [(f"{steps_dir}/run-olddone", 8)])
        self.assertEqual(run_notes, [f"Mac: kept unfinished routing run folder {steps_dir}/run-oldunfinished (no exit file)"])

        # Safety check: a tampered dir header outside configured SITE.steps refuses deletions.
        tampered = listing.replace(f"dir\t{steps_dir}", "dir\t/tmp/evil").replace(steps_dir + "/run-", "/tmp/evil/run-")
        self.assertEqual(tidy.mac_step_runs_plan(tampered, now=now), ([], []))
        self.assertIn("echo ::step_runs", tidy.MAC_LISTING)


if __name__ == "__main__":
    unittest.main()
