#!/usr/bin/env python3
"""Several boards in one repository: the list names them and their files, a repository with no list is as it was,
and two boards walk their first cards with a position and records each."""
import contextlib
import io
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
os.environ.pop("BOARD_GRAPH_BOARDS", None)  # each test here names its own repository, which holds its own list
import boards  # noqa: E402
import mac_keep_pace  # noqa: E402
import run_board_checks  # noqa: E402
from no_window import KWARGS as NO_WINDOW_KWARGS  # noqa: E402

SCRIPTS = pathlib.Path(__file__).resolve().parent
TWO = {"first": "strip", "boards": {"strip": "board/board.json", "driver": "driver/board.json"}}


def write(path, text):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


class ListTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)

    def list(self, data):
        write(self.root / boards.LIST, data if isinstance(data, str) else json.dumps(data))

    def test_a_repository_with_no_list_has_one_board_on_the_files_it_always_had(self):
        self.assertEqual(boards.read(self.root), {})
        only = boards.chosen(self.root, {})
        self.assertEqual((only.name, only.description, only.first), (None, "board/board.json", True))
        self.assertEqual((only.graph, only.records, only.view, only.data),
                         ("tools/board_graph.json", "tools/board_graph_records", "tools/board_graph_view", ""))
        self.assertEqual(only.state("logs/board-graph/state.sqlite3"), "logs/board-graph/state.sqlite3")
        self.assertEqual((only.argument, only.remote_argument), ("", ""))
        moved = boards.chosen(self.root, {boards.ENV: "other/board.json"})  # BOARD_GRAPH_BOARD, as before
        self.assertEqual((moved.name, moved.description, moved.graph), (None, "other/board.json", only.graph))
        self.assertEqual([board.description for board in boards.every(self.root, {})], ["board/board.json"])
        self.assertEqual(boards.unnamed(self.root, {}), [])

    def test_the_first_board_keeps_the_one_board_files_and_another_has_files_named_after_it(self):
        self.list(TWO)
        listed = boards.read(self.root)
        self.assertEqual(list(listed), ["strip", "driver"])
        strip, driver = listed["strip"], listed["driver"]
        self.assertEqual((strip.first, strip.graph, strip.records, strip.view, strip.data),
                         (True, "tools/board_graph.json", "tools/board_graph_records", "tools/board_graph_view", ""))
        self.assertEqual(strip.state("logs/board-graph/state.sqlite3"), "logs/board-graph/state.sqlite3")
        self.assertEqual((driver.first, driver.graph, driver.records, driver.view, driver.data),
                         (False, "tools/board_graph.driver.json", "tools/board_graph_records.driver",
                          "tools/board_graph_view/boards/driver", "boards/driver/"))
        self.assertEqual(driver.state("logs/board-graph/state.sqlite3"), "logs/board-graph/driver/state.sqlite3")
        # A person's command names either board; the Mac is told only the board that is not the first.
        self.assertEqual((strip.argument, strip.remote_argument), (" --board strip", ""))
        self.assertEqual((driver.argument, driver.remote_argument), (" --board driver", " --board driver"))

    def test_a_first_board_outside_the_default_folder_is_the_one_a_script_reads_with_none_named(self):
        import board_description
        self.assertEqual(board_description.find(root=self.root, environ={}), self.root / "board/board.json")
        self.list({"first": "strip", "boards": {"strip": "strip/board.json", "driver": "driver/board.json"}})
        self.assertEqual(board_description.find(root=self.root, environ={}), self.root / "strip/board.json")
        # A board that is named still wins: by the script's own argument, then by BOARD_GRAPH_BOARD.
        named = {boards.ENV: "driver/board.json"}
        self.assertEqual(board_description.find(root=self.root, environ=named), self.root / "driver/board.json")
        self.assertEqual(board_description.find("x/board.json", root=self.root, environ=named),
                         self.root / "x/board.json")

    def test_first_is_the_board_named_not_the_one_listed_first(self):
        self.list({"first": "driver", "boards": TWO["boards"]})
        listed = boards.read(self.root)
        self.assertEqual([name for name, board in listed.items() if board.first], ["driver"])
        self.assertEqual(listed["strip"].graph, "tools/board_graph.strip.json")
        self.assertEqual(boards.chosen(self.root, {}).name, "driver")

    def test_the_board_is_the_one_whose_description_is_named(self):
        self.list(TWO)
        self.assertEqual(boards.chosen(self.root, {}).name, "strip")  # none named: the first
        for given in ("driver/board.json", "driver\\board.json", str(self.root / "driver" / "board.json")):
            self.assertEqual(boards.chosen(self.root, {boards.ENV: given}).name, "driver", given)
        self.assertEqual(boards.chosen(self.root, {boards.ENV: "board/board.json"}).name, "strip")
        scratch = boards.chosen(self.root, {boards.ENV: "tmp/scratch.json"})  # not listed: the first board's files
        self.assertEqual((scratch.name, scratch.first, scratch.description), (None, True, "tmp/scratch.json"))
        self.assertEqual(boards.unnamed(self.root, {}), ["strip", "driver"])
        self.assertEqual(boards.unnamed(self.root, {boards.ENV: "driver/board.json"}), [])

    def test_board_on_the_command_line_names_the_description_for_this_process_and_its_children(self):
        self.list(TWO)
        for argv in (["--board", "driver", "where"], ["next", "--board=driver", "--by", "owner"]):
            environ = {}
            self.assertEqual(boards.take_argument(argv, environ, self.root).name, "driver")
            self.assertEqual(environ, {boards.ENV: "driver/board.json"})
        environ = {}
        self.assertIsNone(boards.take_argument(["where", "--full"], environ, self.root))
        self.assertEqual(environ, {})
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stopped:
            boards.take_argument(["--board", "strap", "where"], {}, self.root)
        self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(err.getvalue(), f"no board 'strap' here: {boards.LIST} lists strip, driver\n")

    def test_a_name_with_no_list_is_refused(self):
        with self.assertRaises(boards.BoardsError) as caught:
            boards.find("driver", self.root)
        self.assertIn("this repository lists no boards", str(caught.exception))

    def test_a_list_that_cannot_be_used_says_what_is_wrong(self):
        for data, words in (
                ("{", "is not JSON"),
                ({"first": "strip"}, "boards must give each board's name its description"),
                ({"boards": TWO["boards"]}, "first must name the board that keeps the files"),
                ({"first": "strap", "boards": TWO["boards"]}, "one of strip, driver"),
                ({"first": "Strip", "boards": {"Strip": "board/board.json"}}, "lower-case letters, digits"),
                ({"first": "a", "boards": {"a": "board/board.json", "b/c": "x.json"}}, "not 'b/c'"),
                ({"first": "a", "boards": {"a": ""}}, "a must name its description"),
                ({"first": "a", "boards": {"a": "/abs/board.json"}}, "must be relative to the repository root"),
                ({"first": "a", "boards": {"a": "board/board.json", "b": "board/board.json"}},
                 "a and b name the same description")):
            self.list(data)
            with self.assertRaises(boards.BoardsError) as caught:
                boards.read(self.root)
            self.assertIn(words, str(caught.exception))
            self.assertTrue(str(caught.exception).startswith(boards.LIST), caught.exception)


class KeepPaceTests(unittest.TestCase):
    """The Mac's keep-pace run follows every listed board: a change to any board's cards or to the list rewrites
    the views, and another board's PCB and schematic are reloaded in KiCad with that board's description."""

    def test_one_board_is_as_it_was(self):
        with tempfile.TemporaryDirectory() as folder:
            graphs, reloaders = mac_keep_pace.every_board(pathlib.Path(folder))
        self.assertEqual(graphs, {"tools/board_graph.json", boards.LIST})
        self.assertEqual(reloaders, {mac_keep_pace.BOARD: ("the PCB Editor", "reload_board", None),
                                     mac_keep_pace.SCHEMATIC: ("the Schematic Editor", "revert_schematic", None)})

    def test_another_boards_files_are_followed_with_its_description(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            write(root / boards.LIST, json.dumps(TWO))
            write(root / "board/board.json", json.dumps(description("board", "strip")))
            write(root / "driver/board.json", json.dumps(description("driver", "driver")))
            graphs, reloaders = mac_keep_pace.every_board(root)
        self.assertEqual(graphs, {"tools/board_graph.json", "tools/board_graph.driver.json", boards.LIST})
        self.assertEqual(reloaders["driver/driver.kicad_pcb"], ("the PCB Editor", "reload_board", "driver/board.json"))
        self.assertEqual(reloaders["driver/driver.kicad_sch"],
                         ("the Schematic Editor", "revert_schematic", "driver/board.json"))
        self.assertIsNone(reloaders[mac_keep_pace.BOARD][2])

    def test_a_pending_reload_of_another_board_is_given_its_description(self):
        calls = []

        class Machine:
            def fetch(self):
                return None

            def standing(self):
                return {"branch": "main", "head": "a" * 40, "target": "a" * 40, "ahead": 0, "behind": 0, "busy": None,
                        "dirty": [], "incoming": []}

            def open_in_kicad(self, paths):
                return paths

            def titles(self):
                return ["driver — PCB Editor"]

            def reload_board(self, description=None):
                calls.append(description)
                return True, "status=reloaded"

        reloaders = dict(mac_keep_pace.RELOADERS,
                         **{"driver/driver.kicad_pcb": ("the PCB Editor", "reload_board", "driver/board.json")})
        previous = {"pending": [{"file": "driver/driver.kicad_pcb", "since": "t", "why": "changed"},
                                {"file": mac_keep_pace.BOARD, "since": "t", "why": "changed"}]}
        with mock.patch.object(mac_keep_pace, "RELOADERS", reloaders):
            status = mac_keep_pace.keep_pace(Machine(), "hand", previous=previous)
        self.assertEqual(calls, ["driver/board.json", None])
        self.assertEqual((status["result"], status["pending"]), ("level", []))


class BoardChecksTests(unittest.TestCase):
    """scripts/run_board_checks.py with several boards: the board named has its description checked and its
    result kept in its own database; with none named, nothing runs."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        write(self.root / boards.LIST, json.dumps(TWO))
        self.said, self.commands = [], []
        patcher = mock.patch.object(run_board_checks, "standing", lambda root: ("c" * 40, []))
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, command, cwd):
        self.commands.append(command[2:])
        return subprocess.CompletedProcess(command, 0, "", "")

    def run_checks(self, **options):
        with mock.patch.dict(os.environ):
            os.environ.pop(boards.ENV, None)
            return run_board_checks.run(self.root, out=self.root / "out", call=self.call, say=self.said.append,
                                        **options)

    def test_with_several_boards_and_none_named_nothing_runs(self):
        self.assertEqual(self.run_checks(), 2)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.said, ["refused: this repository has 2 boards (strip, driver); name the one to check "
                                     "with --board"])

    def test_the_board_named_is_checked_and_its_result_kept_on_its_own_graph(self):
        self.assertEqual(self.run_checks(board="driver"), 0)
        validate, keep = self.commands
        self.assertEqual(validate[-2:], ["--board-description", "driver/board.json"])
        self.assertEqual(keep[:3], ["--board", "driver", "checks"])

    def test_an_unknown_board_is_refused(self):
        self.assertEqual(self.run_checks(board="strap"), 2)
        self.assertEqual(self.commands, [])
        self.assertIn("no board 'strap' here", self.said[0])


def description(folder, stem):
    """A board description for a schematic-stage board in `folder`."""
    return {
        "name": stem, "project": "Two boards", "folder": folder, "schematic": f"{folder}/{stem}.kicad_sch",
        "pcb": f"{folder}/{stem}.kicad_pcb", "kicad_project": f"{folder}/{stem}.kicad_pro",
        "rules": f"{folder}/{stem}.kicad_dru", "footprint_table": f"{folder}/fp-lib-table",
        "footprint_library": f"{folder}/{stem}.pretty", "model_folders": [f"{folder}/models"],
        "bom_export": f"{folder}/exports/{stem}-bom.csv", "schematic_export": f"{folder}/exports/{stem}.pdf",
        "owner_placement": f"{folder}/owner_placement.json", "reports": f"{folder}/reports",
        "production": f"{folder}/production", "drc_exclusions": f"{folder}/reports/exclusions.json",
        "package_name": stem.upper(), "decisions": f"{folder}/DECISIONS.md",
        # Both boards read the same parts data and datasheets folder.
        "parts_data": "hardware/reference/parts.json", "datasheets": "hardware/reference",
        "electrical_audit": None, "board_audit": None, "part_models": None,
        "hand_solder_notes": {"manual": {}, "factory_parts": [], "when_unpopulated": [], "when_factory": []}}


class TwoBoardsTests(unittest.TestCase):
    """A scratch repository with two boards, run through the tool's own command line: each board walks its first
    cards with a position, records and reports of its own; `where` with no board named shows both; a command that
    writes is refused until a board is named; the Mac is told which board; the page's view names the boards.

    The scratch repository holds a copy of these scripts, since each script works on the repository it sits in.
    Its site settings name a machine that does not exist, so nothing here reaches the KiCad machine, and the
    walk names each board's database with --state: the file the tool itself reads on the Mac for that board."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = pathlib.Path(cls.temp.name).resolve()
        cls.root = cls.base / "repo"
        shutil.copytree(SCRIPTS, cls.root / "scripts",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.ps1", "hooks"))
        graph = (SCRIPTS / "fixtures" / "board_graph.json").read_bytes()
        (cls.root / "tools").mkdir()
        (cls.root / "tools/board_graph.json").write_bytes(graph)
        (cls.root / "tools/board_graph.driver.json").write_bytes(graph)
        write(cls.root / boards.LIST, json.dumps(TWO, indent=2) + "\n")
        write(cls.root / "tools/board_graph_site.json", json.dumps({
            "kicad_machine": {"ssh_host": "nobody@127.0.0.1", "ssh_key": str(cls.base / "no-such-key"),
                              "checkout": cls.root.as_posix(), "steps": "~/no-steps",
                              "editor_lock": "~/no-steps/EDITOR_BUSY.d", "worktree_prefix": "~/no-",
                              "cache": "~/.cache/no-kicad", "disposable_copies": "/tmp/no-"},
            "graph_page": {"address": "http://127.0.0.1:9", "launchd_label": "no.board-graph-page"},
            "state_database": "logs/board-graph/state.sqlite3"}))
        for folder, stem in (("board", "strip"), ("driver", "driver")):
            write(cls.root / folder / "board.json", json.dumps(description(folder, stem), indent=2) + "\n")
            write(cls.root / folder / f"{stem}.kicad_sch", "(kicad_sch (version 1))\n")
            write(cls.root / folder / f"{stem}.kicad_pro", "{}\n")
            write(cls.root / folder / "DECISIONS.md", f"# Decisions for {stem}\n")
        # The driver script that reads the scratch repository as the KiCad machine reads its main checkout: each
        # board's database as a file where the list puts it, with no SSH.
        write(cls.root / "scripts/as_the_mac.py", "\n".join([
            "import pathlib, sys",
            "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))",
            "import boards",
            "boards.take_argument()",
            "import mac",
            "mac.LOCAL = True",
            "import board_graph",
            "board_graph.PROGRAM = pathlib.Path(__file__).resolve()",
            "sys.exit(board_graph.main())", ""]))
        write(cls.root / ".gitignore", "logs/\n__pycache__/\nview.json\n")
        cls.env = {key: value for key, value in os.environ.items()
                   if not key.startswith("BOARD_GRAPH_") and key != "GIT_DIR"}
        cls.env.update(PYTHONDONTWRITEBYTECODE="1", GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.invalid",
                       GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.invalid",
                       GIT_TERMINAL_PROMPT="0")
        # A repository with an origin, as a project is: next brings the checkout current before a script's card.
        cls.git("init", "-q", "--bare", "-b", "main", str(cls.base / "origin.git"), cwd=cls.base)
        cls.git("init", "-q", "-b", "main")
        cls.git("add", "-A")
        cls.git("commit", "-q", "-m", "Two boards")
        cls.git("remote", "add", "origin", str(cls.base / "origin.git"))
        cls.git("push", "-q", "-u", "origin", "main")
        cls.states = {"strip": cls.root / "logs/board-graph/state.sqlite3",
                      "driver": cls.root / "logs/board-graph/driver/state.sqlite3"}
        cls.walk()

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
    def script(cls, name, *argv, stdin=None, env=None):
        done = subprocess.run([sys.executable, str(cls.root / "scripts" / name), *argv], cwd=str(cls.root),
                              input=stdin, capture_output=True, env=dict(cls.env, **(env or {})), **NO_WINDOW_KWARGS)
        return (done.returncode, done.stdout.decode("utf-8", "replace").replace("\r\n", "\n"),
                done.stderr.decode("utf-8", "replace").replace("\r\n", "\n"))

    @classmethod
    def graph(cls, board, *argv):
        """The graph's command line for one board, on that board's own scratch database."""
        return cls.script("board_graph.py", "--board", board, "--state", str(cls.states[board]), *argv)

    @classmethod
    def walk(cls):
        """Inventory on both boards, by its script; then Decisions on the strip alone, by the owner."""
        cls.walked = {}
        for board in ("strip", "driver"):
            cls.walked[board, "inventory"] = cls.graph(board, "next")
        cls.walked["strip", "start"] = cls.graph("strip", "next", "--by", "owner", "--note", "the strip's decisions")
        with contextlib.closing(sqlite3.connect(str(cls.states["strip"]))) as db:
            attempt = db.execute("SELECT run_id FROM attempts WHERE node='decisions' AND finished_at IS NULL").fetchone()
        cls.walked["strip", "decisions"] = cls.graph("strip", "record", "decisions", "pass", "--by", "owner",
                                                     "--note", "Yes, the strip's decisions stand",
                                                     "--evidence", "board/DECISIONS.md",
                                                     *(["--attempt", attempt[0]] if attempt else []))

    def rows(self, board):
        with contextlib.closing(sqlite3.connect(str(self.states[board]))) as db:
            return db.execute("SELECT node, outcome, target FROM transitions ORDER BY seq").fetchall()

    def test_each_board_walks_its_first_cards_with_its_own_position(self):
        for step, (code, out, err) in self.walked.items():
            self.assertIn(code, (0, 2), f"{step}: {out}\n{err}")  # 2: a person's card was started
        self.assertIn("inventory pass -> decisions recorded", self.walked["strip", "inventory"][1])
        self.assertIn("inventory pass -> decisions recorded", self.walked["driver", "inventory"][1])
        self.assertIn("decisions pass -> constraints recorded", self.walked["strip", "decisions"][1])
        self.assertEqual(self.rows("strip"), [("inventory", "pass", "decisions"), ("decisions", "pass", "constraints")])
        self.assertEqual(self.rows("driver"), [("inventory", "pass", "decisions")])

    def test_each_board_has_its_own_records_and_reports(self):
        strip =sorted(path.name for path in (self.root / "tools/board_graph_records").iterdir())
        driver = sorted(path.name for path in (self.root / "tools/board_graph_records.driver").iterdir())
        self.assertEqual([name.endswith("-inventory-pass.json") for name in strip], [True, False])
        self.assertTrue(strip[1].endswith("-decisions-pass.json"))
        self.assertEqual(len(driver), 1)
        self.assertTrue(driver[0].endswith("-inventory-pass.json"))
        self.assertFalse(set(strip) & set(driver))
        # Each board's Inventory report is under its own reports folder and read its own design files.
        for folder, stem in (("board", "strip"), ("driver", "driver")):
            report = json.loads((self.root / folder / "reports/inventory/baseline.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(report["detail"]["read"]),
                             [f"{folder}/{stem}.kicad_pro", f"{folder}/{stem}.kicad_sch"])

    def test_where_with_no_board_named_shows_every_boards_position(self):
        code, out, err = self.script("as_the_mac.py", "where")
        self.assertEqual(code, 0, err)
        strip, driver = out.split("Board driver, driver/board.json:\n")
        self.assertTrue(strip.startswith("Board strip, board/board.json:\n"), strip[:200])
        self.assertIn("Board: between Decisions and Constraints (agent)", strip)
        self.assertIn("Board: between Inventory and Decisions (owner)", driver)
        self.assertIn(f"State: {self.states['strip']}, the board strip's database, on this Mac", strip)
        self.assertIn(f"State: {self.states['driver']}, the board driver's database, on this Mac", driver)
        # What a person is told to run names the board.
        self.assertIn("py -3 scripts/board_graph.py --board driver next", self.script(
            "as_the_mac.py", "--board", "driver", "brief", "decisions")[1])
        code, out, err = self.script("as_the_mac.py", "where", "--json")
        self.assertEqual(code, 0, err)
        found = {item["name"]: item for item in json.loads(out)["boards"]}
        self.assertEqual({name: (item["description"], item["where"]["tracks"][0]["position"])
                          for name, item in found.items()},
                         {"strip": ("board/board.json", "constraints"), "driver": ("driver/board.json", "decisions")})
        one = self.script("as_the_mac.py", "where", "--board", "driver")[1]  # after the command's name too
        self.assertIn("between Inventory and Decisions", one)
        self.assertNotIn("Board strip", one)

    def test_a_command_that_writes_is_refused_until_a_board_is_named(self):
        for argv in (["next", "--by", "owner"], ["record", "constraints", "pass", "--by", "owner", "--note", "x"],
                     ["brief", "decisions"], ["checks", "--commit", "c", "--run", "r", "--summary", "s.json"],
                     ["graph"], ["--state", str(self.states["strip"]), "where"]):
            code, out, err = self.script("board_graph.py", *argv)
            self.assertEqual((code, out), (2, ""), argv)
            command = argv[-1] if argv[0].startswith("--") else argv[0]
            self.assertEqual(err, f"{command}: this repository has 2 boards (strip, driver); name the one this is "
                                  f"for, e.g. py -3 scripts/board_graph.py --board strip {command}\n")
        self.assertEqual(self.rows("driver"), [("inventory", "pass", "decisions")])
        code, _, err = self.script("board_graph.py", "--board", "strap", "where")
        self.assertEqual((code, err), (2, f"no board 'strap' here: {boards.LIST} lists strip, driver\n"))

    def test_check_with_no_board_named_checks_every_boards_cards(self):
        # The scratch cards are a copy of a working project's and name docs this repository does not hold, so each
        # board's check fails here; what matters is that each board's cards file was the one checked.
        code, out, err = self.script("board_graph.py", "check")
        self.assertEqual(code, 1)
        for cards in ("board_graph.json", "board_graph.driver.json"):
            self.assertIn(str(self.root / "tools" / cards) + ": ", err)
        code, out, err = self.script("board_graph.py", "--board", "driver", "check")
        self.assertEqual(code, 1)
        self.assertIn("board_graph.driver.json: ", err)
        self.assertNotIn("board_graph.json: ", err)

    def test_a_board_the_list_does_not_name_cannot_write_the_first_boards_position(self):
        write(self.root / "tmp/board.json", json.dumps(description("tmp", "scratch")))
        self.addCleanup(shutil.rmtree, self.root / "tmp")
        code, out, err = self.script("as_the_mac.py", "where", env={boards.ENV: "tmp/board.json"})
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err, f"where: tmp/board.json is not a board {boards.LIST} lists (strip, driver); list it "
                              "there, or name a scratch database with --state\n")
        scratch = self.base / "scratch.sqlite3"  # with a database of its own it runs, as a test's board does
        code, out, err = self.script("board_graph.py", "--state", str(scratch), "where",
                                     env={boards.ENV: "tmp/board.json"})
        self.assertEqual(code, 0, err)
        self.assertIn(f"State: {scratch}, named by --state", out)

    def test_the_mac_is_told_which_board_and_keeps_each_boards_database_apart(self):
        # As a computer that is not the Mac sees it, wherever the test runs: on the Mac, place() names a file.
        probe = ("import mac; mac.LOCAL = False; "
                 "import json, board_graph; print(json.dumps([board_graph.CHOSEN.name, board_graph.SERVE, "
                 "board_graph.place(environ={})['path'], board_graph.RECORDS_IN, board_graph.VIEW_IN, "
                 "str(board_graph.GRAPH.relative_to(board_graph.ROOT).as_posix())]))")
        found = {}
        for name, env in (("none", {}), ("strip", {boards.ENV: "board/board.json"}),
                          ("driver", {boards.ENV: "driver/board.json"})):
            done = subprocess.run([sys.executable, "-c", probe], cwd=str(self.root / "scripts"), capture_output=True,
                                  env=dict(self.env, **env), **NO_WINDOW_KWARGS)
            self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
            found[name] = json.loads(done.stdout)
        checkout = self.root.as_posix()
        strip = ["strip", f"cd {checkout} && exec python3 scripts/board_graph.py serve",
                 f"nobody@127.0.0.1:{checkout}/logs/board-graph/state.sqlite3", "tools/board_graph_records",
                 "tools/board_graph_view", "tools/board_graph.json"]
        self.assertEqual(found["none"], strip)  # none named: the first board, reached as a one-board repository is
        self.assertEqual(found["strip"], strip)
        self.assertEqual(found["driver"], [
            "driver", f"cd {checkout} && exec python3 scripts/board_graph.py --board driver serve",
            f"nobody@127.0.0.1:{checkout}/logs/board-graph/driver/state.sqlite3", "tools/board_graph_records.driver",
            "tools/board_graph_view/boards/driver", "tools/board_graph.driver.json"])
        # What the Mac runs for that command: serve answers from the board's own database.
        request = json.dumps({"op": "read", "args": {}, "graph": None}).encode("utf-8")
        for board, argv, count in (("driver", ["--board", "driver", "serve"], 1), ("strip", ["serve"], 2)):
            code, out, err = self.script("as_the_mac.py", *argv, stdin=request)
            self.assertEqual(code, 0, err)
            copy = self.base / f"copy-{board}.sqlite3"
            copy.write_bytes(__import__("base64").b64decode(json.loads(out)["copy"]))
            with contextlib.closing(sqlite3.connect(str(copy))) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM transitions").fetchone()[0], count, board)

    def test_the_page_has_a_view_for_each_board_that_names_the_boards(self):
        code, out, err = self.script("as_the_mac.py", "--board", "driver", "where")  # a read on the Mac
        self.assertEqual(code, 0, err)
        probe = ("import mac; mac.LOCAL = True; import board_graph, board_graph_view as v; "
                 "state = board_graph.open_state(board_graph.place(), board_graph.RECORDS); "
                 "v.quick_snapshot(board_graph.Graph(), state, board_graph.place()['view'])")
        for env in ({boards.ENV: "board/board.json"}, {boards.ENV: "driver/board.json"}):
            done = subprocess.run([sys.executable, "-c", probe], cwd=str(self.root / "scripts"), capture_output=True,
                                  env=dict(self.env, **env), **NO_WINDOW_KWARGS)
            self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        views = {name: json.loads((self.root / folder / "view.json").read_text(encoding="utf-8"))
                 for name, folder in (("strip", "tools/board_graph_view"),
                                      ("driver", "tools/board_graph_view/boards/driver"))}
        listed = [{"name": "strip", "data": ""}, {"name": "driver", "data": "boards/driver/"}]
        for name, position in (("strip", "constraints"), ("driver", "decisions")):
            self.assertEqual((views[name]["board"], views[name]["boards"]), (name, listed))
            self.assertEqual(views[name]["where"]["tracks"][0]["position"], position)
        self.assertEqual(views["driver"]["source"]["graph"], "tools/board_graph.driver.json")

    def test_the_page_reads_the_board_its_address_names(self):
        page = (SCRIPTS.parent / "tools/board_graph_view/index.html").read_text(encoding="utf-8")
        for words in ("new URLSearchParams(location.search)", "params.get('board')",
                      "boards/${encodeURIComponent(b.name)}/view.json", "fetch(viewJsonUrl()",
                      'id="board-select"'):
            self.assertIn(words, page)
        self.assertNotIn("fetch('view.json'", page)


if __name__ == "__main__":
    unittest.main()
