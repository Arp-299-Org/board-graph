#!/usr/bin/env python3
"""The Mac's keep-pace run: what one run decides, with a stand-in machine (no Mac, no KiCad, no GitHub), the
log and status it leaves, and the git reads against a temporary origin."""
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
# Fixture board description and machine settings, read at import so every checkout derives the same names.
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
os.environ["BOARD_GRAPH_BOARD"] = str(FIXTURES / "board.json")
os.environ["BOARD_GRAPH_SITE"] = str(FIXTURES / "board_graph_site.json")
import mac  # noqa: E402
import mac_keep_pace  # noqa: E402
from mac_keep_pace import BOARD, SCHEMATIC, Checkout, keep_pace  # noqa: E402

AT = "2026-11-06T02:00:00Z"
OLD, NEW = "a" * 40, "b" * 40
BOARD_TITLE = "demo — PCB Editor"


def refuse(*args, **kwargs):
    raise AssertionError("a test reached the real Mac")


mac.ssh = refuse  # the windows, the menus and SSH all go through it


class FakeMac:
    """A stand-in for Checkout: main's standing, the files KiCad has open, its window titles, and the reloads,
    each call written down. By default main is 3 commits behind and clean, and KiCad has nothing open."""

    def __init__(self, **standing):
        self.standing_now = {"branch": "main", "head": OLD, "target": NEW, "ahead": 0, "behind": 3, "busy": None,
                             "dirty": [], "incoming": ["scripts/x.py", "docs/y.md"]}
        self.standing_now.update(standing)
        self.fetch_error = self.merge_error = None
        self.open = set()
        self.window_titles = []
        self.reload = (True, "status=reloaded board=demo.kicad_pcb items=900, the same as the file")
        self.revert = (True, "reverted through File > Revert, not checked against the file: look at it")
        self.refresh_error = self.tell_error = None
        self.calls = []

    def refresh_view(self):
        self.calls.append("refresh_view")
        return self.refresh_error

    def tell_page(self):
        self.calls.append("tell_page")
        return self.tell_error

    def fetch(self):
        self.calls.append("fetch")
        return self.fetch_error

    def standing(self):
        return dict(self.standing_now)

    def open_in_kicad(self, paths):
        return [p for p in paths if p in self.open]

    def titles(self):
        self.calls.append("titles")
        return list(self.window_titles)

    def fast_forward(self, target):
        self.calls.append(("fast_forward", target))
        return self.merge_error

    def reload_board(self):
        self.calls.append("reload_board")
        return self.reload

    def revert_schematic(self):
        self.calls.append("revert_schematic")
        return self.revert


class DecisionTests(unittest.TestCase):
    def run_once(self, machine, by="workflow", previous=None, dry_run=False):
        return keep_pace(machine, by, dry_run=dry_run, previous=previous, at=AT)

    def test_behind_and_clean_fast_forwards_and_tells_the_page(self):
        machine = FakeMac()
        status = self.run_once(machine)
        self.assertEqual(status["result"], "forwarded")
        self.assertEqual(status["said"], "fast-forwarded main by 3 commit(s), aaaaaaaa to bbbbbbbb; the page was told")
        self.assertEqual((status["head"], status["behind"], status["pending"]), ("bbbbbbbb", 0, []))
        # No window titles read: none needed. The page is told after the forward, before any reload.
        self.assertEqual(machine.calls, ["fetch", ("fast_forward", NEW), "tell_page"])
        machine = FakeMac()
        machine.tell_error = "no (Connection refused)"
        self.assertEqual(self.run_once(machine)["said"],
                         "fast-forwarded main by 3 commit(s), aaaaaaaa to bbbbbbbb; the page was not told "
                         "(no (Connection refused))")

    def test_a_forward_that_changed_the_graph_rewrites_the_view_from_the_database(self):
        machine = FakeMac(incoming=["tools/board_graph.json", "docs/y.md"])
        status = self.run_once(machine)
        self.assertEqual(status["said"], "fast-forwarded main by 3 commit(s), aaaaaaaa to bbbbbbbb; view.json "
                                         "rewritten from the database (the graph changed)")
        self.assertEqual(machine.calls, ["fetch", ("fast_forward", NEW), "refresh_view"])  # the snapshot tells the page
        machine = FakeMac(incoming=["tools/board_graph.json"])
        machine.refresh_error = "exit 1"
        status = self.run_once(machine)
        self.assertEqual(status["said"], "fast-forwarded main by 3 commit(s), aaaaaaaa to bbbbbbbb; view.json was "
                                         "not rewritten (exit 1); the page was told")
        self.assertEqual(machine.calls, ["fetch", ("fast_forward", NEW), "refresh_view", "tell_page"])

    def test_level_does_nothing(self):
        machine = FakeMac(behind=0, incoming=[])
        status = self.run_once(machine)
        self.assertEqual((status["result"], status["said"]), ("level", "main is level with GitHub's main"))
        self.assertEqual(machine.calls, ["fetch"])

    def test_dirty_tracked_files_hold_it(self):
        machine = FakeMac(dirty=["scripts/tidy.py", "docs/a.md", "docs/b.md", "docs/c.md"])
        status = self.run_once(machine)
        self.assertEqual(status["result"], "held")
        self.assertIn("tracked files have uncommitted changes (scripts/tidy.py, docs/a.md, docs/b.md and 1 more)",
                      status["said"])
        self.assertNotIn(("fast_forward", NEW), machine.calls)

    def test_another_branch_commits_github_lacks_and_an_operation_in_progress_hold_it(self):
        for standing, words in (({"branch": "route-x"}, "the checkout is on route-x, not main"),
                                ({"branch": None}, "the checkout is on no branch, not main"),
                                ({"ahead": 2}, "main holds 2 commit(s) GitHub's main lacks and is 3 behind"),
                                ({"ahead": 1, "behind": 0}, "main holds 1 commit(s) GitHub's main lacks: left alone"),
                                ({"busy": "a rebase"}, "but a rebase is in progress")):
            machine = FakeMac(**standing)
            status = self.run_once(machine)
            self.assertEqual(status["result"], "held", standing)
            self.assertIn(words, status["said"])
            self.assertEqual(machine.calls, ["fetch"], standing)

    def test_a_failed_fetch_or_merge_is_a_failure(self):
        machine = FakeMac()
        machine.fetch_error = "Could not resolve host: github.com"
        status = self.run_once(machine)
        self.assertEqual(status["result"], "failed")
        self.assertIn("git fetch failed (Could not resolve host: github.com); main is 3 commit(s) behind",
                      status["said"])
        self.assertEqual(machine.calls, ["fetch"])
        machine = FakeMac()
        machine.merge_error = "fatal: Not possible to fast-forward, aborting."
        status = self.run_once(machine)
        self.assertEqual((status["result"], status["head"], status["behind"]), ("failed", "aaaaaaaa", 3))

    def test_board_changed_with_the_editor_open_reloads_it(self):
        machine = FakeMac(incoming=[BOARD, "scripts/x.py"])
        machine.open = {BOARD}
        machine.window_titles = [BOARD_TITLE, ""]
        status = self.run_once(machine, by="tidy")
        self.assertEqual(status["result"], "forwarded")
        self.assertEqual(machine.calls, ["fetch", "titles", ("fast_forward", NEW), "tell_page", "reload_board"])
        self.assertEqual(status["pending"], [])
        self.assertIn("reloaded board/demo.kicad_pcb in the PCB Editor (status=reloaded", status["said"])

    def test_a_board_kicad_has_open_waits_when_the_titles_cannot_be_read(self):
        # The runner's python3 has no Screen Recording: every title comes back empty, so unsaved edits cannot be seen.
        machine = FakeMac(incoming=[BOARD])
        machine.open = {BOARD}
        machine.window_titles = ["", "", ""]
        status = self.run_once(machine)
        self.assertEqual(status["result"], "held")
        self.assertIn("they change board/demo.kicad_pcb, which KiCad has open; this run cannot read KiCad's window "
                      "titles (this process has no Screen Recording)", status["said"])
        self.assertIn("left alone for a run over SSH (scripts/mac.py sync, which the pusher and tidy run)",
                      status["said"])
        self.assertEqual(machine.calls, ["fetch", "titles"])

    def test_unsaved_edits_in_kicad_hold_a_change_to_a_file_it_has_open(self):
        machine = FakeMac(incoming=[SCHEMATIC])
        machine.open = {SCHEMATIC}
        machine.window_titles = ["*demo — Schematic Editor"]
        status = self.run_once(machine, by="tidy")
        self.assertEqual(status["result"], "held")
        self.assertIn("KiCad has unsaved edits (*demo — Schematic Editor): left alone", status["said"])
        self.assertNotIn(("fast_forward", NEW), machine.calls)

    def test_unsaved_edits_elsewhere_do_not_hold_a_change_kicad_has_no_file_of(self):
        machine = FakeMac(incoming=["scripts/x.py"])
        machine.window_titles = ["*demo — PCB Editor"]  # a branch's board in a worktree, say
        self.assertEqual(self.run_once(machine)["result"], "forwarded")
        self.assertEqual(machine.calls, ["fetch", ("fast_forward", NEW), "tell_page"])

    def test_a_refused_reload_stays_pending_until_a_run_that_can_read_the_titles_does_it(self):
        machine = FakeMac(incoming=[BOARD, SCHEMATIC])
        machine.open = {BOARD, SCHEMATIC}
        machine.window_titles = [BOARD_TITLE, "demo — Schematic Editor"]
        machine.reload = (False, "status=refused reason=Live mode requires exactly one running KiCad app; found 2")
        status = self.run_once(machine, by="tidy")
        self.assertEqual(status["result"], "forwarded")
        self.assertEqual(machine.calls[-2:], ["reload_board", "revert_schematic"])
        self.assertEqual([item["file"] for item in status["pending"]], [BOARD])
        self.assertIn("reload of board/demo.kicad_pcb in the PCB Editor refused or failed (status=refused", status["said"])
        self.assertIn("reloaded board/demo.kicad_sch in the Schematic Editor (reverted through File > Revert",
                      status["said"])

        # The workflow's next run: level, and it cannot read the titles, so the reload waits and says so.
        later = FakeMac(behind=0, head=NEW, incoming=[])
        later.open = {BOARD}
        later.window_titles = ["", ""]
        status = self.run_once(later, previous=json.loads(json.dumps(status)))
        self.assertEqual(status["result"], "level")
        self.assertEqual(len(status["pending"]), 1)
        self.assertIn("reload of board/demo.kicad_pcb in the PCB Editor pending since 2026-11-06T02:00:00Z", status["said"])
        self.assertNotIn("reload_board", later.calls)

        # Tidy's run over SSH reads the titles and the reload goes through.
        ssh = FakeMac(behind=0, head=NEW, incoming=[])
        ssh.open, ssh.window_titles = {BOARD}, [BOARD_TITLE]
        status = self.run_once(ssh, by="tidy", previous=status)
        self.assertEqual((status["result"], status["pending"]), ("level", []))
        self.assertIn("reloaded board/demo.kicad_pcb", status["said"])

    def test_a_pending_reload_is_dropped_once_kicad_closes_the_file(self):
        machine = FakeMac(behind=0, incoming=[])
        previous = {"pending": [{"file": BOARD, "since": AT, "why": "changed by this fast-forward"}]}
        status = self.run_once(machine, previous=previous)
        self.assertEqual(status["pending"], [])
        self.assertIn("board/demo.kicad_pcb: no reload needed, KiCad no longer has it open", status["said"])
        self.assertNotIn("titles", machine.calls)

    def test_another_open_file_is_named_not_reloaded(self):
        machine = FakeMac(incoming=["board/demo.kicad_pro"])
        machine.open = {"board/demo.kicad_pro"}
        machine.window_titles = ["demo — KiCad 10.0"]
        status = self.run_once(machine, by="tidy")
        self.assertEqual((status["result"], status["pending"]), ("forwarded", []))
        self.assertIn("board/demo.kicad_pro changed and KiCad has it open: reload it there", status["said"])

    def test_a_dry_run_changes_nothing(self):
        machine = FakeMac(incoming=[BOARD])
        machine.open, machine.window_titles = {BOARD}, [BOARD_TITLE]
        status = self.run_once(machine, by="tidy", dry_run=True)
        self.assertEqual(status["result"], "would_forward")
        self.assertEqual(status["said"], "would fast-forward main by 3 commit(s), aaaaaaaa to bbbbbbbb, then reload "
                                         "the PCB Editor for board/demo.kicad_pcb")
        self.assertEqual(machine.calls, ["fetch", "titles"])


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.logs = pathlib.Path(self.directory.name) / "logs" / "mac-keep-pace"

    def tearDown(self):
        self.directory.cleanup()

    def test_each_run_appends_one_line_and_replaces_the_status(self):
        first = keep_pace(FakeMac(), "push", at=AT)
        mac_keep_pace.write(self.logs, first)
        second = keep_pace(FakeMac(behind=0, head=NEW, incoming=[]), "tidy", previous=first, at="2026-11-06T02:02:00Z")
        mac_keep_pace.write(self.logs, second)
        lines = (self.logs / "keep-pace.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, [
            "2026-11-06T02:00:00Z push forwarded bbbbbbbb: fast-forwarded main by 3 commit(s), aaaaaaaa to bbbbbbbb; "
            "the page was told",
            "2026-11-06T02:02:00Z tidy level bbbbbbbb: main is level with GitHub's main"])
        status = mac_keep_pace.read_status(self.logs)
        self.assertEqual((status["at"], status["by"], status["result"]), ("2026-11-06T02:02:00Z", "tidy", "level"))
        self.assertNotIn("interval_s", status, "nothing runs on an interval")
        self.assertEqual(sorted(path.name for path in self.logs.iterdir()), ["keep-pace.log", "status.json"])

    def test_a_long_log_moves_aside_once_past_its_limit(self):
        status = keep_pace(FakeMac(behind=0, incoming=[]), "workflow", at=AT)
        with mock.patch.object(mac_keep_pace, "LOG_LIMIT", 100):
            for _ in range(3):
                mac_keep_pace.write(self.logs, status)
        self.assertEqual(len((self.logs / "keep-pace.log").read_text().splitlines()), 1)
        self.assertEqual(len((self.logs / "keep-pace.log.1").read_text().splitlines()), 2)

    def test_a_missing_or_broken_status_reads_as_none(self):
        self.assertEqual(mac_keep_pace.read_status(self.logs), {})
        self.logs.mkdir(parents=True)
        (self.logs / "status.json").write_text("{not json")
        self.assertEqual(mac_keep_pace.read_status(self.logs), {})

    def test_off_the_mac_it_refuses(self):
        if sys.platform == "darwin":
            self.skipTest("this is the Mac")
        said = io.StringIO()
        with redirect_stdout(said):
            self.assertEqual(mac_keep_pace.main([]), 2)
        self.assertIn("refused: this script keeps the Mac's checkout", said.getvalue())


class KiCadProcessTests(unittest.TestCase):
    def test_editors_count_and_kicad_cli_and_kicads_python_do_not(self):
        import re
        pattern = re.compile(mac_keep_pace.KICAD_EDITORS)
        bundle = "/Applications/KiCad/KiCad.app/Contents"
        for command, editor in ((f"{bundle}/MacOS/kicad", True), (f"{bundle}/MacOS/kicad /Users/x/demo.kicad_pro", True),
                                (f"{bundle}/Applications/pcbnew.app/Contents/MacOS/pcbnew board/demo.kicad_pcb", True),
                                (f"{bundle}/MacOS/kicad-cli pcb drc", False),
                                (f"{bundle}/Frameworks/Python.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/"
                                 "Python scripts/kicad_demo_electrical_audit.py", False)):
            self.assertEqual(bool(pattern.search(command)), editor, command)


def git(cwd, *args):
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if done.returncode:
        raise AssertionError(f"git {' '.join(args)}: {done.stderr}")
    return done.stdout


class CheckoutTests(unittest.TestCase):
    """Checkout's git reads and its fast-forward, against a temporary origin: no GitHub, no Mac."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        base = pathlib.Path(cls.directory.name)
        cls.origin, cls.mac, cls.other = base / "origin.git", base / "mac", base / "other"
        git(base, "init", "-q", "--bare", "-b", "main", str(cls.origin))
        git(base, "init", "-q", "-b", "main", str(cls.other))
        git(cls.other, "remote", "add", "origin", str(cls.origin))
        cls.configure(cls.other)
        (cls.other / "board").mkdir()
        (cls.other / "board" / "demo.kicad_pcb").write_text("(kicad_pcb)\n")
        (cls.other / "README.md").write_text("one\n")
        git(cls.other, "add", "-A")
        git(cls.other, "commit", "-q", "-m", "one")
        git(cls.other, "push", "-q", "origin", "main")
        git(base, "clone", "-q", "-b", "main", str(cls.origin), str(cls.mac))
        cls.configure(cls.mac)
        (cls.other / "board" / "demo.kicad_pcb").write_text("(kicad_pcb (moved))\n")
        (cls.other / "a file with spaces.md").write_text("two\n")
        git(cls.other, "add", "-A")
        git(cls.other, "commit", "-q", "-m", "two")
        git(cls.other, "push", "-q", "origin", "main")

    @staticmethod
    def configure(where):
        git(where, "config", "user.email", "test@example.com")
        git(where, "config", "user.name", "test")
        git(where, "config", "core.autocrlf", "false")

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_standing_open_files_and_the_fast_forward(self):
        checkout = Checkout(self.mac)
        self.assertIsNone(checkout.fetch())
        s = checkout.standing()
        self.assertEqual((s["branch"], s["ahead"], s["behind"], s["busy"], s["dirty"]), ("main", 0, 1, None, []))
        self.assertEqual(sorted(s["incoming"]), ["a file with spaces.md", BOARD])

        (self.mac / "README.md").write_text("edited\n")
        (self.mac / "untracked.txt").write_text("git protects this\n")
        self.assertEqual(checkout.standing()["dirty"], ["README.md"])
        git(self.mac, "checkout", "-q", "--", "README.md")

        lock = self.mac / "board" / "~demo.kicad_pcb.lck"
        lock.write_text("{}")
        with mock.patch.object(Checkout, "kicad_running", lambda self: True):
            self.assertEqual(checkout.open_in_kicad(s["incoming"]), [BOARD])
        with mock.patch.object(Checkout, "kicad_running", lambda self: False):
            self.assertEqual(checkout.open_in_kicad(s["incoming"]), [], "a lock with no KiCad running is stale")
        lock.unlink()

        rebase = pathlib.Path(git(self.mac, "rev-parse", "--absolute-git-dir").strip()) / "rebase-merge"
        rebase.mkdir()
        self.assertEqual(checkout.standing()["busy"], "a rebase")
        rebase.rmdir()

        self.assertIsNone(checkout.fast_forward(s["target"]))
        after = checkout.standing()
        self.assertEqual((after["head"], after["behind"], after["incoming"]), (s["target"], 0, []))
        self.assertEqual((self.mac / "board" / "demo.kicad_pcb").read_text(), "(kicad_pcb (moved))\n")


class SyncTests(unittest.TestCase):
    """`mac.py sync`, run by a pusher after its push to main and by tidy, is one run of the script over SSH, its
    line printed."""

    def test_sync_runs_the_script_on_the_mac_and_prints_its_line(self):
        sent = []

        def fake_ssh(program, stdin="", timeout=120):
            sent.append(stdin)
            return subprocess.CompletedProcess(program, 0, b"forwarded bbbbbbbb: fast-forwarded main by 3 commit(s)\n", b"")

        said = io.StringIO()
        with mock.patch.object(mac, "ssh", fake_ssh), redirect_stdout(said):
            mac.sync(True)
            mac.sync(False, by="tidy")
        self.assertIn("python3 scripts/mac_keep_pace.py --by push\n", sent[0])
        self.assertIn("python3 scripts/mac_keep_pace.py --by tidy --dry-run\n", sent[1])
        self.assertEqual(said.getvalue().splitlines()[0],
                         "Mac: keep-pace forwarded bbbbbbbb: fast-forwarded main by 3 commit(s)")

    def test_a_job_that_did_not_run_says_so(self):
        def fake_ssh(program, stdin="", timeout=120):
            return subprocess.CompletedProcess(program, 2, b"", b"python3: can't open file 'scripts/mac_keep_pace.py'\n")

        said = io.StringIO()
        with mock.patch.object(mac, "ssh", fake_ssh), redirect_stdout(said):
            mac.sync(True)
        self.assertEqual(said.getvalue().strip(),
                         "Mac: keep-pace did not run (exit 2): python3: can't open file 'scripts/mac_keep_pace.py'")


# A checkout just fast-forwarded to a commit whose page server needs a `mac` module this process never loaded.
NEW_MAC = 'SITE = "the new commit\'s settings"\n'
NEW_SERVE = """import pathlib, sys
import mac
assert mac.SITE == "the new commit's settings", "the page server ran beside an older mac module"
if "--notify" in sys.argv:
    pathlib.Path("told.txt").write_text(str(pathlib.Path(mac.__file__).resolve()), encoding="utf-8")
    sys.exit(0)
"""


class FreshProcessTests(unittest.TestCase):
    """After a fast-forward, a run's steps are the new commit's code in a fresh process, never an import into this
    process, whose modules are the commit before's. The sync run of 9367d3bc failed so: `module 'mac' has no
    attribute 'SITE'`."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "mac.py").write_text(NEW_MAC, encoding="utf-8")

    def test_the_page_is_told_by_the_new_commits_page_server(self):
        (self.root / "scripts" / "board_graph_serve.py").write_text(NEW_SERVE, encoding="utf-8")
        self.assertIsNone(Checkout(self.root).tell_page())
        self.assertEqual(pathlib.Path((self.root / "told.txt").read_text(encoding="utf-8")),
                         (self.root / "scripts" / "mac.py").resolve())

    def test_a_page_server_that_was_not_told_says_why(self):
        (self.root / "scripts" / "board_graph_serve.py").write_text('print("HTTP 403")\nraise SystemExit(1)\n',
                                                                     encoding="utf-8")
        self.assertEqual(Checkout(self.root).tell_page(), "HTTP 403")

    def test_the_schematic_is_reverted_by_the_new_commits_script(self):
        (self.root / "scripts" / "mac_keep_pace.py").write_text(
            'import json, sys\nassert sys.argv[1:] == ["--revert-schematic"]\n'
            'print(json.dumps([True, "reverted by the new commit"]))\n', encoding="utf-8")
        self.assertEqual(Checkout(self.root).revert_schematic(), (True, "reverted by the new commit"))
        (self.root / "scripts" / "mac_keep_pace.py").write_text('raise SystemExit("broken")\n', encoding="utf-8")
        reverted, words = Checkout(self.root).revert_schematic()
        self.assertFalse(reverted)
        self.assertIn("broken", words)

    def test_nothing_is_imported_once_a_run_has_started(self):
        import ast
        tree = ast.parse(pathlib.Path(mac_keep_pace.__file__).read_text(encoding="utf-8"))
        late = [ast.unparse(node) for function in ast.walk(tree)
                if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
                for node in ast.walk(function) if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertEqual(late, ["import fcntl"], "only the lock's fcntl, a standard module, is imported in a function")


class ExcludeTests(unittest.TestCase):
    def test_write_auto_excludes_mac_keep_pace_logs_in_git_info_exclude(self):
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td) / "repo"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "test@example.com")
            git(repo, "config", "user.name", "test")
            (repo / ".gitignore").write_text("logs/board-graph/\n", encoding="utf-8")
            git(repo, "add", ".gitignore")
            git(repo, "commit", "-q", "-m", "ignore board-graph logs only")

            logs_dir = repo / "logs" / "mac-keep-pace"
            first = keep_pace(FakeMac(), "push", at=AT)
            mac_keep_pace.write(logs_dir, first)
            (logs_dir / "run.lock").write_text("", encoding="utf-8")
            mac_keep_pace.write(logs_dir, first)

            exclude = repo / ".git" / "info" / "exclude"
            lines = [line.strip() for line in exclude.read_text(encoding="utf-8").splitlines() if line.strip() == "logs/mac-keep-pace/"]
            self.assertEqual(lines, ["logs/mac-keep-pace/"])
            self.assertEqual((repo / ".gitignore").read_text(encoding="utf-8"), "logs/board-graph/\n")
            self.assertEqual(git(repo, "status", "--porcelain"), "")

            sub = repo / "nested"
            sub.mkdir()
            self.assertFalse(mac_keep_pace.ensure_excluded(sub))


if __name__ == "__main__":
    unittest.main()
