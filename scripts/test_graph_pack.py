#!/usr/bin/env python3
"""The pack a graph runs with (scripts/graph_pack.py): the pack file's shapes, the electronics pack as the engine
takes it, and a project that is not a circuit board, with no board description, walked through the tool's own
command line."""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import graph_pack  # noqa: E402
from no_window import KWARGS as NO_WINDOW_KWARGS  # noqa: E402

# A graph with no board on it: one ordered track of three cards, the second with a script of the project's own.
GRAPH = {
    "version": 2, "milestone": "The essay is published", "docs": ["docs/GOAL.md"],
    "stop": {"minutes": 120, "repeat_failures": 3, "set_by": "a loop guard, not a plan of work"},
    "adopted": {"shape": "A stored position per track."},
    "stay_outcomes": {"more_work": "stay", "needs_owner": "stay", "needs_lead": "stay", "needs_skill": "stay"},
    "move_outcomes": {"pass": "move", "returned": "go back"},
    "rules": [],
    "tracks": [{"id": "essay", "label": "Essay", "start": "outline", "ordered": True}],
    "stages": [],
    "nodes": [
        {"id": "outline", "track": "essay", "label": "Outline", "who": "owner", "task": "Write the outline.",
         "pass_when": "The outline names every section.", "owns": ["essay/outline.md"], "action": None,
         "outcomes": {"pass": "words", "returned": []}},
        {"id": "words", "track": "essay", "label": "Word count", "who": "auto", "task": "Count the draft's words.",
         "pass_when": "The draft has at least five words.", "owns": [], "action": "word_count",
         "outcomes": {"pass": "review", "returned": ["outline"]}},
        {"id": "review", "track": "essay", "label": "Review", "who": "agent", "task": "Read the draft.",
         "pass_when": "No finding is left open.", "owns": [], "action": None,
         "outcomes": {"pass": "done", "returned": ["outline"]}}],
    "open_issues": []}

ACTIONS = '''import pathlib


def word_count(root):
    """The draft has at least five words."""
    words = len((pathlib.Path(root) / "essay/draft.md").read_text(encoding="utf-8").split())
    findings = [] if words >= 5 else [f"the draft has {words} words, fewer than five"]
    return {"outcome": "needs_lead" if findings else "pass", "findings": findings, "detail": {"words": words}}


ACTIONS = {"word_count": (word_count, ["essay/draft.md"])}
'''


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class PackFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def read(self, data):
        write(self.root / graph_pack.DEFAULT, data if isinstance(data, str) else json.dumps(data))
        return graph_pack.read(self.root, {})

    def test_a_project_with_no_pack_file_runs_the_electronics_pack(self):
        self.assertEqual(graph_pack.read(self.root, {}), {"pack": "electronics"})

    def test_another_file_can_be_named(self):
        write(self.root / "elsewhere.json", json.dumps({"pack": "none", "reports": "out/"}))
        self.assertEqual(graph_pack.read(self.root, {graph_pack.ENV: "elsewhere.json"}),
                         {"pack": "none", "reports": "out", "branch_folders": [], "written": []})

    def test_the_none_pack_gives_its_own_folders(self):
        found = self.read({"pack": "none", "reports": "reports", "branch_folders": ["essay/"], "written": ["build"]})
        self.assertEqual(found, {"pack": "none", "reports": "reports", "branch_folders": ["essay"],
                                 "written": ["build"]})
        self.assertIsNone(graph_pack.load(self.root, {}).notes_path("any"))
        pack = graph_pack.load(self.root, {})
        self.assertEqual((pack.name, pack.reports, pack.branch_folders, pack.written, pack.module),
                         ("none", "reports", ("essay",), ("build",), None))
        self.assertEqual((pack.actions, pack.later, pack.commands, pack.views, pack.view_file), ({}, (), {}, {}, None))

    def test_the_none_pack_runs_the_projects_own_scripts(self):
        write(self.root / graph_pack.PROJECT_ACTIONS, ACTIONS)
        self.read({"pack": "none", "reports": "reports"})
        pack = graph_pack.load(self.root, {})
        self.assertEqual(list(pack.actions), ["word_count"])
        self.assertEqual(pack.actions["word_count"].reads, ("essay/draft.md",))

    def test_each_graph_has_its_own_reports_and_is_named_to_the_projects_scripts(self):
        pack = graph_pack.Pack(graph_pack.NONE, "reports")
        self.assertEqual((pack.reports_for(), pack.reports_for("essay", first=True),
                          pack.reports_for("talk", first=False)), ("reports", "reports", "reports.talk"))
        named = graph_pack.Pack(graph_pack.NONE, "reports/{name}")
        self.assertEqual((named.reports_for("essay", first=True), named.reports_for("talk", first=False)),
                         ("reports/essay", "reports/talk"))
        with self.assertRaisesRegex(graph_pack.PackError, "this project lists no graphs"):
            named.reports_for()
        # A board's reports folder is the one its description names, whichever board it is.
        board = graph_pack.Pack(graph_pack.ELECTRONICS, "motor/reports")
        self.assertEqual(board.reports_for("motor", first=False), "motor/reports")

        def plain(root):
            return ("plain", root)

        def told(root, graph=None):
            return ("told", root, graph)

        pack = graph_pack.Pack(graph_pack.NONE, "reports", actions={
            "plain": graph_pack.Action(plain, ("draft.md",)), "told": graph_pack.Action(told, ("{name}/draft.md",))})
        actions = pack.actions_for("talk")
        self.assertEqual((actions["plain"].reads, actions["told"].reads), (("draft.md",), ("talk/draft.md",)))
        self.assertIs(actions["told"].run, told)
        self.assertEqual(graph_pack.run_action(plain, "root", "talk"), ("plain", "root"))
        self.assertEqual(graph_pack.run_action(told, "root", "talk"), ("told", "root", "talk"))
        with self.assertRaisesRegex(ValueError, "the action told reads a path with {name}"):
            pack.actions_for()

    def test_a_wrong_pack_file_is_refused_in_a_sentence(self):
        for data, said in (("{", "is not JSON"),
                           ({"pack": "plumbing"}, "pack must be one of electronics, none"),
                           ({"pack": "none"}, "reports is missing"),
                           ({"pack": "none", "reports": "/abs"}, "reports must be a path relative"),
                           ({"pack": "none", "reports": "../out"}, "reports must be a path relative"),
                           ({"pack": "none", "reports": "r", "branch_folders": "essay"}, "must be a list"),
                           ({"pack": "none", "reports": "r", "actions": "../bad.py"}, "actions must be a path relative"),
                           ({"pack": "none", "reports": "r", "upkeep": "remote"}, "upkeep must be 'local'"),
                           ({"pack": "none", "reports": "r", "folders": []}, "unknown key folders"),
                           ({"pack": "electronics", "reports": "r"}, "reports cannot be set here")):
            with self.subTest(data=data), self.assertRaisesRegex(graph_pack.PackError, said):
                self.read(data)

    def test_the_none_pack_loads_custom_actions_upkeep_and_approval_sources(self):
        write(
            self.root / "dev" / "custom_actions.py",
            ACTIONS + "\ndef fp(root):\n    return {'sha': 'ok'}\nAPPROVAL_SOURCES = {'tool_code': fp}\n",
        )
        found = self.read({"pack": "none", "reports": "logs/reports", "actions": "dev/custom_actions.py", "upkeep": "local"})
        self.assertEqual(found["actions"], "dev/custom_actions.py")
        self.assertEqual(found["upkeep"], "local")
        pack = graph_pack.load(self.root, {})
        self.assertEqual(pack.upkeep, "local")
        self.assertIn("word_count", pack.actions)
        self.assertIn("tool_code", pack.approval_sources)
        self.assertEqual(pack.approval_sources["tool_code"](self.root), {"sha": "ok"})


class ElectronicsPackTests(unittest.TestCase):
    def test_the_engine_holds_what_the_electronics_pack_gives(self):
        environ = dict(os.environ)
        os.environ["BOARD_GRAPH_BOARDS"] = str(SCRIPTS / "fixtures" / "no_boards.json")
        try:
            import board_graph
            import board_graph_actions
        finally:
            os.environ.clear()
            os.environ.update(environ)
        pack = board_graph.PACK
        self.assertEqual((pack.name, pack.module), ("electronics", "scripts/board_graph_actions.py"))
        self.assertIs(board_graph.ACTIONS, board_graph_actions.ACTIONS)
        self.assertIs(board_graph.VIEWS, board_graph_actions.VIEWS)
        self.assertEqual((board_graph.LATER, board_graph.COMMANDS, board_graph.VIEW_FILE, board_graph.REPORTS),
                         (board_graph_actions.LATER, board_graph_actions.COMMANDS, board_graph_actions.BOARD,
                          board_graph_actions.REPORTS))
        self.assertIn(board_graph_actions.DESCRIPTION.rel("folder"), board_graph.DESIGN_FOLDERS)


class NoBoardTests(unittest.TestCase):
    """A scratch repository whose graph is about an essay: no board description, no KiCad file, the pack "none".
    It holds a copy of these scripts, since each script works on the repository it sits in. Its site settings name
    no KiCad machine, so it runs on this machine alone: the database is a file in the repository, made by the
    first command that writes, and nothing is reached over SSH."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = pathlib.Path(cls.temp.name).resolve()
        cls.root = cls.base / "repo"
        shutil.copytree(SCRIPTS, cls.root / "scripts",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.ps1", "hooks", "fixtures"))
        write(cls.root / "tools/board_graph.json", json.dumps(GRAPH, indent=2) + "\n")
        write(cls.root / graph_pack.DEFAULT, json.dumps({"pack": "none", "reports": "reports",
                                                         "branch_folders": ["essay"]}) + "\n")
        write(cls.root / graph_pack.PROJECT_ACTIONS, ACTIONS)
        write(cls.root / "tools/board_graph_site.json", json.dumps({"one_machine": True,
                                                                         "state_database": "logs/graph/state.sqlite3"}))
        for name, text in (("AGENTS.md", "# Agents\n"), ("CLAUDE.md", "# Claude\n"), ("docs/GOAL.md", "# Goal\n"),
                           ("essay/outline.md", "# Outline\n"), ("essay/draft.md", "one two three\n"),
                           (".gitignore", "logs/\n__pycache__/\nview.json\n")):
            write(cls.root / name, text)
        cls.env = {key: value for key, value in os.environ.items()
                   if not key.startswith("BOARD_GRAPH_") and key != "GIT_DIR"}
        cls.env.update(PYTHONDONTWRITEBYTECODE="1", GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.invalid",
                       GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.invalid",
                       GIT_TERMINAL_PROMPT="0")
        cls.git("init", "-q", "--bare", "-b", "main", str(cls.base / "origin.git"), cwd=cls.base)
        cls.git("init", "-q", "-b", "main")
        cls.git("add", "-A")
        cls.git("commit", "-q", "-m", "An essay")
        cls.git("remote", "add", "origin", str(cls.base / "origin.git"))
        cls.git("push", "-q", "-u", "origin", "main")
        cls.state = cls.root / "logs/graph/state.sqlite3"

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def git(cls, *args, cwd=None):
        done = subprocess.run(["git", *args], cwd=str(cwd or cls.root), capture_output=True, env=cls.env,
                              **NO_WINDOW_KWARGS)
        assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
        return done.stdout.decode("utf-8", "replace")

    @classmethod
    def graph(cls, *argv):
        done = subprocess.run([sys.executable, str(cls.root / "scripts" / "board_graph.py"), *argv],
                              cwd=str(cls.root), capture_output=True, env=cls.env, **NO_WINDOW_KWARGS)
        return (done.returncode, done.stdout.decode("utf-8", "replace").replace("\r\n", "\n"),
                done.stderr.decode("utf-8", "replace").replace("\r\n", "\n"))

    def test_1_the_graph_checks_and_shows_its_position_with_no_board(self):
        self.assertFalse((self.root / "board").exists())
        code, out, err = self.graph("check")
        self.assertEqual(code, 0, out + err)
        self.assertIn("3 nodes on 1 tracks, consistent", out)
        code, out, err = self.graph("where")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Essay: before Outline (owner), the track's start", out)
        self.assertIn(f"State: {self.state}, the one database, on this machine", out)
        self.assertNotIn("Mac", out)

    def test_2_the_cards_are_walked_and_the_projects_script_decides_its_card(self):
        code, out, err = self.graph("next", "--track", "essay", "--by", "owner")
        self.assertEqual(code, 2, out + err)  # 2: a person's card was started
        code, out, err = self.graph("record", "outline", "pass", "--by", "owner", "--note", "outlined",
                                   "--evidence", "essay/outline.md",
                                   "--attempt", re.search(r"[0-9a-f]{32}", out + err).group(0))
        self.assertEqual(code, 0, out + err)
        # The project's script finds the draft short: the card stays, with the finding.
        write(self.root / "reports/words/counting.md", "# How the words are counted\n")
        code, out, err = self.graph("next", "--track", "essay")
        self.assertIn("the draft has 3 words, fewer than five", out + err)
        report = json.loads((self.root / "reports/words/word_count.json").read_text(encoding="utf-8"))
        self.assertEqual(report["outcome"], "needs_lead")
        self.assertTrue(self.state.is_file())
        code, out, err = self.graph("where", "--full")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("Mac", out)
        self.assertIn("Essay: between Outline and Word count (auto), waiting on the lead", out)
        self.assertIn("Script: word_count (next runs it)", out)
        # The page's data lists each card's files with no board description: the file the card owns and what its
        # reports folder holds for a person to read (the script's own JSON report is shown with the script).
        view = json.loads((self.root / "tools/board_graph_view/view.json").read_text(encoding="utf-8"))
        cards = view["artifacts"]["cards"]
        self.assertEqual([item["path"] for item in cards["outline"]["items"]], ["essay/outline.md"])
        self.assertEqual(([item["path"] for item in cards["words"]["items"]], cards["words"]["badge"]),
                         (["reports/words/counting.md"], "1 file"))
        self.assertEqual(cards["review"]["items"], [])


class TwoGraphsTests(NoBoardTests):
    """The same scratch repository with a second graph listed beside the essay's, a talk: each has its own cards,
    position and database, and neither is a board."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        talk = json.loads(json.dumps(GRAPH).replace("essay", "talk").replace("Essay", "Talk"))
        write(cls.root / "tools/board_graph.talk.json", json.dumps(talk, indent=2) + "\n")
        write(cls.root / "tools/board_graph_boards.json", json.dumps(
            {"first": "essay", "boards": {"essay": "graphs/essay.json", "talk": "graphs/talk.json"}}))
        for name in ("essay", "talk"):
            write(cls.root / f"graphs/{name}.json", json.dumps({"name": name, "project": "Writing"}))
        write(cls.root / "talk/outline.md", "# Outline\n")
        cls.git("add", "-A")
        cls.git("commit", "-q", "-m", "A talk")
        cls.git("push", "-q")

    def test_1_the_graph_checks_and_shows_its_position_with_no_board(self):
        code, out, err = self.graph("where")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Graph essay, graphs/essay.json:", out)
        self.assertIn("Graph talk, graphs/talk.json:", out)
        self.assertIn("Talk: before Outline (owner), the track's start", out)
        self.assertNotIn("Board ", out)

    def test_2_the_cards_are_walked_and_the_projects_script_decides_its_card(self):
        code, out, err = self.graph("next", "--track", "talk", "--by", "owner")
        self.assertEqual(code, 2, out + err)
        self.assertIn("this repository has 2 graphs (essay, talk); name the one", out + err)
        code, out, err = self.graph("--board", "talk", "next", "--track", "talk", "--by", "owner")
        self.assertEqual(code, 2, out + err)
        code, out, err = self.graph("--board", "talk", "record", "outline", "pass", "--by", "owner", "--note", "done",
                                    "--evidence", "talk/outline.md",
                                    "--attempt", re.search(r"[0-9a-f]{32}", out + err).group(0))
        self.assertEqual(code, 0, out + err)
        # The project's scripts are one file for every graph: the talk's card runs the same word count.
        write(self.root / "reports.talk/words/counting.md", "# How the words are counted\n")
        code, out, err = self.graph("--board", "talk", "next", "--track", "talk")
        self.assertIn("the draft has 3 words, fewer than five", out + err)
        # Each graph keeps its own reports: the talk's are beside the essay's, which has none yet.
        self.assertTrue((self.root / "reports.talk/words/word_count.json").is_file())
        self.assertFalse((self.root / "reports/words").exists())
        code, out, err = self.graph("--board", "talk", "where")
        self.assertIn("Talk: between Outline and Word count (auto), waiting on the lead", out)
        self.assertIn("the graph talk's database, on this machine", out)
        self.assertTrue((self.root / "logs/graph/talk/state.sqlite3").is_file())
        # The essay has not moved, and the page's data names both graphs and the project.
        code, out, err = self.graph("--board", "essay", "where")
        self.assertIn("Essay: before Outline (owner), the track's start", out)
        view = json.loads((self.root / "tools/board_graph_view/boards/talk/view.json").read_text(encoding="utf-8"))
        self.assertEqual((view["project"], view["board"], [item["name"] for item in view["boards"]]),
                         ("Writing", "talk", ["essay", "talk"]))
        self.assertEqual([item["path"] for item in view["artifacts"]["cards"]["words"]["items"]],
                         ["reports.talk/words/counting.md"])


class RepeatedWorkTests(NoBoardTests):
    """The same scratch repository for work that repeats: the essay's cards are grouped in stages though no track
    is called board, the owner may drop an essay at its outline, and each further essay is a graph of its own made
    with `boards.py new`."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        graph = json.loads(json.dumps(GRAPH))
        graph["move_outcomes"]["dropped"] = "end"
        graph["stages"] = [{"id": "think", "label": "Think"}, {"id": "write", "label": "Write"}]
        for node in graph["nodes"]:
            node["stage"] = "think" if node["id"] == "outline" else "write"
        graph["nodes"][0]["outcomes"]["dropped"] = True
        write(cls.root / "tools/board_graph.json", json.dumps(graph, indent=2) + "\n")
        write(cls.root / "tools/board_graph_boards.json", json.dumps({"first": "essay",
                                                                       "boards": {"essay": "graphs/essay.json"}}))
        write(cls.root / "graphs/essay.json", json.dumps({"name": "The first essay", "project": "Writing"}))
        write(cls.root / graph_pack.DEFAULT, json.dumps({"pack": "none", "reports": "reports",
                                                         "branch_folders": ["essay"], "notes": "notes/{name}.md"}))
        cls.git("add", "-A")
        cls.git("commit", "-q", "-m", "Essays, one graph each")
        cls.git("push", "-q")

    @classmethod
    def boards(cls, *argv):
        done = subprocess.run([sys.executable, str(cls.root / "scripts" / "boards.py"), *argv],
                              cwd=str(cls.root), capture_output=True, env=cls.env, **NO_WINDOW_KWARGS)
        return done.returncode, (done.stdout + done.stderr).decode("utf-8", "replace").replace("\r\n", "\n")

    def test_1_the_graph_checks_and_shows_its_position_with_no_board(self):
        code, out, err = self.graph("check")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.graph("where")
        self.assertEqual(code, 0, out + err)
        self.assertIn("[Think] > Write", out)
        self.assertIn("Essay: before Outline (owner), the track's start, stage Think", out)
        self.assertIn("dropped ends the track, by the owner", out)

    def test_2_the_cards_are_walked_and_the_projects_script_decides_its_card(self):
        # A second essay: the first one's cards, and files, a position and a page of its own.
        code, out = self.boards("new", "second", "--title", "The second essay")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads((self.root / "graphs/second.json").read_text(encoding="utf-8")),
                         {"name": "The second essay", "project": "Writing"})
        self.assertEqual((self.root / "tools/board_graph.second.json").read_bytes(),
                         (self.root / "tools/board_graph.json").read_bytes())
        for argv, said in ((("new", "second"), "second is already listed"), (("new", "Not A Name"), "a name is"),
                           (("new", "third", "--like", "absent"), "no board 'absent' here")):
            code, out = self.boards(*argv)
            self.assertEqual(code, 1, out)
            self.assertIn(said, out)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "A second essay")
        self.git("push", "-q")
        # With no track named, next steps the staged track. Only the owner drops, and a reason is the whole record.
        code, out, err = self.graph("--board", "second", "next", "--by", "owner")
        self.assertEqual(code, 2, out + err)
        attempt = re.search(r"[0-9a-f]{32}", out + err).group(0)
        code, out, err = self.graph("--board", "second", "record", "outline", "dropped", "--by", "agent:someone",
                                    "--note", "no", "--attempt", attempt)
        self.assertNotEqual(code, 0)
        self.assertIn("dropping essay is the owner's to record", out + err)
        code, out, err = self.graph("--board", "second", "record", "outline", "dropped", "--by", "owner",
                                    "--note", "Not worth the time.", "--attempt", attempt)
        self.assertEqual(code, 0, out + err)
        self.assertIn("essay is dropped at outline", out)
        code, out, err = self.graph("--board", "second", "next", "--by", "owner")
        self.assertIn("Essay: dropped; there is no next card", out + err)
        code, out, err = self.graph("where")
        self.assertIn("Essay: dropped at Outline: Not worth the time.", out)
        self.assertIn("Essay: before Outline (owner), the track's start, stage Think", out)  # the first, untouched
        # A card that does not name the outcome refuses it, and the page's data says where each essay stands.
        code, out, err = self.graph("--board", "essay", "next", "--by", "owner")
        code, out, err = self.graph("--board", "essay", "record", "outline", "pass", "--by", "owner", "--note", "ok",
                                    "--evidence", "essay/outline.md",
                                    "--attempt", re.search(r"[0-9a-f]{32}", out + err).group(0))
        self.assertEqual(code, 0, out + err)
        code, out, err = self.graph("--board", "essay", "record", "words", "dropped", "--by", "owner", "--note", "no")
        self.assertIn("words does not allow dropped", out + err)
        view = json.loads((self.root / "tools/board_graph_view/boards/second/view.json").read_text(encoding="utf-8"))
        self.assertEqual(view["state"]["tracks"]["essay"], {"position": "done", "done": True, "started": True,
                                                            "dropped": True})
        self.assertEqual([view["state"]["nodes"][key]["status"] for key in ("outline", "words", "review")],
                         ["ahead", "ahead", "ahead"])
        self.assertEqual(view["where"]["tracks"][0]["dropped"]["label"], "Outline")
        # The pack names a notes file per graph: the view carries its words, and the page's server watches it.
        self.assertEqual(view["notes"], {"path": "notes/second.md", "exists": False, "text": ""})
        write(self.root / "notes/second.md", "The idea, in a sentence.\n")
        code, out, err = self.graph("--board", "second", "where")
        done = subprocess.run([sys.executable, str(self.root / "scripts" / "board_graph_view.py"), "--board", "second",
                               "snapshot", "--quick"], cwd=str(self.root), capture_output=True, env=self.env,
                              **NO_WINDOW_KWARGS)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        view = json.loads((self.root / "tools/board_graph_view/boards/second/view.json").read_text(encoding="utf-8"))
        self.assertEqual(view["notes"], {"path": "notes/second.md", "exists": True,
                                         "text": "The idea, in a sentence.\n"})
        self.assertEqual(view["boards"], [{"name": "essay", "data": "", "title": "The first essay"},
                                          {"name": "second", "data": "boards/second/", "title": "The second essay"}])


if __name__ == "__main__":
    unittest.main()
