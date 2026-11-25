#!/usr/bin/env python3
"""The board checks run by hand: a checkout that is not exactly its commit is refused, the checks run with their
reports outside the checkout, a run that changed the checkout keeps nothing, and the result is kept on the graph
under the commit, as a workflow's last step keeps it."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone

SCRIPTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
# Fixture machine settings, which board_graph.py reads at import in the last test.
os.environ["BOARD_GRAPH_SITE"] = str(SCRIPTS / "fixtures" / "board_graph_site.json")
# The tests read every checkout as a repository with one board: a project's own list of boards (scripts/boards.py)
# is not read here, nor by the board_graph.py the last test starts. The file named is not there.
os.environ["BOARD_GRAPH_BOARDS"] = str(SCRIPTS / "fixtures" / "no_boards.json")
import run_board_checks  # noqa: E402
from no_window import KWARGS as NO_WINDOW  # noqa: E402

AT = datetime(2026, 11, 11, 20, 30, 5, tzinfo=timezone.utc)
SUMMARY = {"pass": True, "checks": [
    {"check": "Board rules check, DRC (G2), zones refilled", "ok": True, "detail": "0 copper violations", "open": 0},
    {"check": "Routing progress", "ok": True, "detail": "none open"}]}


def git(root, *args):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="test",
               GIT_AUTHOR_EMAIL="test@example.com", GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.com")
    done = subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=str(root), capture_output=True, text=True,
                          env=env, check=False, **NO_WINDOW)
    if done.returncode:
        raise AssertionError(done.stderr)
    return done.stdout.strip()


class RunTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name) / "project"
        self.out = pathlib.Path(temp.name) / "reports"
        (self.root / "board").mkdir(parents=True)
        (self.root / "board" / "x.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("*.kicad_prl\n", encoding="utf-8")
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "a board")
        self.head = git(self.root, "rev-parse", "HEAD")
        self.calls, self.said = [], []

    def call(self, code=0, summary=SUMMARY, during=None):
        """Stands in for the two scripts: validate_board.py writes `summary` (None: it died first) and exits `code`."""
        def run(command, cwd):
            self.calls.append([str(part) for part in command[1:]])
            if command[1].endswith("validate_board.py"):
                out = pathlib.Path(command[command.index("--out") + 1])
                out.mkdir(parents=True, exist_ok=True)
                if summary is not None:
                    (out / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
                if during:
                    during()
                return types.SimpleNamespace(returncode=code)
            return types.SimpleNamespace(returncode=0)
        return run

    def run_checks(self, **options):
        return run_board_checks.run(self.root, out=self.out, say=self.said.append, at=AT, **options)

    def test_a_clean_checkout_runs_the_checks_and_keeps_the_result_under_its_commit(self):
        self.assertEqual(self.run_checks(call=self.call()), 0)
        validate, keep = self.calls
        self.assertEqual(validate, [str(self.root / "scripts" / "validate_board.py"), "--out", str(self.out)])
        self.assertEqual(keep, [str(self.root / "scripts" / "board_graph.py"), "checks", "--commit", self.head, "--run",
                                "local-20261111T203005Z", "--event", "local", "--summary",
                                str(self.out / "summary.json")])
        self.assertEqual(self.said, [f"Reports: {self.out}"])

    def test_a_failed_check_is_kept_too_and_exits_1(self):
        self.assertEqual(self.run_checks(call=self.call(code=1, summary=dict(SUMMARY, **{"pass": False}))), 1)
        self.assertEqual(len(self.calls), 2)

    def test_the_base_the_description_and_the_database_are_passed_on(self):
        state = self.root.parent / "scratch.sqlite3"
        self.assertEqual(self.run_checks(call=self.call(), base="origin/main", description="board/other.json",
                                         state=state), 0)
        validate, keep = self.calls
        self.assertEqual(validate[3:], ["--base", "origin/main", "--board-description", "board/other.json"])
        self.assertEqual(keep[1:4], ["--state", str(state), "checks"])

    def test_a_checkout_that_is_not_exactly_its_commit_is_refused_before_anything_runs(self):
        (self.root / "board" / "x.kicad_pcb").write_text("(kicad_pcb (edited))\n", encoding="utf-8")
        (self.root / "board" / "new.kicad_dru").write_text("(version 1)\n", encoding="utf-8")
        (self.root / "board" / "x.kicad_prl").write_text("{}", encoding="utf-8")  # ignored: kicad-cli writes it
        self.assertEqual(self.run_checks(call=self.call()), 2)
        self.assertEqual(self.calls, [])
        self.assertIn(f"refused: this checkout is not exactly {self.head[:8]}", self.said[0])
        self.assertEqual(sorted(line.split()[-1] for line in self.said[1:]),
                         ["board/new.kicad_dru", "board/x.kicad_pcb"])

    def test_an_ignored_file_does_not_refuse(self):
        (self.root / "board" / "x.kicad_prl").write_text("{}", encoding="utf-8")
        self.assertEqual(self.run_checks(call=self.call()), 0)

    def test_untracked_mac_keep_pace_logs_do_not_refuse_when_only_board_graph_logs_ignored(self):
        (self.root / ".gitignore").write_text("*.kicad_prl\nlogs/board-graph/\n", encoding="utf-8")
        git(self.root, "add", ".gitignore")
        git(self.root, "commit", "-q", "-m", "ignore only logs/board-graph")
        self.head = git(self.root, "rev-parse", "HEAD")

        kp = self.root / "logs" / "mac-keep-pace"
        kp.mkdir(parents=True)
        (kp / "status.json").write_text('{"result":"level"}\n', encoding="utf-8")
        (kp / "keep-pace.log").write_text("2026-11-22T00:00:00Z push level\n", encoding="utf-8")
        (kp / "run.lock").write_text("", encoding="utf-8")
        self.assertIn("?? logs/", git(self.root, "status", "--porcelain"))

        def touch_keep_pace():
            (kp / "keep-pace.log").write_text("2026-11-22T00:01:00Z push level\n", encoding="utf-8")

        self.assertEqual(self.run_checks(call=self.call(during=touch_keep_pace)), 0)

        (self.root / "logs" / "other.txt").write_text("noise\n", encoding="utf-8")
        self.assertEqual(self.run_checks(call=self.call()), 2)
        self.assertIn("logs/other.txt", self.said[-1])

    def test_a_run_that_changed_the_checkout_keeps_nothing(self):
        def edit():
            (self.root / "board" / "x.kicad_pcb").write_text("(kicad_pcb (refilled))\n", encoding="utf-8")
        self.assertEqual(self.run_checks(call=self.call(during=edit)), 2)
        self.assertEqual(len(self.calls), 1)  # the checks ran; nothing was kept
        self.assertIn("nothing kept: the checkout changed during the run", self.said[0])
        self.assertIn("board/x.kicad_pcb", self.said[0])

    def test_an_older_summary_is_never_kept_for_a_run_that_died(self):
        self.out.mkdir()
        (self.out / "summary.json").write_text(json.dumps(SUMMARY), encoding="utf-8")
        self.assertEqual(self.run_checks(call=self.call(code=1, summary=None)), 1)
        self.assertFalse((self.out / "summary.json").exists())
        self.assertEqual(len(self.calls), 2)  # kept as a run with no checks, which the card reads as a finding

    def test_the_reports_go_outside_the_checkout_by_default(self):
        self.assertEqual(run_board_checks.run(self.root, call=self.call(), say=self.said.append, at=AT), 0)
        out = pathlib.Path(self.calls[0][2])
        self.addCleanup(lambda: [path.unlink() for path in out.glob("*")] and out.rmdir())
        self.assertEqual(out, pathlib.Path(tempfile.gettempdir()) / "board-graph-checks" / self.head[:12])
        self.assertEqual(git(self.root, "status", "--porcelain"), "")

    def test_what_it_keeps_is_what_the_board_checks_card_reads(self):
        """The real `board_graph.py checks` on a scratch database, then the card's own reading of that row."""
        import board_graph
        import board_graph_actions

        state_file = self.root.parent / "state.sqlite3"

        def call(command, cwd):
            if command[1].endswith("validate_board.py"):
                return self.call()(command, cwd)
            self.calls.append([str(part) for part in command[1:]])
            arguments = [str(part) for part in command[2:]]
            return types.SimpleNamespace(returncode=board_graph.main(["--records", str(self.root.parent / "records"),
                                                                      *arguments]))

        with tempfile.TemporaryFile("w+", encoding="utf-8") as quiet:
            stdout, sys.stdout = sys.stdout, quiet
            try:
                code = self.run_checks(call=call, state=state_file)
            finally:
                sys.stdout = stdout
        self.assertEqual(code, 0)
        state = board_graph.State(state_file, self.root.parent / "records")
        row = state.checks_for(self.head)
        self.assertEqual((row["run_id"], row["event"], row["passed"], row["open"]),
                         ("local-20261111T203005Z", "local", True, 0))
        found = board_graph_actions.board_checks(self.root, lookup=state.checks_for)
        self.assertEqual((found["outcome"], found["findings"]), ("pass", []))
        self.assertEqual(found["detail"]["run"]["event"], "local")


if __name__ == "__main__":
    unittest.main()
