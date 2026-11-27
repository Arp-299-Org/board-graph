#!/usr/bin/env python3
"""The board graph loads consistently, position follows transitions, and next runs a node's script."""
import base64
import contextlib
import hashlib
import datetime
import io
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
# Fixture machine settings, read at import so tests name the KiCad machine database consistently across checkouts.
os.environ["BOARD_GRAPH_SITE"] = str(pathlib.Path(__file__).resolve().parent / "fixtures" / "board_graph_site.json")
# The tests read every checkout as a repository with one board: a project's own list of boards (scripts/boards.py)
# is not read here. The file named is not there.
os.environ["BOARD_GRAPH_BOARDS"] = str(pathlib.Path(__file__).resolve().parent / "fixtures" / "no_boards.json")
import board_graph  # noqa: E402
import board_graph_actions  # noqa: E402
import board_graph_codex  # noqa: E402
import mac  # noqa: E402
import site_settings  # noqa: E402
import tidy  # noqa: E402
from board_graph import (DONE, Graph, State, checkout_problems, checks, git_text, hash_at, hash_path,  # noqa: E402
                         next_step, record, render, where, workspace)
import boards  # noqa: E402
from board_graph_actions import ACTIONS, COMMANDS, LATER, Action, refs  # noqa: E402
from board_graph_tool import MANIFEST  # noqa: E402
from kicad_sexp import child, children  # noqa: E402
from no_window import KWARGS as NO_WINDOW_KWARGS  # noqa: E402

SCRIPTS = pathlib.Path(__file__).resolve().parent
# The graph fixture (with its side tracks, framework list and open issues), kept with the tests so every checkout
# reads the same one. The tests that check the project's own graph read board_graph.GRAPH.
FIXTURE_GRAPH = SCRIPTS / "fixtures" / "board_graph.json"


def no_network(*args, **kwargs):
    raise AssertionError("a test reached for the real tidy, the Mac or GitHub")


# No test touches this repository's branches, the Mac or GitHub: each passes its own stand-ins.
board_graph.run_tidy = no_network
board_graph.pull_request_view = no_network
board_graph.open_pull_requests = no_network
board_graph.remote_call = no_network  # the SSH round trip to the Mac's database
board_graph.mac_checkout = no_network  # on the Mac, the live database: every test names its own with --state
tidy.mac_status = no_network

PULLS = [
    {"number": 90, "title": "Nothing names it", "headRefName": "x", "isDraft": False, "url": "u90",
     "updatedAt": "2026-11-05T09:00:00Z", "statusCheckRollup": []},
    {"number": 85, "title": "Asked", "headRefName": "y", "isDraft": True, "url": "u85",
     "updatedAt": "2026-11-05T09:00:00Z",
     "statusCheckRollup": [{"name": "validate", "status": "COMPLETED", "conclusion": "FAILURE"}]},
]


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.graph = Graph()

    def test_board_track_walks_the_seven_stages_in_order(self):
        stages = [self.graph.nodes[key]["stage"] for key in self.graph.track_nodes("board")]
        order = [stage["id"] for stage in self.graph.data["stages"]]
        self.assertEqual(sorted(set(stages), key=order.index), order)
        self.assertEqual(stages, sorted(stages, key=order.index), "stages must not interleave")

    def test_every_returned_target_is_earlier_on_the_board_or_on_firmware(self):
        board = self.graph.track_nodes("board")
        for key in board:
            for target in self.graph.nodes[key]["outcomes"]["returned"]:
                if target in board:
                    self.assertLess(board.index(target), board.index(key), f"{key} returns forward to {target}")
                else:
                    self.assertEqual(self.graph.nodes[target]["track"], "firmware", f"{key} returns to {target}")

    def test_allowed_outcomes_name_their_targets(self):
        allowed = self.graph.allowed("stock")
        self.assertEqual(allowed["pass"], ["datasheets"])
        self.assertEqual(allowed["returned"], ["constraints", "decisions"])
        for outcome in self.graph.stay:
            self.assertEqual(allowed[outcome], ["stock"])
        self.assertNotIn("returned", self.graph.allowed("inventory"))

    def test_open_issues_block_the_nodes_they_name(self):
        # An issue of the test's own, so closing one of the board's issues never breaks this.
        data = json.loads(board_graph.GRAPH.read_text(encoding="utf-8"))
        data["open_issues"] = [{"id": "a_test_issue", "close_before": ["package", "order"], "nets": [],
                                "issue": "made for this test"}]
        graph = Graph(data=data)
        self.assertEqual(graph.blocking_issues("package"), ["a_test_issue"])
        self.assertEqual(graph.blocking_issues("order"), ["a_test_issue"])
        self.assertEqual(graph.blocking_issues("inventory"), [])
        # The board's own issues, whichever the graph holds today: each blocks every node it names.
        for issue in self.graph.data["open_issues"]:
            for key in issue["close_before"]:
                self.assertIn(issue["id"], self.graph.blocking_issues(key))

    def test_mermaid_names_every_node_and_pass_edge(self):
        text = self.graph.mermaid()
        for key, node in self.graph.nodes.items():
            self.assertIn(f'{key}["{node["label"]}"]', text)
            if node["outcomes"]["pass"] != DONE:
                self.assertIn(f"{key} --> {node['outcomes']['pass']}", text)

    def test_validation_rejects_a_pass_edge_that_leaves_the_chain(self):
        data = json.loads(board_graph.GRAPH.read_text(encoding="utf-8"))
        next(node for node in data["nodes"] if node["id"] == "bom")["outcomes"]["pass"] = "placement"
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "graph.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                Graph(path)
        self.assertIn("must visit every node", str(caught.exception))

    def test_validation_rejects_unknown_targets_and_tracks(self):
        data = json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))
        stock = next(node for node in data["nodes"] if node["id"] == "stock")
        stock["outcomes"]["returned"].append("nowhere")
        stock["requires"] = ["nothing"]
        data["open_issues"][0]["close_before"] = ["nobody"]
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "graph.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                Graph(path)
        message = str(caught.exception)
        for fragment in ("unknown node nowhere", "requires unknown node nothing", "unknown node nobody"):
            self.assertIn(fragment, message)

    def test_the_shipped_graphs_have_no_cards_waiting_on_each_other(self):
        self.assertEqual(self.graph.order_problems(), [])
        self.assertEqual(Graph(FIXTURE_GRAPH).order_problems(), [])

    def test_check_names_cards_that_require_each_other_in_a_loop(self):
        data = json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))
        board = [node for node in data["nodes"] if node["track"] == "board"]
        board[0]["requires"] = [board[2]["id"]]  # the first card waits on the third, which comes after it
        [problem] = Graph(data=data).order_problems()
        self.assertIn("wait on each other", problem)
        for node in board[:3]:
            self.assertIn(node["id"], problem)
        self.assertIn("it requires it", problem)

    def test_check_names_an_issue_whose_closing_card_waits_on_the_card_it_blocks(self):
        # Cycle case: a card on one track cannot pass with an issue open, and the card whose work closes
        # that issue requires the first card.
        data = json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))
        ordered = {track["id"] for track in data["tracks"] if track["ordered"]}
        blocked = [node for node in data["nodes"] if node["track"] == "board"][-1]
        closer = [node for node in data["nodes"] if node["track"] in ordered and node["track"] != "board"][-1]
        closer["requires"] = [blocked["id"]]
        blocked.pop("requires", None)
        issue = {"id": "loop", "close_before": [blocked["id"]], "nets": [], "issue": "needs the other card"}
        data["open_issues"] = [issue]
        self.assertEqual(Graph(data=json.loads(json.dumps(data))).order_problems(), [])  # nothing says who closes it
        issue["closed_by"] = closer["id"]
        [problem] = Graph(data=json.loads(json.dumps(data))).order_problems()
        self.assertIn(f"{blocked['id']} waits on {closer['id']} (closes open issue loop, which blocks it)", problem)
        self.assertIn(f"{closer['id']} waits on {blocked['id']} (it requires it)", problem)
        issue["closed_by"] = blocked["id"]  # a card that closes its own issue waits on nothing
        self.assertEqual(Graph(data=json.loads(json.dumps(data))).order_problems(), [])
        issue["closed_by"] = "nobody"
        with self.assertRaises(ValueError) as caught:
            Graph(data=data)
        self.assertIn("closed_by names unknown node nobody", str(caught.exception))

    def test_cli_check_fails_on_a_loop(self):
        data = json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))
        board = [node for node in data["nodes"] if node["track"] == "board"]
        board[0]["requires"] = [board[1]["id"]]
        with tempfile.TemporaryDirectory() as directory:
            root, path = self.checkout(directory, data)
            path.write_text(json.dumps(data, indent=1), encoding="utf-8")
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                code = board_graph.main(["--graph", str(path), "check"])
        self.assertEqual(code, 1)
        self.assertIn("wait on each other", errors.getvalue())

    def checkout(self, directory, data):
        """A graph file in a temporary checkout that holds the files every agent loads and nothing else."""
        root = pathlib.Path(directory)
        (root / "tools").mkdir()
        for name in board_graph.LOADED:
            (root / name).write_text("# loaded\n", encoding="utf-8")
        path = root / "tools" / "graph.json"
        path.write_text(json.dumps(data), encoding="utf-8")  # the whole graph on one line
        return root, path

    def test_check_names_a_line_a_read_would_cut(self):
        self.assertEqual(self.graph.reading_problems(), [])
        data = json.loads(board_graph.GRAPH.read_text(encoding="utf-8"))
        data.pop("docs")
        for node in data["nodes"]:
            node.pop("docs", None)
        with tempfile.TemporaryDirectory() as directory:
            root, path = self.checkout(directory, data)
            [problem] = Graph(path).reading_problems()
            self.assertRegex(problem, rf"^line 1 is \d+ characters, over {board_graph.LINE_CAP}: a Read cuts it$")
            self.assertEqual(Graph(data=data).reading_problems(), [])
            self.assertEqual(board_graph.main(["--graph", str(path), "check"]), 1)

    def test_check_names_a_doc_that_is_missing_or_over_a_cut(self):
        data = json.loads(board_graph.GRAPH.read_text(encoding="utf-8"))
        for node in data["nodes"]:
            node.pop("docs", None)
        data["docs"] = ["docs/GONE.md", "docs/LONG.md", "docs/TALL.md", "docs/WIDE.md"]
        data["nodes"][0]["docs"] = ["docs/WIDE.md"]
        with tempfile.TemporaryDirectory() as directory:
            root, path = self.checkout(directory, data)
            (root / "docs").mkdir()
            long = ("x" * 59 + "\n") * (board_graph.DOC_CHARS // 60 + 1)  # over the characters, every line short
            (root / "docs" / "LONG.md").write_text(long, encoding="utf-8")
            (root / "docs" / "TALL.md").write_text("x\n" * (board_graph.DOC_LINES + 1), encoding="utf-8")
            (root / "docs" / "WIDE.md").write_text("ok\n" + "x" * (board_graph.LINE_CAP + 1) + "\n", encoding="utf-8")
            problems = Graph(path).reading_problems()
            self.assertEqual(problems[0][:10], "line 1 is ")  # the graph's own long line comes first
            self.assertEqual(problems[1:], [
                "docs/GONE.md is missing, and a card names it",
                f"docs/LONG.md is {len(long)} characters, over {board_graph.DOC_CHARS}: "
                "a shell's output cut drops its end",
                f"docs/TALL.md is {board_graph.DOC_LINES + 1} lines, over {board_graph.DOC_LINES}: a Read stops there",
                f"docs/WIDE.md: line 2 is over {board_graph.LINE_CAP} characters: a Read cuts it",
            ])
            self.assertEqual(Graph(path).docs(data["nodes"][0]["id"]), data["docs"] + ["docs/WIDE.md"])

    def test_check_names_a_report_longer_than_a_read(self):
        data = json.loads(board_graph.GRAPH.read_text(encoding="utf-8"))
        data.pop("docs")
        for node in data["nodes"]:
            node.pop("docs", None)
        with tempfile.TemporaryDirectory() as directory:
            root, path = self.checkout(directory, data)
            folder = root / board_graph.REPORTS / "placement"
            folder.mkdir(parents=True)
            (folder / "placement.json").write_text("{}\n" * 10, encoding="utf-8")
            (folder / "tall.json").write_text("x\n" * (board_graph.DOC_LINES + 1), encoding="utf-8")
            (folder / "render.png").write_bytes(b"\n" * (board_graph.DOC_LINES + 1))  # not text: not read whole
            problems = [p for p in Graph(path).reading_problems() if not p.startswith("line 1 is")]
            self.assertEqual(problems, [f"board/reports/placement/tall.json is {board_graph.DOC_LINES + 1} lines, over "
                                        f"{board_graph.DOC_LINES}: a Read stops there; put the long list in a file beside "
                                        "the report"])


class NamedPathTests(unittest.TestCase):
    """{name} in a path a card owns or a doc it reads is the graph's name."""

    def data(self):
        data = json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))
        data["docs"] = ["notes/{name}.md"]
        data["nodes"][0]["owns"] = ["{name}/outline.md", "shared.md"]
        return data

    def test_a_listed_graph_reads_its_own_name(self):
        with mock.patch.object(board_graph, "CHOSEN", boards.Board("talk", "graphs/talk.json", first=False)):
            graph = Graph(FIXTURE_GRAPH, data=self.data())
        self.assertEqual((graph.data["docs"], graph.data["nodes"][0]["owns"]),
                         (["notes/talk.md"], ["talk/outline.md", "shared.md"]))

    def test_a_project_with_no_list_is_told(self):
        with self.assertRaises(ValueError) as refused:
            Graph(FIXTURE_GRAPH, data=self.data())
        self.assertIn("{name}/outline.md says {name}, a graph's name, and this project lists no graphs",
                      str(refused.exception))


class PositionTests(unittest.TestCase):
    def setUp(self):
        self.graph = Graph(FIXTURE_GRAPH)
        self.directory = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.directory.name) / "state.sqlite3",
                           pathlib.Path(self.directory.name) / "records")

    def tearDown(self):
        self.directory.cleanup()

    def board(self):
        return next(track for track in where(self.graph, self.state)["tracks"] if track["id"] == "board")

    def test_every_track_starts_at_its_start(self):
        view = where(self.graph, self.state)
        positions = {track["id"]: track.get("position") for track in view["tracks"]}
        self.assertEqual(positions, {"board": "inventory", "firmware": "firmware_ready",
                                     "research": "ipc_research", "owner": "long_lead_parts", "framework": None})
        board = self.board()
        self.assertIsNone(board["node"]["since"])
        self.assertTrue(board["stages"][0]["current"])
        self.assertIn("Board: before Inventory (auto), the track's start", render(view))

    def test_pass_moves_forward_and_stay_outcomes_keep_position(self):
        self.state.append(self.graph, "board", "inventory", "pass", "decisions", "test", "read cleanly")
        self.assertEqual(self.state.position(self.graph, "board"), "decisions")
        self.state.append(self.graph, "board", "decisions", "needs_owner", "decisions", "test", "waiting")
        self.assertEqual(self.state.position(self.graph, "board"), "decisions")
        board = self.board()
        self.assertEqual(board["node"]["moved_because"], "waiting")
        self.assertEqual(board["node"]["moved_by"], "test")
        self.assertTrue(self.state.passed("inventory"))
        self.assertFalse(self.state.passed("decisions"))

    def test_a_node_only_another_checkouts_graph_has_is_named_not_a_crash(self):
        """One database per machine: a branch's graph adds a node and a record moves the track there; a checkout
        whose graph lacks the node reads the same database."""
        self.state.append(self.graph, "board", "inventory", "pass", "decisions", "test", "read cleanly")
        with self.state.writing() as db:
            db.execute("INSERT INTO transitions(at,track,node,outcome,target,actor,note,evidence_json,inputs_json)"
                       " VALUES(?,?,?,?,?,?,?,?,?)",
                       ("2026-01-01T00:00:00+00:00", "board", "decisions", "pass", "branch_only", "agent:branch",
                        "a branch's graph added branch_only", "[]", "{}"))
        board = self.board()
        self.assertEqual(board["position"], "branch_only")
        self.assertTrue(board["node"]["unknown"])
        self.assertEqual(board["node"]["moved_by"], "agent:branch")
        self.assertFalse(any(stage["current"] for stage in board["stages"]))
        text = render(where(self.graph, self.state))
        self.assertIn("Board: at branch_only, a node this checkout's graph does not have", text)
        self.assertIn("  moved here 2026-01-01T00:00:00+00:00 by agent:branch: a branch's graph added branch_only", text)
        self.assertIn("  nothing on it since 2026-01-01T00:00:00Z", text)
        self.assertIn("Bring this checkout level, or read where there.", text)
        self.assertNotIn("Task:", text.split("Firmware (")[0])

    def test_returned_moves_back_and_the_stage_row_follows(self):
        for node, target in (("inventory", "decisions"), ("decisions", "constraints"), ("constraints", "schematic"),
                             ("schematic", "bom"), ("bom", "stock")):
            self.state.append(self.graph, "board", node, "pass", target, "test", "ok")
        self.assertEqual(self.board()["stages"][1]["current"], True)
        self.state.append(self.graph, "board", "stock", "returned", "constraints", "test", "part gone, circuit changes")
        board = self.board()
        self.assertEqual(board["position"], "constraints")
        self.assertTrue(board["stages"][0]["current"])
        self.assertIn("[Requirements and constraints]", render(where(self.graph, self.state)))

    def test_done_ends_a_track(self):
        self.state.append(self.graph, "firmware", "firmware_ready", "pass", DONE, "test", "built and measured")
        view = where(self.graph, self.state)
        firmware = next(track for track in view["tracks"] if track["id"] == "firmware")
        self.assertEqual(firmware["position"], DONE)
        self.assertNotIn("node", firmware)
        self.assertIn("done", render(view))

    def test_requires_and_blocking_issues_are_reported(self):
        self.state.append(self.graph, "board", "placement", "pass", "routing_batch", "test", "approved")
        node = self.board()["node"]
        self.assertEqual(node["requires"], [{"node": "ipc_prototype", "passed": False}])
        self.state.append(self.graph, "research", "ipc_research", "pass", "ipc_prototype", "test", "surveyed")
        self.state.append(self.graph, "research", "ipc_prototype", "pass", "router_comparison", "test", "repaired")
        self.assertEqual(self.board()["node"]["requires"], [{"node": "ipc_prototype", "passed": True}])
        self.state.append(self.graph, "board", "visual_review", "pass", "verification", "test", "looked")
        text = render(where(self.graph, self.state))
        blocking = self.graph.blocking_issues("verification")  # whichever issues the graph holds today
        if blocking:
            self.assertIn("Blocked by open issues: " + ", ".join(blocking), text)
        else:
            self.assertNotIn("Blocked by open issues", text)

    def test_framework_transitions_never_move_a_track(self):
        self.state.append(self.graph, "framework", "usage", "more_work", "usage", "test", "partial accounting")
        view = where(self.graph, self.state)
        framework = next(track for track in view["tracks"] if track["id"] == "framework")
        self.assertIsNone(framework.get("position"))
        usage = next(node for node in framework["nodes"] if node["id"] == "usage")
        self.assertEqual(usage["last_outcome"], "more_work")
        self.assertEqual(self.state.position(self.graph, "board"), "inventory")

    def test_append_refuses_a_node_off_its_track_and_an_unknown_target(self):
        with self.assertRaises(ValueError):
            self.state.append(self.graph, "board", "firmware_ready", "pass", DONE, "test", "")
        with self.assertRaises(ValueError):
            self.state.append(self.graph, "board", "inventory", "pass", "elsewhere", "test", "")
        self.assertEqual(self.state.transitions(), [])

    def test_cli_where_check_and_graph(self):
        import contextlib
        import io
        out = io.StringIO()
        offline = {"at": "2026-11-05T00:00:00+00:00", "fetch_error": "offline", "here": {"checkouts": [],
                   "tidy_pending": []}, "mac": {"reached": False, "error": "offline"}, "merge_questions": [],
                   "last_upkeep": None}
        this = {"path": "C:/demo", "branch": "main", "this": True, "head": "abc12345", "behind": 0, "ahead": 0,
                "dirty_owned": []}
        state = ["--state", str(self.state.path), "--records", str(self.state.records)]
        unread = {"tidy_pending": None, "not_on_main": None}  # as when git does not answer
        with contextlib.redirect_stdout(out), mock.patch.object(board_graph, "workspace", lambda *a, **k: offline), \
                mock.patch.object(board_graph, "checkout_here", lambda *a, **k: dict(this)), \
                mock.patch.object(board_graph, "branches_here", lambda *a, **k: dict(unread)):
            self.assertEqual(board_graph.main(state + ["where", "--full"]), 0)
            full = out.getvalue()
            self.assertEqual(board_graph.main(state + ["where"]), 0)
            quick = out.getvalue()[len(full):]
            self.assertEqual(board_graph.main(["check"]), 0)
            self.assertEqual(board_graph.main(["graph"]), 0)
        self.assertIn("Board: before Inventory (auto), the track's start", full)
        self.assertIn("Mac: not reached (offline)", full)
        # The default reads this checkout and the database; the stand-in kept nothing, so the rest is not read.
        self.assertIn("Workspace, quick read (`where --full` reads it all now):", quick)
        self.assertIn("  Here, main at abc12345 (C:/demo): level with GitHub's main\n", quick)
        self.assertIn("  Here, tidy pending: not read", quick)
        self.assertIn("consistent", out.getvalue())
        self.assertIn("flowchart LR", out.getvalue())


class RecordTests(unittest.TestCase):
    """record() moves a track only along the graph's edges, with evidence and inputs hashed."""

    def setUp(self):
        self.graph = Graph(FIXTURE_GRAPH)
        self.directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        self.state = State(self.root / "state.sqlite3", self.root / "records")
        (self.root / "board").mkdir()
        (self.root / "board/DECISIONS.md").write_text("decisions\n", encoding="utf-8")
        (self.root / "docs").mkdir()
        (self.root / "docs/PRODUCT_TRUTH.md").write_text("truth\n", encoding="utf-8")
        (self.root / "report.json").write_text("{}\n", encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def rec(self, node, outcome, actor="agent:test", note="because", evidence=("report.json",), to=None):
        return record(self.graph, self.state, node, outcome, actor, note, evidence, to, root=self.root)

    def test_local_export_recovers_after_the_database_commits(self):
        with mock.patch.object(board_graph.workflow_history, "publish", side_effect=OSError("export interrupted")):
            with self.assertRaisesRegex(OSError, "export interrupted"):
                self.rec("inventory", "more_work")
        row, = self.state.transitions()
        self.assertFalse((self.state.records / row["record_name"]).exists())
        reopened = State(self.state.path, self.state.records)
        self.assertEqual(len(reopened.transitions()), 1)
        self.assertEqual(json.loads((reopened.records / row["record_name"]).read_bytes()), json.loads(board_graph.record_text(row)))

    def test_changed_routing_contract_cannot_qualify_the_open_attempt(self):
        key = "routing_batch"
        self.graph.nodes[key]["action"] = "routing_run"
        self.graph.nodes[key]["requires"] = []
        self.graph.nodes[key]["docs"] = ["board/DECISIONS.md"]
        self.state.append(self.graph, "board", "placement", "pass", key, "owner", "fixture approved placement")
        attempt, _, _ = self.state.open_work(self.graph, key, "agent:test", "", {}, str(self.root))
        (self.root / "board/DECISIONS.md").write_text("changed after start")
        with self.assertRaisesRegex(ValueError, "board/DECISIONS.md"):
            record(self.graph, self.state, key, "pass", "agent:test", "done", ["report.json"],
                   root=self.root, attempt_id=attempt["run_id"])
        stay = record(self.graph, self.state, key, "needs_lead", "agent:test", "requirements changed",
                      root=self.root, attempt_id=attempt["run_id"])
        self.assertEqual(stay["target"], key)

    def test_failed_bench_observation_cannot_finish_the_product(self):
        graph = Graph()
        key = "product_judgment"
        graph.data["approval_mode"] = "off"  # exercise the independent first-article receipt in isolation
        graph.nodes[key]["integration"] = {"boards": {"motor": "motor.pcb"}}
        (self.root / "motor.pcb").write_bytes(b"reviewed geometry")
        self.state.append(graph, "board", "subsystems", "pass", key, "owner", "fixture")
        report = self.root / "acceptance.json"
        observation = {"passed": False, "observation": "unit failed first use", "evidence": "report.json"}
        receipt = {"schema": "board-graph/product-acceptance/v1", "verdict": "accepted",
                   "board_revisions": {"motor": hash_path(self.root / "motor.pcb")}, "units": [{"id": "U01", "firmware": "none", "checks": [observation]}]}
        report.write_text(json.dumps(receipt))
        def finish():
            return record(graph, self.state, key, "pass", "owner", "first article decision",
                          ["acceptance.json", "report.json"], root=self.root)
        with self.assertRaisesRegex(ValueError, "passing observations"):
            finish()
        self.assertEqual(self.state.position(graph, "board"), key)
        observation.update(passed=True, observation="unit passed first use after repair")
        report.write_text(json.dumps(receipt))
        self.assertEqual(finish()["target"], "done")

    def test_final_package_check_runs_after_preparation_and_can_refuse_the_pass(self):
        graph = Graph()
        graph.data["approval_mode"] = "off"  # this test isolates the package action at completion
        self.state.append(graph, "board", "quote", "pass", "package", "owner", "scope chosen")
        with mock.patch.object(board_graph, "ACTIONS", {}):
            with self.assertRaisesRegex(ValueError, "completion check package is unavailable"):
                record(graph, self.state, "package", "pass", "agent:test", "files prepared", ["report.json"], root=self.root)
        check = mock.Mock(return_value={"findings": ["sealed package uses an older board"]})
        with mock.patch.dict(board_graph.ACTIONS, {"package": Action(check, ())}):
            with self.assertRaisesRegex(ValueError, "older board"):
                record(graph, self.state, "package", "pass", "agent:test", "files prepared", ["report.json"], root=self.root)
            check.return_value = {"findings": []}
            passed = record(graph, self.state, "package", "pass", "agent:test", "files verified", ["report.json"], root=self.root)
            self.assertEqual(passed["target"], graph.nodes["package"]["outcomes"]["pass"])

    def test_verification_cannot_borrow_review_from_an_earlier_board(self):
        a = board_graph_actions
        source = self.root / "reviewed.pcb"
        source.write_text("original")
        receipt = {"schema": "board-graph/independent-review/v1", "board_sha256": hash_path(source),
                   "schematic_sha256": hash_path(source), "builder": "builder", "reviewer": "critic",
                   "checks": [{"passed": True, "observation": "Reproduced the design check", "evidence": "report.json"}]}
        (self.root / "review.json").write_text(json.dumps(receipt))
        with mock.patch.object(a, "BOARD", source.name), mock.patch.object(a, "SCHEMATIC", source.name), mock.patch.object(
                a, "freshness", return_value={"findings": []}):
            a.check_shipping_pass("verification", "freshness", ["review.json", "report.json"], self.root)
            source.write_text("different vias and outline")
            with self.assertRaisesRegex(ValueError, "stale"):
                a.check_shipping_pass("verification", "freshness", ["review.json", "report.json"], self.root)

    def test_pass_moves_hashes_evidence_and_owned_inputs_and_exports_one_record(self):
        item = self.rec("inventory", "pass")
        self.assertEqual(item["target"], "decisions")
        self.assertEqual(item["evidence"], [{"path": "report.json", "sha256": hash_path(self.root / "report.json")}])
        self.assertEqual(self.state.position(self.graph, "board"), "decisions")
        item = self.rec("decisions", "pass", actor="owner", note="the owner's words")
        self.assertEqual(item["inputs"], {"board/DECISIONS.md": hash_path(self.root / "board/DECISIONS.md"),
                                          "docs/PRODUCT_TRUTH.md": hash_path(self.root / "docs/PRODUCT_TRUTH.md")})
        records = sorted(p.name for p in (self.root / "records").glob("*.json"))
        self.assertEqual(len(records), 2)
        self.assertTrue(records[0].endswith("-inventory-pass.json"))
        self.assertTrue(records[1].endswith("-decisions-pass.json"))
        saved = json.loads((self.root / "records" / records[1]).read_text(encoding="utf-8"))
        self.assertEqual(saved["actor"], "owner")
        self.assertEqual(saved["inputs"], item["inputs"])

    def gated(self, lines):
        """The Inventory card's action given a gate that finds `lines`, as the Routing card's has."""
        import graph_pack
        action = self.graph.nodes["inventory"]["action"]
        gate = graph_pack.Gate(lambda root: list(lines), "Record needs_lead and bring it to the owner.",
                               "WARNING, read first.")
        return mock.patch.dict(board_graph.PACK.gates, {action: gate})

    def test_a_card_whose_gate_has_findings_refuses_pass_and_more_work_from_anyone_but_the_owner(self):
        with self.gated(["70 tracks added outside the routing run, by commit 448f900c (Joiner routed)"]):
            for outcome in ("pass", "more_work"):
                with self.assertRaises(ValueError) as refused:
                    self.rec("inventory", outcome)
                self.assertIn(f"inventory cannot record {outcome}: 70 tracks added outside the routing run",
                              str(refused.exception))
                self.assertIn("Record needs_lead and bring it to the owner.", str(refused.exception))
            self.assertEqual(self.state.transitions(), [])
            self.assertEqual(self.rec("inventory", "needs_lead", evidence=())["outcome"], "needs_lead")
            self.assertEqual(self.rec("inventory", "more_work", actor="owner", note="I accept it")["actor"], "owner")
            self.assertIn("Not through this card's tool, so pass and more_work are refused:\n    70 tracks",
                          board_graph.brief(self.graph, self.state, "inventory", root=self.root))

    def test_a_gate_with_no_findings_and_a_card_with_no_gate_record_as_before(self):
        with self.gated([]):
            self.assertIn("  WARNING, read first.\n", board_graph.brief(self.graph, self.state, "inventory", root=self.root))
            self.assertEqual(self.rec("inventory", "pass")["target"], "decisions")
        self.assertIn("Copper is laid in the editor by this card's routing run", board_graph.PACK.gates["routing_run"].warning)
        self.assertNotIn("banned from routing", board_graph.PACK.gates["routing_run"].warning)
        text = board_graph.brief(self.graph, self.state, "decisions", root=self.root)
        self.assertNotIn("Not through this card's tool", text)
        self.assertIn(board_graph.BROKEN_CARD, text)

    def test_a_fresh_database_imports_the_records_in_order(self):
        self.rec("inventory", "pass")
        self.rec("decisions", "needs_owner", note="waiting", evidence=())
        rebuilt = State(self.root / "other.sqlite3", self.root / "records")
        self.assertEqual([(row["node"], row["outcome"], row["target"]) for row in rebuilt.transitions()],
                         [("inventory", "pass", "decisions"), ("decisions", "needs_owner", "decisions")])
        self.assertEqual(rebuilt.position(self.graph, "board"), "decisions")
        self.assertEqual(rebuilt.import_records(), 0)

    def test_a_database_from_before_record_names_gains_the_column(self):
        import sqlite3
        from contextlib import closing
        path = self.root / "old.sqlite3"
        with closing(sqlite3.connect(str(path))) as db:
            db.execute("""CREATE TABLE transitions (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, track TEXT NOT NULL, node TEXT NOT NULL,
                outcome TEXT NOT NULL, target TEXT NOT NULL, actor TEXT NOT NULL, note TEXT NOT NULL,
                evidence_json TEXT NOT NULL, inputs_json TEXT NOT NULL)""")
            db.execute("INSERT INTO transitions(at,track,node,outcome,target,actor,note,evidence_json,inputs_json)"
                       " VALUES('2026-11-04T00:00:00+00:00','board','inventory','pass','decisions','old','kept','[]','{}')")
            db.commit()
        state = State(path, self.root / "records")
        self.assertEqual(state.position(self.graph, "board"), "decisions")
        self.assertIsNone(state.transitions()[0]["record_name"])
        record(self.graph, state, "decisions", "pass", "owner", "owner words", ("report.json",), root=self.root)
        self.assertEqual(state.position(self.graph, "board"), "constraints")
        self.assertTrue(state.transitions()[-1]["record_name"].endswith("-decisions-pass.json"))

    def test_refuses_a_node_that_is_not_the_position(self):
        with self.assertRaisesRegex(ValueError, "board is at inventory, not decisions"):
            self.rec("decisions", "pass", actor="owner")
        self.assertEqual(self.state.transitions(), [])

    def test_refuses_outcomes_the_node_does_not_allow(self):
        with self.assertRaisesRegex(ValueError, "does not allow skip"):
            self.rec("inventory", "skip")
        with self.assertRaisesRegex(ValueError, "does not allow returned"):
            self.rec("inventory", "returned", to="decisions")
        self.rec("inventory", "pass")
        self.rec("decisions", "pass", actor="owner", note="owner words")
        with self.assertRaisesRegex(ValueError, "must name one of decisions"):
            self.rec("constraints", "returned", to="inventory")
        with self.assertRaisesRegex(ValueError, "only for returned"):
            self.rec("constraints", "pass", to="decisions")
        item = self.rec("constraints", "returned", to="decisions", note="a decision has to change")
        self.assertEqual(item["target"], "decisions")
        self.assertEqual(self.state.position(self.graph, "board"), "decisions")
        # A return is said as one, not as "between": the work went back.
        self.assertIn("board returned from constraints to decisions", board_graph.recorded(self.graph, self.state, item))
        self.assertIn("Board: returned from Constraints to Decisions (owner)",
                      board_graph.render(board_graph.where(self.graph, self.state)))

    def test_pass_needs_evidence_a_note_and_the_owner_on_owner_nodes(self):
        with self.assertRaisesRegex(ValueError, "needs at least one evidence path"):
            self.rec("inventory", "pass", evidence=())
        with self.assertRaisesRegex(ValueError, "note is required"):
            self.rec("inventory", "pass", note="  ")
        with self.assertRaisesRegex(ValueError, "evidence not found"):
            self.rec("inventory", "pass", evidence=("missing.json",))
        self.rec("inventory", "more_work", note="still reading", evidence=())
        self.rec("inventory", "pass")
        with self.assertRaisesRegex(ValueError, "owner's to pass"):
            self.rec("decisions", "pass")
        self.rec("decisions", "pass", actor="owner", note="the owner's words")
        self.assertEqual(self.state.position(self.graph, "board"), "constraints")

    def test_pass_is_blocked_by_open_issues_and_unmet_requires(self):
        self.graph.data["open_issues"] = [  # the test's own issues: the live ones close as the board is finished
            {"id": "routing_issue", "close_before": ["routing_batch"], "nets": [], "issue": "a test issue"},
            {"id": "verify_issue", "close_before": ["verification"], "nets": [], "issue": "another"}]
        chain = self.graph.track_nodes("board")
        for node in chain[:chain.index("routing_batch")]:
            actor = "owner" if self.graph.nodes[node]["who"] == "owner" else "agent:test"
            self.state.append(self.graph, "board", node, "pass", self.graph.nodes[node]["outcomes"]["pass"],
                              actor, "seeded")
        with self.assertRaisesRegex(ValueError, "open issues name it: routing_issue"):
            self.rec("routing_batch", "pass")
        self.graph.data["open_issues"] = [issue for issue in self.graph.data["open_issues"]
                                          if issue["id"] != "routing_issue"]
        with self.assertRaisesRegex(ValueError, "until these pass: ipc_prototype"):
            self.rec("routing_batch", "pass")
        self.rec("ipc_research", "pass")
        self.rec("ipc_prototype", "pass")
        attempt, _, _ = self.state.open_work(self.graph, "routing_batch", "agent:test", "", {}, str(self.root))
        record(self.graph, self.state, "routing_batch", "pass", "agent:test", "fixture passed", ["report.json"],
               root=self.root, attempt_id=attempt["run_id"])
        self.rec("board_checks", "pass")
        self.rec("visual_review", "pass")
        with self.assertRaisesRegex(ValueError, "open issues name it: verify_issue"):
            self.rec("verification", "pass")
        self.rec("verification", "more_work", note="closing issues", evidence=())
        self.assertEqual(self.state.position(self.graph, "board"), "verification")

    def test_framework_nodes_record_without_a_position(self):
        item = self.rec("usage", "more_work", note="partial accounting", evidence=())
        self.assertEqual(item["target"], "usage")
        self.assertEqual(self.state.position(self.graph, "board"), "inventory")

    def test_cli_record(self):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        base = ["--state", str(self.state.path), "--records", str(self.state.records)]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(board_graph.main(base + ["record", "decisions", "pass", "--by", "owner",
                                                      "--note", "x", "--evidence", "tools/board_graph.json"]), 1)
            self.assertEqual(board_graph.main(base + ["record", "inventory", "pass", "--by", "agent:cli",
                                                      "--note", "read cleanly", "--evidence",
                                                      "tools/board_graph.json"]), 0)
        self.assertIn("board is at inventory, not decisions", err.getvalue())
        self.assertIn("inventory pass -> decisions recorded at", out.getvalue())
        self.assertIn("board is between inventory and decisions", out.getvalue())
        self.assertEqual(self.state.position(self.graph, "board"), "decisions")


class StartTests(unittest.TestCase):
    """next, for a person's card, opens an attempt at the position (State.open_work); the card's next record closes
    it; where says who is on it."""

    def setUp(self):
        self.graph = Graph(FIXTURE_GRAPH)
        self.directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        self.state = State(self.root / "state.sqlite3", self.root / "records")
        (self.root / "report.json").write_text("{}\n", encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def begin(self, node="inventory", actor="agent:a", note="", take=False):
        """What next does for a person's card: the attempt opened for `actor`, as State.open_work writes it."""
        if node not in self.graph.nodes:
            raise ValueError(f"unknown node {node}")
        inputs = {owned: hash_path(self.root / owned) for owned in self.graph.nodes[node]["owns"]}
        return self.state.open_work(self.graph, node, actor, note, inputs, str(self.root.resolve()), take)

    def board(self):
        return next(track for track in where(self.graph, self.state)["tracks"] if track["id"] == "board")

    def test_next_opens_an_attempt_and_the_record_there_closes_it(self):
        attempt, already, taken = self.begin(note="reading the files")
        self.assertEqual((already, taken), (False, []))
        self.assertEqual((attempt["action"], attempt["actor"], attempt["note"], attempt["finished_at"]),
                         ("agent", "agent:a", "reading the files", None))
        node = self.board()["node"]
        self.assertEqual(node["working"], [{"run_id": attempt["run_id"], "by": "agent:a", "action": "agent",
                                            "since": attempt["started_at"], "note": "reading the files"}])
        self.assertIsNone(node["waiting_since"])
        item = record(self.graph, self.state, "inventory", "pass", "agent:a", "read cleanly", ("report.json",),
                      root=self.root, attempt_id=attempt["run_id"])
        [closed] = self.state.attempts("inventory")
        self.assertEqual((closed["finished_at"], closed["outcome"]), (item["at"], "pass"))
        self.assertEqual([entry["run_id"] for entry in item["closed"]], [attempt["run_id"]])
        self.assertIn("closed the open attempt by agent:a", board_graph.recorded(self.graph, self.state, item))
        [saved] = (self.root / "records").glob("*.json")
        self.assertNotIn("closed", json.loads(saved.read_text(encoding="utf-8")))
        node = self.board()["node"]
        self.assertEqual((node["id"], node["working"], node["waiting_since"]), ("decisions", [], item["at"]))

    def test_record_closes_only_the_named_open_attempt(self):
        first, _, _ = self.begin()
        script = self.state.start_attempt("inventory", "baseline", {}, board_graph.now())
        framework, _, _ = self.begin(node="usage", actor="agent:framework")
        with self.assertRaisesRegex(ValueError, "name it with --attempt"):
            record(self.graph, self.state, "inventory", "more_work", "agent:a", "still reading", (), root=self.root)
        record(self.graph, self.state, "inventory", "more_work", "agent:a", "still reading", (), root=self.root,
               attempt_id=first["run_id"])
        outcomes = {(row["node"], row["actor"]): row["outcome"] for row in self.state.attempts()}
        self.assertEqual(outcomes, {("inventory", "agent:a"): "more_work", ("inventory", "script:baseline"): None,
                                    ("usage", "agent:framework"): None})
        self.assertTrue(script)
        aside = record(self.graph, self.state, "usage", "more_work", "agent:framework", "counted", (), root=self.root,
                       attempt_id=framework["run_id"])
        self.assertIn("framework is a list: usage recorded more_work", board_graph.recorded(self.graph, self.state, aside))

    def test_next_refuses_what_is_not_the_position(self):
        for node, actor, words in (("decisions", "agent:a", "board is at inventory, not decisions"),
                                   ("nowhere", "agent:a", "unknown node nowhere")):
            with self.assertRaisesRegex(ValueError, words):
                self.begin(node=node, actor=actor)
        self.state.append(self.graph, "firmware", "firmware_ready", "pass", DONE, "agent:test", "built")
        with self.assertRaisesRegex(ValueError, "firmware is at done, not firmware_ready"):
            self.begin(node="firmware_ready")
        self.assertEqual(self.state.attempts(), [])
        attempt, _, _ = self.begin(node="usage", actor="agent:framework")  # a list's nodes are each open to work
        self.assertEqual(attempt["node"], "usage")

    def test_someone_elses_open_attempt_refuses_until_take(self):
        first, _, _ = self.begin(note="first look")
        with self.assertRaisesRegex(ValueError, r"inventory is on: agent:a since .*Z \(first look\)\. Pass --take"):
            self.begin(actor="agent:b")
        again, already, _ = self.begin()
        self.assertEqual((again["run_id"], already, len(self.state.attempts())), (first["run_id"], True, 1))
        second, already, taken = self.begin(actor="agent:b", take=True)
        self.assertEqual((already, [entry["run_id"] for entry in taken]), (False, [first["run_id"]]))
        rows = {row["run_id"]: row for row in self.state.attempts()}
        self.assertEqual(rows[first["run_id"]]["outcome"], "taken")
        self.assertTrue(rows[first["run_id"]]["finished_at"])
        self.assertIsNone(rows[second["run_id"]]["finished_at"])
        self.state.take([second], board_graph.now())
        self.state.start_attempt("inventory", "baseline", {}, board_graph.now())
        with self.assertRaisesRegex(ValueError, "inventory is on: script:baseline"):
            self.begin(actor="agent:c")

    def test_where_says_working_or_waiting_with_the_age(self):
        text = render(where(self.graph, self.state))
        self.assertIn("  nothing on it, and nothing recorded here yet: next starts it", text)
        attempt, _, _ = self.begin(note="reading")
        since = board_graph.parse_time(attempt["started_at"])
        text = render(where(self.graph, self.state), at=since + timedelta(minutes=12)).split("Firmware (")[0]
        self.assertIn("Board: on Inventory (auto)", text)
        self.assertIn(f"  on it: working since {board_graph.stamp(attempt['started_at'])} by agent:a (12 min ago): reading", text)
        self.assertNotIn("nothing on it", text)
        item = record(self.graph, self.state, "inventory", "needs_lead", "agent:a", "stuck", (), root=self.root,
                      attempt_id=attempt["run_id"])
        text = render(where(self.graph, self.state),
                      at=board_graph.parse_time(item["at"]) + timedelta(hours=3, minutes=30))
        # A stay that asks for someone says so on the card's face, with the ask, until their record.
        self.assertIn("Board: before Inventory (auto), the track's start, stage Requirements and constraints, waiting on the lead", text)
        self.assertIn(f"  needs_lead {item['at']} by agent:a: stuck", text)
        self.assertNotIn("moved here", text.split("Firmware (")[0])
        self.assertIn(f"  nothing on it since {board_graph.stamp(item['at'])} (4 h ago): it waits on the lead; the "
                      "record that answers moves it", text)
        waits = self.board()["node"]["waiting_on"]
        self.assertEqual((waits["outcome"], waits["who"], waits["by"], waits["since"], waits["note"]),
                         ("needs_lead", "the lead", "agent:a", item["at"], "stuck"))
        record(self.graph, self.state, "inventory", "more_work", "agent:a", "unstuck", (), root=self.root)
        self.assertIsNone(self.board()["node"]["waiting_on"])
        self.begin(node="usage", actor="agent:framework")
        self.assertRegex(render(where(self.graph, self.state)),
                         r"  Token use: no outcome recorded\n    working since .* by agent:framework \(under a minute ago\)")

    def test_waiting_since_counts_a_script_run_that_ended_after_the_last_record(self):
        record(self.graph, self.state, "inventory", "more_work", "agent:a", "later", (), root=self.root)
        run = self.state.start_attempt("inventory", "baseline", {}, "2099-01-01T00:00:00+00:00")
        self.state.finish_attempt(run, "2099-01-01T00:01:00+00:00", "error", None, None)
        self.assertEqual(self.board()["node"]["waiting_since"], "2099-01-01T00:01:00+00:00")

    def test_finish_attempt_keeps_the_parts_of_the_report_the_view_shows(self):
        run = self.state.start_attempt("inventory", "baseline", {}, "2026-11-06T02:00:00+00:00")
        report = {"findings": [f"finding {n}" for n in range(35)], "outcome": "needs_lead", "detail": {"x": 1},
                  "merge_commit": None, "error": None}
        self.state.finish_attempt(run, "2026-11-06T02:00:09+00:00", "needs_lead", "r.json", "f" * 64,
                                  board_graph.kept_report(report))
        [attempt] = self.state.attempts()
        self.assertEqual((attempt["report"]["findings_total"], len(attempt["report"]["findings"])), (35, 30))
        self.assertEqual(set(attempt["report"]), {"findings", "findings_total"})
        self.assertNotIn("report_json", attempt)
        kept = board_graph.kept_report({"findings": [], "pull_request": 84, "merge_commit": "abc", "differ": ["a"],
                                        "this_checkout_differs": [], "error": "no"})
        self.assertEqual(kept, {"findings": [], "findings_total": 0, "pull_request": 84, "merge_commit": "abc",
                                "differ": ["a"], "error": "no"})
        bare = self.state.start_attempt("inventory", "baseline", {}, "2026-11-06T02:01:00+00:00")
        self.state.finish_attempt(bare, "2026-11-06T02:01:01+00:00", "error", None, None)
        self.assertIsNone(self.state.attempts()[-1]["report"])

    def test_cli_next_starts_a_persons_card_and_says_who_is_on_it(self):
        self.state.append(self.graph, "board", "inventory", "pass", "decisions", "agent:test", "seeded")
        base = ["--state", str(self.state.path), "--records", str(self.state.records)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(board_graph, "bring_current", return_value=True) as upkeep:
            self.assertEqual(board_graph.main(base + ["next"]), 1)
            self.assertEqual(board_graph.main(base + ["next", "--by", "agent:cli", "--note", "x"]), 2)
            self.assertEqual(board_graph.main(base + ["next", "--by", "agent:two"]), 2)
            self.assertEqual(board_graph.main(base + ["next", "--by", "agent:two", "--take"]), 2)
        self.assertEqual(upkeep.call_count, 2)  # initial start and takeover, not the status query
        self.assertIn("Decisions is a person's card (owner): say who starts it, --by owner or --by agent:<name>", err.getvalue())
        self.assertIn("Decisions is on: working since", out.getvalue())
        self.assertIn("by agent:cli (under a minute ago): x", out.getvalue())
        self.assertIn("Do its task and record the outcome with record; that record closes it.", out.getvalue())
        self.assertIn("Its next record closes it; --take starts it anew for someone else.", out.getvalue())
        self.assertIn("took Decisions over from agent:cli", out.getvalue())
        self.assertIn("Board: on Decisions (owner)", out.getvalue())
        self.assertEqual([row["actor"] for row in self.state.attempts(open_only=True)], ["agent:two"])

    def test_a_write_kept_waiting_by_another_session_says_so_and_writes_nothing(self):
        self.state.append(self.graph, "board", "inventory", "pass", "decisions", "agent:test", "seeded")
        holder = sqlite3.connect(str(self.state.path), isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")  # another session in the middle of a write
        err = io.StringIO()
        try:
            with mock.patch.object(board_graph, "BUSY", 0.1), \
                    mock.patch.object(board_graph, "bring_current", return_value=True), contextlib.redirect_stderr(err), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(board_graph.main(["--state", str(self.state.path), "--records", str(self.state.records),
                                                   "next", "--by", "agent:late"]), 1)
        finally:
            holder.rollback()
            holder.close()
        self.assertIn(f"the state database {self.state.path} did not answer (database is locked); a write waits 0.1 s",
                      err.getvalue())
        self.assertEqual(self.state.attempts(), [])
        self.assertEqual(len(self.state.transitions()), 1)

    def test_a_database_from_before_the_attempt_columns_gains_them(self):
        path = self.root / "old.sqlite3"
        with contextlib.closing(sqlite3.connect(str(path))) as db:
            db.execute("""CREATE TABLE attempts (run_id TEXT PRIMARY KEY, node TEXT NOT NULL, action TEXT NOT NULL,
                inputs_json TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT, report_path TEXT,
                report_sha256 TEXT)""")
            db.execute("INSERT INTO attempts(run_id,node,action,inputs_json,started_at) "
                       "VALUES('old','inventory','baseline','{}','2026-11-05T00:00:00+00:00')")
            db.commit()
        state = State(path, self.root / "records")
        [attempt] = state.attempts()
        self.assertEqual((attempt["actor"], attempt["note"]), ("script:baseline", ""))
        self.assertEqual(where(self.graph, state)["tracks"][0]["node"]["working"][0]["by"], "script:baseline")


class NextTests(unittest.TestCase):
    """next runs the script at the position, records what it reports, and never silently repeats a run."""

    def setUp(self):
        self.graph = Graph(FIXTURE_GRAPH)
        self.directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        self.state = State(self.root / "state.sqlite3", self.root / "records")
        (self.root / "input.txt").write_text("first\n", encoding="utf-8")
        self.calls = 0
        self.upkeeps = []
        self.current = True

    def tearDown(self):
        self.directory.cleanup()

    def upkeep(self, graph, state, root, node, why, out, err, tidy_run=None):
        """Stands in for bring_current: notes the call and what the attempts table held then."""
        self.upkeeps.append((node, why, len(state.attempts())))
        if not self.current:
            err.write(f"{node}: next runs nothing, because the checkouts are not current: stand-in\n")
        return self.current

    def actions(self, result=None, error=None, check=None):
        """Fake baseline and bom scripts that read input.txt and report `result`."""
        def run(root):
            self.calls += 1
            if check:
                check(pathlib.Path(root))
            if error:
                raise error
            return result or {"outcome": "pass", "findings": [], "detail": {"lines": 1}}
        return {"baseline": Action(run, ("input.txt",)), "bom": Action(run, ("input.txt",))}

    def graph_file(self):
        path = self.root / "tools/board_graph.json"
        path.parent.mkdir()
        data = json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))
        path.write_text(json.dumps(data), encoding="utf-8")
        self.graph = Graph(path)
        return path, data

    def test_upkeep_reselects_the_action_and_checks_new_ownership(self):
        path, data = self.graph_file()
        inventory = next(item for item in data["nodes"] if item["id"] == "inventory")
        inventory.update(action="bom", owns=["new_input.txt"])
        (self.root / "new_input.txt").write_text("new\n", encoding="utf-8")
        called = []

        def tidy_run(root):
            self.assertEqual(self.state.attempts(), [])
            called.append("tidy")
            path.write_text(json.dumps(data), encoding="utf-8")
            return {"code": 0, "said": "updated\n", "mac": None}

        def checkouts(graph, root):
            self.assertEqual(self.state.attempts(), [])
            called.append("ownership")
            self.assertEqual(graph.nodes["inventory"]["owns"], ["new_input.txt"])
            return []

        def action(name):
            def run(root):
                called.append(name)
                return {"outcome": "pass", "findings": [], "detail": {}}
            return Action(run, ())

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(board_graph, "checkout_problems", side_effect=checkouts), \
                mock.patch.object(board_graph, "lfs_content", return_value=([], "")):
            code = next_step(self.graph, self.state, root=self.root, out=out, err=err, tidy_run=tidy_run,
                             actions={"baseline": action("old action"), "bom": action("new action")})
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(called, ["tidy", "ownership", "new action"])
        [attempt] = self.state.attempts("inventory")
        self.assertEqual(attempt["inputs"], {"new_input.txt": hash_path(self.root / "new_input.txt"), MANIFEST: None})
        self.assertEqual(self.state.position(self.graph, "board"), "decisions")

    def test_upkeep_refuses_an_invalid_updated_graph_before_any_attempt(self):
        path, _ = self.graph_file()

        def tidy_run(root):
            path.write_text("{invalid", encoding="utf-8")
            return {"code": 0, "said": "updated\n", "mac": None}

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(board_graph, "checkout_problems") as checkouts:
            code = next_step(self.graph, self.state, root=self.root, out=out, err=err,
                             tidy_run=tidy_run, actions=self.actions())
        self.assertEqual(code, 1)
        self.assertIn("the graph could not be refreshed after tidy", err.getvalue())
        checkouts.assert_not_called()
        self.assertEqual((self.state.attempts(), self.state.transitions()), ([], []))

    def test_upkeep_refuses_when_the_selected_card_was_removed(self):
        path, data = self.graph_file()
        data["nodes"] = [item for item in data["nodes"] if item["id"] != "automation"]

        def tidy_run(root):
            path.write_text(json.dumps(data), encoding="utf-8")
            return {"code": 0, "said": "updated\n", "mac": None}

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(board_graph, "checkout_problems", return_value=[]), \
                mock.patch.object(board_graph, "lfs_content", return_value=([], "")):
            code = next_step(self.graph, self.state, track="framework", node="automation", by="agent:test",
                             root=self.root, out=out, err=err, tidy_run=tidy_run, actions={})
        self.assertEqual(code, 1)
        self.assertIn("the refreshed graph removed this card from framework", err.getvalue())
        self.assertNotIn("the checkouts are not current", out.getvalue())
        self.assertEqual((self.state.attempts(), self.state.transitions()), ([], []))

    def step(self, actions, again=False, track="board", by=None, note="", take=False, node=None, views=None):
        out, err = io.StringIO(), io.StringIO()
        code = next_step(self.graph, self.state, track, again, root=self.root, actions=actions, out=out, err=err,
                         upkeep=self.upkeep, by=by, note=note, take=take, node=node, views=views)
        return code, out.getvalue(), err.getvalue()

    def seed_views(self, stored):
        """Placement passed once by the owner on view `stored`; then the board came back to it, the card next starts."""
        self.state.append(self.graph, "board", "placement", "pass", "routing_batch", "owner", "approved",
                          inputs={"view:placement": stored})
        self.state.append(self.graph, "board", "footprints", "pass", "placement", "test", "a part changed")

    def test_an_unchanged_view_passes_the_card_by_itself(self):
        self.seed_views("same")
        views = {"placement": lambda root: "same", "copper_and_silk": lambda root: "x"}
        code, out, err = self.step(self.actions(), views=views)
        self.assertEqual(code, 0, err)
        last = self.state.transitions("board")[-1]
        self.assertEqual((last["node"], last["outcome"], last["actor"], last["target"]),
                         ("placement", "pass", "script:unchanged", "routing_batch"))
        self.assertEqual(last["inputs"]["view:placement"], "same")
        self.assertIn("passes by itself", out)
        self.assertIn("that pass stands", last["note"])
        self.assertEqual([a for a in self.state.attempts() if a["finished_at"] is None], [])
        self.assertEqual(self.upkeeps[0][1], "before comparing its view")

    def test_a_changed_view_starts_the_card_as_usual(self):
        self.seed_views("same")
        views = {"placement": lambda root: "moved", "copper_and_silk": lambda root: "x"}
        code, out, err = self.step(self.actions(), by="owner", note="look", views=views)
        self.assertEqual(code, 2, err)
        self.assertEqual(self.state.position(self.graph, "board"), "placement")
        self.assertEqual(len([a for a in self.state.attempts() if a["finished_at"] is None]), 1)
        self.assertNotIn("passes by itself", out)

    def test_no_earlier_pass_means_no_comparison(self):
        self.state.append(self.graph, "board", "footprints", "pass", "placement", "test", "first time")
        views = {"placement": lambda root: "same", "copper_and_silk": lambda root: "x"}
        code, out, err = self.step(self.actions(), by="owner", note="look", views=views)
        self.assertEqual(code, 2, err)
        self.assertEqual(self.state.position(self.graph, "board"), "placement")

    def seed_return(self, stored):
        """Placement passed by the owner on view `stored`; then Routing batch returned the work to it."""
        self.state.append(self.graph, "board", "placement", "pass", "routing_batch", "owner", "approved",
                          inputs={"view:placement": stored})
        self.state.append(self.graph, "board", "routing_batch", "returned", "placement", "agent:test",
                          "a part sits on a track")

    def test_a_returned_card_does_not_pass_by_itself_on_an_unchanged_view(self):
        self.seed_return("same")
        views = {"placement": lambda root: "same", "copper_and_silk": lambda root: "x"}
        text = render(where(self.graph, self.state))
        self.assertIn("Does not pass by itself now: Routing batch returned the work to it", text)
        self.assertNotIn("Passes by itself when", text)
        self.assertIn("Does not pass by itself now: Routing batch returned the work to it",
                      board_graph.brief(self.graph, self.state, "placement", root=self.root))
        code, out, err = self.step(self.actions(), by="owner", note="fix it", views=views)
        self.assertEqual(code, 2, err)
        self.assertIn("Placement does not pass by itself: Routing batch returned the work to it", out)
        self.assertNotIn("passes by itself:", out)
        self.assertEqual(self.state.position(self.graph, "board"), "placement")
        self.assertNotIn(board_graph.UNCHANGED, [row["actor"] for row in self.state.transitions("board")])
        self.assertEqual(len([a for a in self.state.attempts() if a["finished_at"] is None]), 1)

    def test_a_returned_card_passes_by_itself_again_after_its_own_pass(self):
        self.seed_return("same")
        self.state.append(self.graph, "board", "placement", "pass", "routing_batch", "owner", "fixed",
                          inputs={"view:placement": "same"})
        self.state.append(self.graph, "board", "footprints", "pass", "placement", "test", "a part changed")
        self.assertIn("Passes by itself when", render(where(self.graph, self.state)))
        views = {"placement": lambda root: "same", "copper_and_silk": lambda root: "x"}
        code, out, err = self.step(self.actions(), views=views)
        self.assertEqual(code, 0, err)
        last = self.state.transitions("board")[-1]
        self.assertEqual((last["node"], last["outcome"], last["actor"]), ("placement", "pass", board_graph.UNCHANGED))
        self.assertIn("passes by itself", out)

    def chain(self, models=None, models_error=None):
        """Fake datasheets and models scripts that read input.txt: datasheets passes, models reports `models`
        (a pass when None) or raises `models_error`. Each run is noted in self.ran."""
        self.ran = []

        def script(name, result=None, error=None):
            def run(root):
                self.ran.append(name)
                if error:
                    raise error
                return result or {"outcome": "pass", "findings": [], "detail": {}}
            return Action(run, ("input.txt",))
        return {"datasheets": script("datasheets"), "models": script("models", models, models_error)}

    def test_next_starts_the_card_in_line_runs_its_script_and_stops_there(self):
        self.seed("datasheets")
        code, out, err = self.step(self.chain())
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ran, ["datasheets"])  # one card per next: the move is the owner's, card by card
        self.assertEqual(self.upkeeps, [("datasheets", "before its script", 0)])
        self.assertEqual(self.state.position(self.graph, "board"), "models")
        for text in ("datasheets pass -> models recorded", "board is between datasheets and models",
                     "Board: between Datasheets and Models (auto)", "nothing on it since", "next starts it"):
            self.assertIn(text, out)
        self.assertEqual(out.count("Board: "), 1)  # the track's picture once, where the marker is now
        code, out, err = self.step(self.chain())
        self.assertEqual((code, self.ran), (0, ["models"]))
        self.assertEqual(self.state.position(self.graph, "board"), "footprints")
        self.assertEqual([(row["node"], row["actor"]) for row in self.state.transitions("board")[-2:]],
                         [("datasheets", "script:datasheets"), ("models", "script:models")])
        # Footprints' script is not connected here: next starts the card for whoever runs it elsewhere.
        code, out, err = self.step(self.chain())
        self.assertEqual(code, 1)
        self.assertIn("Footprints is a person's card (auto): say who starts it", err)
        code, out, err = self.step(self.chain(), by="agent:mac", note="on the Mac")
        self.assertEqual(code, 2, err)
        self.assertIn("Board: on Footprints (auto)", out)
        self.assertIn("Footprints is on: working since", out)
        self.assertIn("by agent:mac (under a minute ago): on the Mac", out)
        self.assertIn("Its script footprints has no function in board_graph_actions.py or the project's "
                      "scripts/board_graph_project_actions.py and is not connected to next: "
                      "run it where it runs and record the outcome with record; that record closes it.", out)
        self.assertEqual(self.upkeeps[-1], ("footprints", "starting it for agent:mac", 2))
        [held] = self.state.attempts("footprints", open_only=True)
        self.assertEqual((held["actor"], held["action"], held["note"]), ("agent:mac", "agent", "on the Mac"))

    def test_an_outcome_other_than_pass_keeps_the_track_on_the_card(self):
        self.seed("datasheets")
        self.step(self.chain())
        code, out, _ = self.step(self.chain({"outcome": "needs_lead", "findings": ["J5 height"], "detail": {}}))
        self.assertEqual((code, self.ran), (0, ["models"]))
        self.assertEqual(self.state.position(self.graph, "board"), "models")
        self.assertIn("board stays at models, nothing on it", out)
        self.assertIn("Board: between Datasheets and Models (auto)", out)

    def test_a_persons_card_is_started_for_by_and_left_on_until_its_record(self):
        code, out, _ = self.step(self.actions())
        self.assertEqual((code, self.calls), (0, 1))
        self.assertIn("Board: between Inventory and Decisions (owner)", out)
        code, out, err = self.step(self.actions())
        self.assertEqual(code, 1)
        self.assertIn("Decisions is a person's card (owner): say who starts it, --by owner or --by agent:<name>", err)
        code, out, _ = self.step(self.actions(), by="owner", note="deciding")
        self.assertEqual(code, 2)
        self.assertIn("Board: on Decisions (owner)", out)
        self.assertIn("Decisions is on: working since", out)
        self.assertIn("by owner (under a minute ago): deciding", out)
        self.assertIn("Do its task and record the outcome with record; that record closes it.", out)
        self.assertEqual(self.upkeeps, [("inventory", "before its script", 0),
                                        ("decisions", "starting it for owner", 1)])
        code, out, _ = self.step(self.actions(), by="owner")
        self.assertEqual(code, 2)
        self.assertIn("Decisions is on: working since", out)
        self.assertIn("Its next record closes it; --take starts it anew for someone else.", out)
        self.assertEqual(len(self.state.attempts("decisions")), 1)
        self.assertEqual(len(self.upkeeps), 2)  # an already-active card is a status query
        code, out, _ = self.step(self.actions(), by="agent:other", take=True)
        self.assertEqual(code, 2)
        self.assertIn("took Decisions over from owner", out)
        self.assertEqual(self.upkeeps[-1], ("decisions", "starting it for agent:other", 2))
        rows = self.state.attempts("decisions")
        self.assertEqual([(row["actor"], row["outcome"]) for row in rows], [("owner", "taken"), ("agent:other", None)])
        (self.root / "report.json").write_text("{}\n", encoding="utf-8")
        item = record(self.graph, self.state, "decisions", "pass", "owner", "decided", ("report.json",), root=self.root,
                      attempt_id=rows[1]["run_id"])
        self.assertEqual([entry["run_id"] for entry in item["closed"]], [rows[1]["run_id"]])
        self.assertEqual(self.state.position(self.graph, "board"), "constraints")

    def test_a_script_that_never_finished_holds_the_card_until_again(self):
        self.seed("datasheets")
        owns = set(self.graph.nodes["datasheets"]["owns"]) | {"input.txt"}
        inputs = {name: hash_path(self.root / name) for name in sorted(owns)}
        interrupted = self.state.start_attempt("datasheets", "datasheets", inputs, board_graph.now())
        code, out, err = self.step(self.chain())
        self.assertEqual((code, self.ran), (2, []))
        self.assertIn("Board: on Datasheets (auto)", out)
        self.assertIn("Datasheets is on: working since", out)
        self.assertIn("A run that never finished: inspect and settle its exact attempt before --again.", out)
        board_graph.settle(self.graph, self.state, "datasheets", "agent:lead", root=self.root, attempt_id=interrupted)
        code, out, err = self.step(self.chain(), again=True)
        self.assertEqual((code, self.ran), (0, ["datasheets"]))
        self.assertEqual(self.state.position(self.graph, "board"), "models")
        self.assertTrue(all(row["finished_at"] for row in self.state.attempts("datasheets")))
        self.assertTrue(interrupted)

    def test_a_failure_stops_with_exit_1_and_the_card_stays(self):
        self.seed("datasheets")
        self.step(self.chain())
        code, out, err = self.step(self.chain(models_error=ValueError("no model folder")))
        self.assertEqual((code, self.ran), (1, ["models"]))
        self.assertIn("ValueError: no model folder", err)
        self.assertIn("next stops: Models's script models failed (above).\n", out)
        self.assertEqual(self.state.position(self.graph, "board"), "models")

    def test_the_upkeep_runs_before_the_attempt_row_and_a_refusal_runs_nothing(self):
        self.assertEqual(self.step(self.actions())[0], 0)
        self.assertEqual(self.upkeeps, [("inventory", "before its script", 0)])
        self.current = False
        self.seed("bom")
        code, _, err = self.step(self.actions())
        self.assertEqual(code, 1)
        self.assertIn("checkouts are not current", err)
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(self.state.attempts()), 1)
        self.assertEqual(self.state.position(self.graph, "board"), "bom")

    def test_upkeep_at_a_persons_node_and_a_script_not_connected_here(self):
        self.seed("decisions")
        self.assertEqual(self.step(self.actions(), by="owner")[0], 2)
        self.assertEqual(self.upkeeps, [("decisions", "starting it for owner", 0)])
        self.seed("board_checks")
        code, out, _ = self.step(self.actions(), by="agent:mac")
        self.assertEqual(code, 2)
        self.assertEqual(self.upkeeps, [("decisions", "starting it for owner", 0),
                                        ("board_checks", "starting it for agent:mac", 1)])
        self.assertIn("is not connected to next: run it where it runs", out)
        [held] = self.state.attempts("board_checks", open_only=True)
        record(self.graph, self.state, "board_checks", "more_work", "agent:mac", "closing", (), root=self.root,
               attempt_id=held["run_id"])
        self.current = False
        code, out, err = self.step(self.actions(), by="agent:mac")
        self.assertEqual(code, 1)
        self.assertIn("checkouts are not current", err)
        self.assertNotIn("is not connected to next", out)

    def test_actionless_card_prepares_before_start_and_status_query_does_not_prepare_again(self):
        self.assertIsNone(self.graph.nodes["automation"]["action"])
        code, out, err = self.step(self.actions(), track="framework", node="automation", by="agent:a", note="work")
        self.assertEqual(code, 2, err)
        self.assertEqual(self.upkeeps, [("automation", "starting it for agent:a", 0)])
        [attempt] = self.state.attempts("automation", open_only=True)
        self.assertEqual((attempt["actor"], attempt["note"]), ("agent:a", "work"))
        self.assertIn(attempt["brief"], out)
        self.assertEqual(self.calls, 0)

        self.current = False  # a status query must not rerun or depend on upkeep
        code, out, err = self.step(self.actions(), track="framework", node="automation", by="agent:b")
        self.assertEqual((code, err), (2, ""))
        self.assertIn("Graph execution adapters is on", out)
        self.assertEqual(self.upkeeps, [("automation", "starting it for agent:a", 0)])
        self.assertEqual([row["run_id"] for row in self.state.attempts("automation", open_only=True)],
                         [attempt["run_id"]])
        self.assertEqual(self.calls, 0)

    def test_failed_actionless_preparation_refuses_start_and_preserves_a_failed_takeover(self):
        self.assertIsNone(self.graph.nodes["automation"]["action"])
        self.current = False
        code, out, err = self.step(self.actions(), track="framework", node="automation", by="agent:a")
        self.assertEqual(code, 1)
        self.assertIn("checkouts are not current", err)
        self.assertIn("next stops: the checkouts are not current", out)
        self.assertNotIn("Brief:", out)
        self.assertEqual(self.upkeeps, [("automation", "starting it for agent:a", 0)])
        self.assertEqual((self.state.attempts(), self.state.transitions(), self.calls), ([], [], 0))

        self.current = True
        self.assertEqual(self.step(self.actions(), track="framework", node="automation", by="agent:a")[0], 2)
        [original] = self.state.attempts("automation", open_only=True)
        self.current = False
        code, out, err = self.step(self.actions(), track="framework", node="automation", by="agent:b", take=True)
        self.assertEqual(code, 1)
        self.assertIn("checkouts are not current", err)
        self.assertNotIn("took Graph execution adapters over", out)
        [still_open] = self.state.attempts("automation", open_only=True)
        self.assertEqual((still_open["run_id"], still_open["actor"]), (original["run_id"], "agent:a"))
        self.assertEqual(self.upkeeps[-1], ("automation", "starting it for agent:b", 1))
        self.assertEqual(self.calls, 0)

    def test_a_persons_card_with_a_connected_script_runs_it_for_them_and_leaves_the_card_on(self):
        self.seed("placement")
        found = {"outcome": "needs_lead", "findings": ["R1: turned to 90 degrees, approved at 0 degrees"],
                 "detail": {"parts_compared": 1}}
        base = self.actions(result=found)
        actions = dict(base, placement=base["bom"])
        carried = []
        out, err = io.StringIO(), io.StringIO()
        code = next_step(self.graph, self.state, "board", root=self.root, actions=actions, out=out, err=err,
                         upkeep=self.upkeep, by="owner", note="approving",
                         after_report=lambda name, message: carried.append((name, message)) or "carried\n")
        text = out.getvalue()
        self.assertEqual(code, 2, err.getvalue())
        self.assertEqual(self.upkeeps, [("placement", "starting it for owner", 0)])
        self.assertIn("Board: on Placement (owner)", text)
        self.assertIn("Placement is on: working since", text)
        self.assertIn("Its script placement reports needs_lead: 1 finding: R1: turned to 90 degrees, approved at 0 "
                      "degrees\nReport: board/reports/placement/placement.json", text)
        self.assertIn("These are starting baseline findings to resolve during this card's work", text)
        self.assertIn("carried\nDo its task and record the outcome with record, citing the report; that record "
                      "closes it.", text)
        [report] = self.reports()
        self.assertEqual(carried, [(report.relative_to(self.root).as_posix(),
                                    "Report: Placement's script for owner: 1 finding: R1: turned to 90 degrees, "
                                    "approved at 0 degrees")])
        self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["findings"], found["findings"])
        self.assertEqual(self.calls, 1)
        self.assertEqual([(row["actor"], row["action"], row["outcome"], row["report_path"] is not None)
                          for row in self.state.attempts("placement")],  # the card opens first, then its script runs
                         [("owner", "agent", None, False), ("script:placement", "placement", "needs_lead", True)])
        self.assertEqual(self.state.transitions("board")[-1]["node"], "footprints")  # nothing recorded: theirs
        # Started again while on: who is on it, and the script does not run again.
        code, text, _ = self.step(actions, by="owner", note="approving")
        self.assertEqual((code, self.calls), (2, 1))
        self.assertIn("Placement is on: working since", text)
        self.assertNotIn("Its script", text)

    def test_starting_a_persons_card_prints_its_brief_and_keeps_it_on_the_attempt(self):
        self.seed("placement")
        code, out, err = self.step(self.actions(), by="owner", note="approving")
        self.assertEqual(code, 2, err)
        [attempt] = [row for row in self.state.attempts("placement") if row["actor"] == "owner"]
        self.assertTrue(attempt["brief"].startswith("Brief: Placement (owner), card placement on Board\n"
                                                   "Board: on Placement (owner), stage Footprints, models, placement\n"),
                        attempt["brief"][:200])
        self.assertIn(attempt["brief"], out)
        for text in ("  on it: working since", "Read whole, in this order, before anything else",
                     "    docs/PRODUCT_TRUTH.md (missing here)\n    board/SPACE.md (missing here)\n"
                     "    board/DECISIONS.md (missing here)",
                     "Its files go straight to main, in small commits: board/owner_placement.json",
                     "Newest reports: none under board/reports/placement/", "Last on it: nobody yet",
                     "CI, this commit: not read", "CI: no run has written to the graph yet",
                     "Stop: 120 minutes or 3 failures of the same command on unchanged source inputs, a loop guard (the graph's for every card); past it, "
                     "stop and record where you are", "Started: now, by next, for the one reading this",
                     "Close it:", "record placement", f"--attempt {attempt['run_id']}", "pass -> routing_batch",
                     "Last transitions:"):
            self.assertIn(text, attempt["brief"])
        # Started again while on: next says who is on it, prints no brief, and the row keeps the first.
        code, out, err = self.step(self.actions(), by="owner", note="approving")
        self.assertIn("Placement is on: working since", out)
        self.assertNotIn("Brief:", out)
        [again] = [row for row in self.state.attempts("placement") if row["actor"] == "owner"]
        self.assertEqual(again["brief"], attempt["brief"])

    def test_started_routing_brief_prints_a_pasteable_command_for_this_attempt(self):
        profile = {"tasks": "motor/socket batch", "provider": "codex", "model": "gpt-6-astra",
                   "review_model": "gpt-6.1-terra", "effort": "low"}
        board = boards.Board("motor", "motor/board.json", first=False)
        template = board_graph_actions.routing_command(board, profile)
        scratch = self.root / "scratch db.sqlite3"
        self.state.selected_state_path = scratch
        self.seed("routing_batch")
        with mock.patch.dict(COMMANDS, routing_run=template):
            code, out, err = self.step(self.actions(), by="agent:astra", note="one socket connection")
        self.assertEqual(code, 2, err)
        [attempt] = self.state.attempts("routing_batch")
        [line] = [line.removeprefix("  Tool: ") for line in out.splitlines() if line.startswith("  Tool: ")]
        expected = template.replace("<id>", attempt["run_id"]).replace("<who>", board_graph.shell_quote("agent:astra"))
        expected += " --graph " + board_graph.shell_quote(self.graph.path.resolve())
        expected += " --state " + board_graph.shell_quote(scratch.resolve())
        expected += " --records " + board_graph.shell_quote(self.state.records.resolve())
        self.assertEqual(line, expected)
        self.assertIn("--board motor ", line)
        self.assertIn("--review-model ", line)

    def test_routing_command_includes_review_provider_only_when_configured(self):
        board = boards.Board("motor", "motor/board.json", first=False)
        plain_profile = {"tasks": "motor/tasks", "provider": "codex", "model": "gpt-6.1-terra"}
        self.assertNotIn("--review-provider", board_graph_actions.routing_command(board, plain_profile))
        cross_profile = {**plain_profile, "review_provider": "gemini", "review_model": "gemini-4-neon-high"}
        cmd = board_graph_actions.routing_command(board, cross_profile)
        self.assertIn("--review-provider ", cmd)
        self.assertIn("gemini", cmd)

    def test_brief_previews_any_card_without_starting_it(self):
        self.graph.data["open_issues"].append({"id": "test_issue", "close_before": ["routing_batch"], "nets": ["/X"],
                                               "issue": "the test's own issue, in full"})
        text = board_graph.brief(self.graph, self.state, "routing_batch", root=self.root)
        self.assertTrue(text.startswith("Brief: Routing batch (agent), card routing_batch on Board\n"
                                        "  not where Board stands (inventory): a preview, until the track reaches it\n"
                                        "  Task: " + self.graph.nodes["routing_batch"]["task"] + "\n"), text[:300])
        self.assertIn("  Open issues to close before it passes, in full:\n", text)
        self.assertIn("    test_issue (nets /X): the test's own issue, in full\n", text)
        self.assertIn("Its files go on a branch and a pull request (board files; the owner merges): board/demo.kicad_pcb",
                      text)
        self.assertIn("  Start it: py -3 scripts/board_graph.py next --track board --by <owner | agent:name>", text)
        text = board_graph.brief(self.graph, self.state, "automation", root=self.root)
        self.assertIn("  on Framework (off the board path), a list: no outcome recorded yet\n  Task: Connect", text)
        self.assertIn("    docs/PRODUCT_TRUTH.md (missing here)\n    docs/BOARD_PIPELINE_CONTRACT.md (missing here)", text)
        self.assertIn("next --track framework --node automation --by", text)
        self.assertEqual(self.state.attempts(), [])  # a preview starts nothing

    def test_settle_records_needs_lead_only_for_a_thread_that_ended_without_a_record(self):
        self.seed("placement")
        item, said = board_graph.settle(self.graph, self.state, "placement", "agent:launcher", root=self.root)
        self.assertEqual((item, said), (None, "Placement: nothing open, and nothing was ever on it\n"))
        code, out, err = self.step(self.actions(), by="agent:fresh", note="trying")
        self.assertEqual(code, 2, err)
        [held] = self.state.attempts("placement", open_only=True)
        later = (board_graph.parse_time(held["started_at"]) + datetime.timedelta(minutes=50)).isoformat()
        item, said = board_graph.settle(self.graph, self.state, "placement", "agent:launcher", note="its window closed.",
                                       tokens=312000, root=self.root, at=later, attempt_id=held["run_id"])
        self.assertEqual(item["outcome"], "needs_lead")
        self.assertEqual(item["actor"], "agent:launcher")
        self.assertIn(f"The thread ended without a record: agent:fresh since {board_graph.stamp(held['started_at'])}, "
                      "50 min of the stop's 120; 312,000 tokens used. The lead looks at what "
                      "it left before anyone starts the card again. its window closed.", item["note"])
        self.assertTrue(said.startswith("Placement: settled by the launcher, needs_lead; agent:fresh since"))
        self.assertTrue(said.endswith("; 312,000 tokens kept on it\n"), said)
        self.assertEqual(self.state.attempts("placement", open_only=True), [])
        [done] = [row for row in self.state.attempts("placement") if row["actor"] == "agent:fresh"]
        self.assertEqual((done["outcome"], done["tokens"]), ("needs_lead", 312000))  # kept in the record's own write
        self.assertEqual(self.state.position(self.graph, "board"), "placement")
        item, said = board_graph.settle(self.graph, self.state, "placement", "agent:launcher", root=self.root)
        self.assertIsNone(item)
        self.assertRegex(said, r"^Placement: nothing open; the last attempt, agent:fresh since .*, ended .* with needs_lead\n$")
        # Every thread's close keeps its tokens, also after a normal record closed the card.
        with self.assertRaisesRegex(ValueError, "name --attempt"):
            board_graph.settle(self.graph, self.state, "placement", "agent:launcher", tokens=240000, root=self.root)
        item, said = board_graph.settle(self.graph, self.state, "placement", "agent:launcher", tokens=240000, root=self.root,
                                        attempt_id=held["run_id"])
        self.assertIsNone(item)
        self.assertTrue(said.endswith("with needs_lead; 240,000 tokens kept on it\n"), said)
        [done] = [row for row in self.state.attempts("placement") if row["actor"] == "agent:fresh"]
        self.assertEqual(done["tokens"], 240000)

    def test_a_persons_card_whose_script_raises_is_still_started(self):
        self.seed("placement")
        base = self.actions(error=RuntimeError("boom"))
        code, out, err = self.step(dict(base, placement=base["bom"]), by="owner", note="approving")
        self.assertEqual(code, 2, err)
        self.assertIn("Its script placement failed: RuntimeError: boom. Report: board/reports/placement/placement.json",
                      out)
        self.assertNotIn("starting baseline findings", out)
        self.assertEqual([(row["actor"], row["outcome"]) for row in self.state.attempts("placement")],
                         [("owner", None), ("script:placement", "error")])

    def test_a_lists_card_is_named_with_node_and_started_for_a_person(self):
        label = "Framework (off the board path)"
        code, out, err = self.step(self.actions(), track="framework")
        self.assertEqual(code, 1)
        self.assertIn(f"{label} is a list, not a row: name the card to start with --node, one of automation, usage, "
                      "shared_evidence", err)
        code, out, err = self.step(self.actions(), track="framework", node="inventory", by="agent:a")
        self.assertEqual(code, 1)
        self.assertIn(f"inventory is not a card of {label}; its cards are automation, usage, shared_evidence", err)
        code, out, err = self.step(self.actions(), track="board", node="bom", by="agent:a")
        self.assertEqual(code, 1)
        self.assertIn("Board stands at inventory, the card next starts; --node names a list's card", err)
        code, out, err = self.step(self.actions(), track="framework", node="automation")
        self.assertEqual(code, 1)
        self.assertIn("Graph execution adapters is a person's card (agent): say who starts it", err)
        code, out, err = self.step(self.actions(), track="framework", node="automation", by="agent:a", note="adapters")
        self.assertEqual(code, 2, err)
        self.assertIn(f"Brief: Graph execution adapters (agent), card automation on {label}\n"
                      f"  on {label}, a list: no outcome recorded yet\n  on it: working since", out)
        self.assertIn("Graph execution adapters is on: working since", out)
        self.assertIn("by agent:a (under a minute ago): adapters", out)
        self.assertIn("Do its task and record the outcome with record; that record closes it.", out)
        self.assertEqual(self.upkeeps, [("automation", "starting it for agent:a", 0)])
        [held] = self.state.attempts("automation", open_only=True)
        self.assertEqual((held["actor"], held["action"], held["note"]), ("agent:a", "agent", "adapters"))
        code, out, _ = self.step(self.actions(), track="framework", node="automation", by="agent:b")
        self.assertEqual(code, 2)
        self.assertIn("Graph execution adapters is on: working since", out)
        self.assertIn("Its next record closes it; --take starts it anew for someone else.", out)
        item = record(self.graph, self.state, "automation", "more_work", "agent:a", "a step", (), root=self.root,
                      attempt_id=held["run_id"])
        self.assertEqual([row["actor"] for row in item["closed"]], ["agent:a"])
        self.assertEqual(self.state.attempts("automation", open_only=True), [])
        self.assertEqual(self.state.position(self.graph, "board"), "inventory")  # a list's record moves no track

    def seed(self, until):
        """Walk the board track to `until` as the right people would have."""
        chain = self.graph.track_nodes("board")
        for node in chain[:chain.index(until)]:
            actor = "owner" if self.graph.nodes[node]["who"] == "owner" else "agent:test"
            self.state.append(self.graph, "board", node, "pass", self.graph.nodes[node]["outcomes"]["pass"],
                              actor, "seeded")

    def reports(self):
        folder = self.root / board_graph.REPORTS
        return sorted(folder.glob("*/*.json")) if folder.is_dir() else []

    def test_a_persons_card_is_started_for_the_person_and_records_nothing(self):
        self.seed("decisions")
        code, out, err = self.step(self.actions())
        self.assertEqual(code, 1)
        self.assertIn("Decisions is a person's card (owner): say who starts it", err)
        code, out, _ = self.step(self.actions(), by="owner")
        self.assertEqual(code, 2)
        self.assertIn("Board: on Decisions (owner)", out)
        self.assertIn("Decisions is on: working since", out)
        base = ["--state", str(self.state.path), "--records", str(self.state.records)]
        with contextlib.redirect_stdout(io.StringIO()) as printed, contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(board_graph, "bring_current", side_effect=AssertionError("status ran upkeep")):
            self.assertEqual(board_graph.main(base + ["next", "--by", "owner"]), 2)
        self.assertIn("Decisions is on: working since", printed.getvalue())
        self.assertEqual(len(self.state.transitions()), 1)
        self.seed("placement")
        before = self.state.transitions()
        code, out, _ = self.step(self.actions(), by="owner")
        self.assertEqual((code, "Placement is on: working since" in out), (2, True))
        self.assertEqual(self.state.transitions(), before)
        self.assertEqual(self.calls, 0)
        self.assertEqual([(row["node"], row["action"], row["outcome"]) for row in self.state.attempts()],
                         [("decisions", "agent", None), ("placement", "agent", None)])
        self.assertEqual(self.reports(), [])

    def test_a_script_that_needs_kicad_or_the_mac_is_started_for_whoever_runs_it_there(self):
        self.seed("package")
        with mock.patch.object(board_graph, "LATER", ("package",)):  # the electronics pack has none of its own now
            code, out, _ = self.step(self.actions(), by="agent:mac")
        self.assertEqual(code, 2)
        self.assertIn("Its script package needs KiCad or the Mac and is not connected to next: run it where it "
                      "runs and record the outcome with record; that record closes it.", out)
        self.assertEqual([(row["actor"], row["action"]) for row in self.state.attempts()], [("agent:mac", "agent")])
        self.assertEqual(self.state.position(self.graph, "board"), "package")

    def test_an_interrupted_attempt_blocks_until_again(self):
        with self.assertRaises(KeyboardInterrupt):
            self.step(self.actions(error=KeyboardInterrupt()))
        [attempt] = self.state.attempts("inventory")
        self.assertIsNone(attempt["finished_at"])
        code, out, _ = self.step(self.actions())
        self.assertEqual(code, 2)
        self.assertIn("Inventory is on: working since", out)
        self.assertIn("A run that never finished: inspect and settle its exact attempt before --again.", out)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.state.transitions(), [])
        board_graph.settle(self.graph, self.state, "inventory", "agent:lead", root=self.root, attempt_id=attempt["run_id"])
        code, _, _ = self.step(self.actions(), again=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.state.position(self.graph, "board"), "decisions")

    def test_unresolved_findings_block_until_again(self):
        self.seed("bom")
        lead = {"outcome": "needs_lead", "findings": ["C1: wrong part"], "detail": {}}
        code, out, _ = self.step(self.actions(lead))
        self.assertEqual(code, 0)
        self.assertIn("bom needs_lead -> bom recorded", out)
        self.assertIn("  C1: wrong part", out)
        last = self.state.transitions("board")[-1]
        self.assertEqual((last["outcome"], last["actor"], last["note"]),
                         ("needs_lead", "script:bom", "1 finding: C1: wrong part"))
        self.assertEqual(self.state.position(self.graph, "board"), "bom")
        code, _, err = self.step(self.actions(lead))
        self.assertEqual(code, 1)
        self.assertIn("findings are unresolved", err)
        self.assertIn("--again", err)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.step(self.actions(lead), again=True)[0], 0)
        self.assertEqual(self.calls, 2)

    def test_a_return_and_the_way_back_count_as_a_changed_position(self):
        self.seed("bom")
        lead = {"outcome": "needs_lead", "findings": ["C1: wrong part"], "detail": {}}
        self.step(self.actions(lead))
        self.state.append(self.graph, "board", "bom", "returned", "schematic", "agent:test", "fix the symbol")
        self.state.append(self.graph, "board", "schematic", "pass", "bom", "agent:test", "nothing to change")
        self.assertEqual(self.step(self.actions(lead))[0], 0)
        self.assertEqual(self.calls, 2)

    def test_a_changed_input_allows_a_new_run(self):
        lead = {"outcome": "needs_lead", "findings": ["unreadable"], "detail": {}}
        self.step(self.actions(lead))
        self.assertEqual(self.step(self.actions(lead))[0], 1)
        (self.root / "input.txt").write_text("second\n", encoding="utf-8")
        self.assertEqual(self.step(self.actions())[0], 0)
        self.assertEqual(self.calls, 2)
        first, second = self.state.attempts("inventory")
        self.assertNotEqual(first["inputs"], second["inputs"])
        self.assertEqual(second["inputs"], {"input.txt": hash_path(self.root / "input.txt"), MANIFEST: None})

    def test_a_new_version_of_the_tool_is_a_changed_input(self):
        """The tool's manifest is hashed with what the script reads: the same version holds an unresolved attempt
        until --again, and an update of the tool lets the card's script run again by itself."""
        manifest = self.root / MANIFEST
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text('{"name": "board-graph", "version": "0.4.0"}\n', encoding="utf-8")
        lead = {"outcome": "needs_lead", "findings": ["the board is not there yet"], "detail": {}}
        self.assertEqual(self.step(self.actions(lead))[0], 0)
        [first] = self.state.attempts("inventory")
        self.assertEqual(first["inputs"], {"input.txt": hash_path(self.root / "input.txt"),
                                           MANIFEST: hash_path(manifest)})
        code, _, err = self.step(self.actions(lead))
        self.assertEqual((code, self.calls), (1, 1))
        self.assertIn("nothing it reads has changed, the tool is the same version and the position has not moved", err)
        manifest.write_text('{"name": "board-graph", "version": "0.4.1"}\n', encoding="utf-8")
        code, out, _ = self.step(self.actions())
        self.assertEqual((code, self.calls), (0, 2))
        self.assertIn("inventory pass -> decisions recorded", out)
        first, second = self.state.attempts("inventory")
        self.assertNotEqual(first["inputs"][MANIFEST], second["inputs"][MANIFEST])
        self.assertEqual(first["inputs"]["input.txt"], second["inputs"]["input.txt"])

    def test_the_attempt_row_exists_before_the_action_runs(self):
        seen = []

        def check(root):
            with contextlib.closing(sqlite3.connect(str(self.state.path))) as db:
                seen.extend(db.execute("SELECT node, action, started_at, finished_at FROM attempts").fetchall())
            seen.append(self.reports())

        self.assertEqual(self.step(self.actions(check=check))[0], 0)
        (node, action, started_at, finished_at), reports = seen
        self.assertEqual((node, action, finished_at, reports), ("inventory", "baseline", None, []))
        self.assertTrue(started_at)

    def test_an_action_that_raises_leaves_an_error_attempt_and_no_transition(self):
        code, _, err = self.step(self.actions(error=ValueError("cannot read the board")))
        self.assertEqual(code, 1)
        self.assertIn("ValueError: cannot read the board", err)
        [attempt] = self.state.attempts("inventory")
        self.assertEqual(attempt["outcome"], "error")
        self.assertTrue(attempt["finished_at"])
        report = json.loads((self.root / attempt["report_path"]).read_text(encoding="utf-8"))
        self.assertEqual(report["outcome"], "error")
        self.assertEqual(report["detail"]["error"], "ValueError: cannot read the board")
        self.assertEqual(self.state.transitions(), [])
        self.assertEqual(self.state.position(self.graph, "board"), "inventory")
        code, _, err = self.step(self.actions())
        self.assertEqual(code, 1)
        self.assertIn("error that is unresolved", err)

    def test_a_card_someone_is_on_must_close_before_its_script_runs_again(self):
        attempt, _, _ = self.state.open_work(self.graph, "inventory", "agent:lead", "", {}, str(self.root))
        code, out, _ = self.step(self.actions())
        self.assertEqual((code, self.calls), (2, 0))
        self.assertIn("Inventory is on: working since", out)
        self.assertEqual(self.step(self.actions(), again=True)[0], 2)
        self.assertEqual(self.calls, 0)
        board_graph.settle(self.graph, self.state, "inventory", "agent:lead", root=self.root, attempt_id=attempt["run_id"])
        self.assertEqual(self.step(self.actions(), again=True)[0], 0)
        work, run = self.state.attempts("inventory")
        self.assertEqual((work["actor"], work["outcome"], run["actor"], run["outcome"]),
                         ("agent:lead", "needs_lead", "script:baseline", "pass"))
        self.assertEqual(self.state.attempts(open_only=True), [])

    def test_a_result_in_the_wrong_shape_is_an_error(self):
        code, _, err = self.step(self.actions({"outcome": "ok", "findings": [], "detail": {}}))
        self.assertEqual(code, 1)
        self.assertIn("not an outcome of pass or needs_lead", err)
        self.assertEqual(self.state.transitions(), [])

    def test_a_passing_action_moves_the_position_and_writes_the_report(self):
        code, out, _ = self.step(self.actions())
        self.assertEqual(code, 0)
        self.assertEqual(self.state.position(self.graph, "board"), "decisions")
        [path] = self.reports()
        name = path.relative_to(self.root).as_posix()
        [attempt] = self.state.attempts("inventory")
        self.assertEqual(name, "board/reports/inventory/baseline.json")  # one path per card, replaced each run
        report = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(report["inputs"], {"input.txt": hash_path(self.root / "input.txt"), MANIFEST: None})
        self.assertEqual(report["inputs"], attempt["inputs"])
        self.assertEqual({key: report[key] for key in ("node", "action", "run_id", "started_at", "finished_at",
                                                       "outcome", "findings", "detail")},
                         {"node": "inventory", "action": "baseline", "run_id": attempt["run_id"],
                          "started_at": attempt["started_at"], "finished_at": attempt["finished_at"],
                          "outcome": "pass", "findings": [], "detail": {"lines": 1}})
        self.assertEqual((attempt["report_path"], attempt["report_sha256"]), (name, hash_path(path)))
        [transition] = self.state.transitions()
        self.assertEqual((transition["actor"], transition["note"], transition["evidence"]),
                         ("script:baseline", "no findings", [{"path": name, "sha256": hash_path(path)}]))
        self.assertEqual(len(list((self.root / "records").glob("*-inventory-pass.json"))), 1)
        for text in ("inventory pass -> decisions recorded at", "board is between inventory and decisions",
                     "No findings.", "Board: between Inventory and Decisions (owner)"):
            self.assertIn(text, out)

    def test_what_a_script_says_that_is_not_a_finding_is_in_the_record_and_printed(self):
        said = ["schematic stage: there is no board file yet", "not there yet: board/x.kicad_pcb (the board)"]
        code, out, _ = self.step(self.actions({"outcome": "pass", "findings": [], "detail": {"notes": said}}))
        self.assertEqual(code, 0)
        [transition] = self.state.transitions()
        self.assertEqual((transition["outcome"], transition["note"]),
                         ("pass", "no findings; schematic stage: there is no board file yet; not there yet: "
                                  "board/x.kicad_pcb (the board)"))
        self.assertIn("No findings.\nNote: schematic stage: there is no board file yet\n"
                      "Note: not there yet: board/x.kicad_pcb (the board)\n", out)
        self.assertEqual(board_graph.findings_note(["a", "b"], ["c"]), "2 findings: a; b; c")

    def test_notes_that_are_not_a_list_of_sentences_are_an_error(self):
        code, _, err = self.step(self.actions({"outcome": "pass", "findings": [], "detail": {"notes": "one string"}}))
        self.assertEqual(code, 1)
        self.assertIn("detail.notes is one string, not a list of sentences", err)
        self.assertEqual(self.state.transitions(), [])


SCHEMATIC_FILES = ("board/demo.kicad_sch", "board/demo.kicad_sym", "board/demo-parts.kicad_sym",
                   "board/demo.kicad_pro", "board/demo.kicad_dru")


def write(root, files):
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(text.encode("utf-8"))


def git_settings(root):
    """Git with no user or system configuration and a fixed author, while the patch is active."""
    return mock.patch.dict(os.environ, {
        "GIT_CONFIG_GLOBAL": str(root / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com"})


class GitWork:
    """GitHub, offline: a bare origin; `seed`, a clone that plays the owner merging on GitHub; `pc`, this computer's
    checkout on main, where next runs. Pull request 84 is open: its head changes the schematic and adds a report
    folder. Git reads no user or system configuration while it runs. The repositories are made once and copied
    for each test (remotes are relative paths, so a copy pushes to its own origin): git is slow to start on Windows.
    """

    made = None

    def __init__(self, base):
        template, self.head = self.template()
        self.env = git_settings(template)
        self.env.start()
        shutil.copytree(template / "work", base, dirs_exist_ok=True)
        self.base = base
        self.origin, self.seed, self.pc = base / "origin.git", base / "seed", base / "pc"

    @classmethod
    def template(cls):
        if cls.made is None:
            directory = tempfile.TemporaryDirectory()
            root = pathlib.Path(directory.name)
            (root / "gitconfig").write_text("", encoding="utf-8")
            work = root / "work"
            work.mkdir()
            with git_settings(root):
                git_text(work, "init", "-q", "--bare", "origin.git")
                git_text(work / "origin.git", "symbolic-ref", "HEAD", "refs/heads/main")
                git_text(work, "clone", "-q", "origin.git", "seed")
                git_text(work / "seed", "symbolic-ref", "HEAD", "refs/heads/main")
                git_text(work / "seed", "remote", "set-url", "origin", "../origin.git")
                maker = cls.__new__(cls)
                maker.seed = work / "seed"
                maker.commit({name: f"{name} 1\n" for name in SCHEMATIC_FILES}, "start")
                git_text(maker.seed, "push", "-q", "origin", "main")
                git_text(work, "clone", "-q", "origin.git", "pc")
                git_text(work / "pc", "remote", "set-url", "origin", "../origin.git")
                head = maker.open_pull_request(84, {"board/demo.kicad_sch": "schematic, readable\n",
                                                    "board/reports/pr84/critic.md": "no open finding\n"})
            cls.made = (directory, root, head)
        return cls.made[1], cls.made[2]

    def stop(self):
        self.env.stop()

    def commit(self, files, message, where=None):
        where = where or self.seed
        write(where, files)
        git_text(where, "add", "-A")
        git_text(where, "commit", "-q", "-m", message)
        return git_text(where, "rev-parse", "HEAD").strip()

    def open_pull_request(self, number, files):
        """A branch with `files`, published as pull request `number`'s head."""
        git_text(self.seed, "checkout", "-q", "-b", f"pr{number}")
        head = self.commit(files, f"pull request {number}")
        git_text(self.seed, "push", "-q", "origin", f"pr{number}:refs/pull/{number}/head")
        git_text(self.seed, "checkout", "-q", "main")
        return head

    def merge(self, number):
        git_text(self.seed, "merge", "-q", "--no-ff", f"pr{number}", "-m", f"Merge pull request #{number}")
        git_text(self.seed, "push", "-q", "origin", "main")
        return git_text(self.seed, "rev-parse", "HEAD").strip()

    def push_to_main(self, files, message):
        self.commit(files, message)
        git_text(self.seed, "push", "-q", "origin", "main")


class MergeTests(unittest.TestCase):
    """A needs_owner that asks for a merge, and next checking that merge: GitHub and tidy are stand-ins."""

    def setUp(self):
        self.graph = Graph(FIXTURE_GRAPH)
        self.directory = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.directory.name)
        self.work = GitWork(base)
        self.state = State(base / "state.sqlite3", base / "records")
        for node in ("inventory", "decisions", "constraints"):
            self.state.append(self.graph, "board", node, "pass", self.graph.nodes[node]["outcomes"]["pass"],
                              "owner" if node == "decisions" else "agent:test", "seeded")
        self.pr = {"state": "OPEN", "isDraft": True, "mergeCommit": None, "mergedAt": None,
                   "statusCheckRollup": [{"name": "validate", "status": "COMPLETED", "conclusion": "SUCCESS"}]}
        self.tidied, self.tidy_result = [], {"code": 0, "mac": None}
        self.head = self.work.head
        self.pr["headRefOid"] = self.head

    def tearDown(self):
        self.work.stop()
        self.directory.cleanup()

    def github(self, number):
        self.assertEqual(number, 84)
        return dict(self.pr)

    def tidy_run(self, root):
        """Stands in for tidy --apply: fetch and fast-forward main, and say so."""
        self.tidied.append(pathlib.Path(root))
        if self.tidy_result["code"]:
            return dict(self.tidy_result, said="git fetch failed; nothing done\n")
        git_text(root, "fetch", "-q", "origin")
        behind = int(git_text(root, "rev-list", "--count", "HEAD..origin/main"))
        git_text(root, "merge", "-q", "--ff-only", "origin/main")
        said = f"main is up to date ({root})\n" if not behind else f"fast-forward main by {behind} commit(s) ({root})\n"
        if self.tidy_result["mac"]:
            said += f"Mac: skipped ({self.tidy_result['mac']})\n"
        return dict(self.tidy_result, said=said)

    def ask(self, **options):
        return record(self.graph, self.state, "schematic", "needs_owner", "agent:test", "merge pull request 84?",
                      options.pop("evidence", ("board/reports/pr84",)), root=self.work.pc,
                      pull_request=options.pop("pull_request", 84), then=options.pop("then", "pass"),
                      github=self.github)

    def step(self):
        out, err = io.StringIO(), io.StringIO()
        code = next_step(self.graph, self.state, root=self.work.pc, out=out, err=err, tidy_run=self.tidy_run,
                         github=self.github)
        return code, out.getvalue(), err.getvalue()

    def merged(self):
        self.pr.update(state="MERGED", isDraft=False, mergeCommit={"oid": self.work.merge(84)},
                       mergedAt="2026-11-05T17:04:36Z")

    def graph_file(self, data):
        path = self.work.base / "definition/tools/board_graph.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        self.graph = Graph(path)
        return path

    def step_with_graph_update(self, path, data):
        def update(root):
            result = self.tidy_run(root)
            path.write_text(json.dumps(data), encoding="utf-8")
            return result
        out, err = io.StringIO(), io.StringIO()
        code = next_step(self.graph, self.state, root=self.work.pc, out=out, err=err, tidy_run=update,
                         github=self.github)
        return code, out.getvalue(), err.getvalue()

    def test_merge_uses_the_definition_updated_during_upkeep(self):
        data = json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))
        data["open_issues"].append({"id": "old_blocker", "close_before": ["schematic"], "nets": [],
                                    "issue": "Resolved in the merged definition."})
        path = self.graph_file(data)
        self.ask()
        self.merged()
        data["open_issues"] = [issue for issue in data["open_issues"] if issue["id"] != "old_blocker"]
        code, out, err = self.step_with_graph_update(path, data)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.tidied, [self.work.pc])
        self.assertEqual(self.state.transitions("board")[-1]["outcome"], "pass")
        self.assertEqual(self.state.position(self.graph, "board"), "bom")
        self.assertIn("board is between schematic and bom", out)

    def test_merge_still_refuses_a_blocker_added_by_the_updated_definition(self):
        data = json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))
        path = self.graph_file(data)
        self.ask()
        self.merged()
        data["open_issues"].append({"id": "new_blocker", "close_before": ["schematic"], "nets": [],
                                    "issue": "Must be resolved before passing."})
        code, _, err = self.step_with_graph_update(path, data)
        self.assertEqual(code, 1)
        self.assertIn("cannot pass while open issues name it: new_blocker", err)
        self.assertEqual(self.state.transitions("board")[-1]["outcome"], "needs_owner")
        self.assertEqual(self.state.merge_question("board")["number"], 84)

    def test_merge_upkeep_refuses_a_dirty_file_newly_owned_by_the_definition(self):
        data = json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))
        path = self.graph_file(data)
        self.ask()
        self.merged()
        next(item for item in data["nodes"] if item["id"] == "schematic")["owns"].append("docs/newly-owned.txt")
        write(self.work.pc, {"docs/newly-owned.txt": "uncommitted\n"})
        code, _, err = self.step_with_graph_update(path, data)
        self.assertEqual(code, 1)
        self.assertIn("has uncommitted changes to files a node owns: docs/newly-owned.txt", err)
        self.assertEqual(self.state.attempts(), [])
        self.assertEqual(self.state.transitions("board")[-1]["outcome"], "needs_owner")

    def test_the_question_stores_the_expectation_at_the_pull_requests_head(self):
        item = self.ask()
        question = item["inputs"]["pull_request"]
        expected = {name: hashlib.sha256(f"{name} 1\n".encode()).hexdigest() for name in SCHEMATIC_FILES}
        expected["board/demo.kicad_sch"] = hashlib.sha256(b"schematic, readable\n").hexdigest()
        self.assertEqual(question, {"number": 84, "then": "pass", "head": self.head, "expected": expected,
                                    "evidence": ["board/reports/pr84"]})
        # The report folder exists only at the head; it is hashed there, as hash_path would hash it checked out.
        self.assertFalse((self.work.pc / "board/reports").exists())
        [evidence] = item["evidence"]
        self.assertEqual(evidence["at"], self.head)
        git_text(self.work.seed, "checkout", "-q", "pr84")
        self.assertEqual(evidence["sha256"], hash_path(self.work.seed / "board/reports/pr84"))
        # The inputs still hash this checkout's own owned files, as every record does.
        self.assertEqual(item["inputs"]["board/demo.kicad_sch"], hash_path(self.work.pc / "board/demo.kicad_sch"))
        self.assertEqual(self.state.merge_question("board")["number"], 84)
        self.assertIn("Waits on: the owner merging pull request 84; then next checks main's files and records pass",
                      render(where(self.graph, self.state)))

    def test_a_question_needs_needs_owner_an_open_pull_request_and_an_outcome_to_follow(self):
        with self.assertRaisesRegex(ValueError, "are for needs_owner"):
            record(self.graph, self.state, "schematic", "more_work", "agent:test", "x", (), root=self.work.pc,
                   pull_request=84, then="pass", github=self.github)
        with self.assertRaisesRegex(ValueError, "one of pass, more_work, needs_lead, needs_skill"):
            self.ask(then="returned")
        with self.assertRaisesRegex(ValueError, "one of pass"):
            self.ask(then=None)
        with self.assertRaisesRegex(ValueError, "evidence not found: board/reports/none .here or at pull request 84"):
            self.ask(evidence=("board/reports/none",))
        self.pr["state"] = "MERGED"
        with self.assertRaisesRegex(ValueError, "pull request 84 is merged, not open"):
            self.ask()
        self.assertEqual(self.state.position(self.graph, "board"), "schematic")
        self.assertIsNone(self.state.merge_question("board"))

    def test_next_while_the_pull_request_is_open_waits_and_runs_nothing(self):
        self.ask()
        code, out, _ = self.step()
        self.assertEqual(code, 2)
        self.assertIn("waiting for the owner: merge pull request 84", out)
        self.assertEqual((self.tidied, self.state.attempts(), self.state.upkeeps()), ([], [], []))
        self.assertEqual(self.state.transitions()[-1]["outcome"], "needs_owner")

    def use_scoped_schematic(self):
        data = json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))
        data["approval_mode"] = "enforce"
        data["docs"] = []  # This isolated PR fixture has no project-wide reading.
        schematic = next(node for node in data["nodes"] if node["id"] == "schematic")
        schematic["docs"] = []
        schematic["approval_files"] = ["board/demo.kicad_sch"]
        self.graph_file(data)

    def test_scoped_review_survives_an_unchanged_merge(self):
        self.use_scoped_schematic()
        self.ask()
        self.merged()
        code, _, err = self.step()
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.state.merge_question("board"))
        self.assertEqual(self.state.position(self.graph, "board"), "bom")
        last = self.state.transitions("board")[-1]
        self.assertEqual(last["actor"], "agent:test")
        self.assertEqual(last["inputs"]["reviewed_merge"]["head"], self.head)
        review = board_graph.approval_review(self.graph, self.state, self.work.pc)
        self.assertEqual(review.verdict("schematic")["status"], "current")

    def test_changed_merge_evidence_hands_the_card_back_to_a_reviewer(self):
        self.use_scoped_schematic()
        self.ask()
        self.merged()
        self.work.push_to_main({"board/reports/pr84/new.md": "extra findings\n"}, "revise review")
        code, _, err = self.step()
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.state.merge_question("board"))
        row = self.state.transitions("board")[-1]
        self.assertEqual((row["node"], row["outcome"]), ("schematic", "needs_lead"))
        self.assertIn("Fresh review", row["note"])
        code = next_step(self.graph, self.state, root=self.work.pc, by="agent:reviewer", actions={},
                         out=io.StringIO(), err=io.StringIO(), upkeep=lambda *args: True)
        self.assertEqual(code, 2)
        self.assertTrue(self.state.attempts("schematic", open_only=True))

    def test_next_after_the_merge_tidies_checks_main_and_records_the_outcome(self):
        self.ask()
        self.merged()
        code, out, err = self.step()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.tidied, [self.work.pc])
        self.assertEqual(self.state.position(self.graph, "board"), "bom")
        last = self.state.transitions("board")[-1]
        self.assertEqual((last["node"], last["outcome"], last["actor"]), ("schematic", "pass", "script:merge_check"))
        self.assertIn(f"pull request 84 merged as {self.pr['mergeCommit']['oid'][:8]}", last["note"])
        readable = hashlib.sha256(b"schematic, readable\n").hexdigest()[:8]
        self.assertIn(f"board/demo.kicad_sch {readable}", last["note"])
        [report, folder] = last["evidence"]
        self.assertEqual(folder, {"path": "board/reports/pr84",
                                  "sha256": hash_path(self.work.pc / "board/reports/pr84")})
        saved = json.loads((self.work.pc / report["path"]).read_text(encoding="utf-8"))
        self.assertEqual((saved["merge_commit"], saved["differ"], saved["outcome"]),
                         (self.pr["mergeCommit"]["oid"], [], "pass"))
        [upkeep] = self.state.upkeeps()
        self.assertEqual((upkeep["node"], upkeep["why"], upkeep["outcome"], upkeep["mac"], upkeep["problems"]),
                         ("schematic", "pull request 84 merged", "current", None, []))
        self.assertIn("fast-forward main by 2 commit(s)", upkeep["said"])
        self.assertTrue(upkeep["finished_at"])
        self.assertIn("Upkeep (tidy --apply):\n  fast-forward main by 2 commit(s)", out)
        [attempt] = self.state.attempts("schematic")
        self.assertEqual((attempt["action"], attempt["outcome"]), ("merge_check", "pass"))
        self.assertIsNone(self.state.merge_question("board"))
        # The merge check is one next: the track is between the cards now, and BOM waits for its own next.
        self.assertIn("board is between schematic and bom", out)
        self.assertIn("Board: between Schematic and BOM (auto)", out)
        self.assertEqual(self.state.attempts("bom"), [])

    def test_main_files_that_differ_from_the_question_record_nothing(self):
        self.ask()
        self.merged()
        self.work.push_to_main({"board/demo.kicad_sch": "schematic, changed after the merge\n"}, "later change")
        before = self.state.transitions()
        code, _, err = self.step()
        self.assertEqual(code, 1)
        self.assertIn("main's files are not the ones the question named", err)
        expected = hashlib.sha256(b"schematic, readable\n").hexdigest()[:8]
        found = hashlib.sha256(b"schematic, changed after the merge\n").hexdigest()[:8]
        self.assertIn(f"  board/demo.kicad_sch: expected {expected}, main has {found}", err)
        self.assertNotIn("board/demo.kicad_pro", err)
        self.assertEqual(self.state.transitions(), before)
        self.assertEqual(self.state.attempts("schematic")[-1]["outcome"], "mismatch")

    def test_a_tidy_that_fails_refuses_and_runs_nothing(self):
        self.ask()
        self.merged()
        self.tidy_result["code"] = 1
        code, _, err = self.step()
        self.assertEqual(code, 1)
        self.assertIn("tidy did not finish, so main here cannot be made current", err)
        self.assertIn("tidy said: git fetch failed; nothing done", err)
        self.assertEqual(self.state.attempts(), [])
        [upkeep] = self.state.upkeeps()
        self.assertEqual(upkeep["outcome"], "refused")
        self.assertEqual(self.state.position(self.graph, "board"), "schematic")

    def test_an_uncommitted_owned_file_on_main_here_refuses(self):
        self.ask()
        self.merged()
        write(self.work.pc, {"board/demo.kicad_pro": "edited by hand\n"})
        code, _, err = self.step()
        self.assertEqual(code, 1)
        self.assertIn("has uncommitted changes to files a node owns: board/demo.kicad_pro", err)
        self.assertEqual((self.state.attempts(), self.state.upkeeps()[-1]["outcome"]), ([], "refused"))

    def test_a_mac_that_is_not_reached_is_a_note_and_next_goes_on(self):
        self.ask()
        self.merged()
        self.tidy_result["mac"] = "ssh failed: Connection timed out"
        code, out, err = self.step()
        self.assertEqual(code, 0, err)
        self.assertIn("  Mac: skipped (ssh failed: Connection timed out)", out)
        self.assertIn("The Mac was not brought current; next goes on", out)
        self.assertEqual(self.state.upkeeps()[-1]["mac"], "ssh failed: Connection timed out")
        self.assertEqual(self.state.position(self.graph, "board"), "bom")

    def test_a_pull_request_closed_without_a_merge_needs_a_person(self):
        self.ask()
        self.pr["state"] = "CLOSED"
        code, _, err = self.step()
        self.assertEqual(code, 1)
        self.assertIn("closed without a merge", err)
        self.assertEqual((self.tidied, self.state.attempts()), ([], []))

    def test_the_upkeep_fetches_lfs_files_that_are_pointers_and_refuses_when_they_stay(self):
        calls = []
        listings = iter(["abc * board/models/a.step\n", "abc * board/models/a.step\n"])
        with mock.patch.object(board_graph, "git_text", lambda root, *args: next(listings)), \
                mock.patch.object(board_graph, "run_git", lambda root, *args, **k: calls.append(args)):
            self.assertEqual(board_graph.lfs_content(pathlib.Path(".")), ([], ""))  # files already: no pull
        self.assertEqual(calls, [])
        listings = iter(["abc - board/models/a.step\ndef - board/models/b.step\n",
                         "abc * board/models/a.step\ndef * board/models/b.step\n"])
        with mock.patch.object(board_graph, "git_text", lambda root, *args: next(listings)), \
                mock.patch.object(board_graph, "run_git", lambda root, *args, **k: calls.append(args)):
            self.assertEqual(board_graph.lfs_content(pathlib.Path(".")),
                             ([], "git lfs pull fetched 2 file(s) that were pointers here\n"))
        self.assertEqual(calls, [("lfs", "pull")])
        listings = iter(["abc - board/models/a.step\n", "abc - board/models/a.step\n"])
        with mock.patch.object(board_graph, "git_text", lambda root, *args: next(listings)), \
                mock.patch.object(board_graph, "run_git", lambda root, *args, **k: None):
            problems, said = board_graph.lfs_content(pathlib.Path("."))
        self.assertEqual(problems, ["1 git-LFS file(s) are still pointers after git lfs pull: board/models/a.step"])

        def no_lfs(root, *args):
            raise ValueError("git lfs: not a git command")
        with mock.patch.object(board_graph, "git_text", no_lfs):
            self.assertEqual(board_graph.lfs_content(pathlib.Path(".")),
                             ([], "git lfs did not answer (git lfs: not a git command); LFS files were not checked\n"))

    def test_a_checkout_behind_main_is_not_current(self):
        self.merged()
        git_text(self.work.pc, "fetch", "-q", "origin")
        side = self.work.base / "side"
        git_text(self.work.pc, "worktree", "add", "-q", "-b", "side", str(side), "HEAD")
        problems = checkout_problems(self.graph, side)
        self.assertEqual(len(problems), 2)
        self.assertRegex(problems[0], r"^main here \(.+\) is 2 commit\(s\) behind GitHub's main")
        self.assertRegex(problems[1], r"^this checkout \(.+side\) is 2 commit\(s\) behind GitHub's main and next "
                                      r"reads its files")
        git_text(self.work.pc, "merge", "-q", "--ff-only", "origin/main")
        self.assertEqual(len(checkout_problems(self.graph, self.work.pc)), 0)

    def test_main_here_with_commits_github_lacks_is_not_current_even_mid_rebase(self):
        self.work.commit({"docs/local.md": "only here\n"}, "a commit only here", where=self.work.pc)
        [problem] = checkout_problems(self.graph, self.work.pc)
        self.assertRegex(problem, r"^main here \(.+pc\) has 1 commit\(s\) GitHub's main does not: push them first$")
        self.merged()
        git_text(self.work.pc, "fetch", "-q", "origin")
        # A rebase holds main on no branch; the branch itself still says where main here stands.
        git_text(self.work.pc, "checkout", "-q", "--detach")
        problems = checkout_problems(self.graph, self.work.pc)
        self.assertEqual(problems[0], "main here (no checkout is on it) has diverged from GitHub's main: "
                                      "2 commit(s) behind and 1 ahead")
        self.assertRegex(problems[1], r"^this checkout \(.+pc\) is 2 commit\(s\) behind")
        view = workspace(self.graph, self.state, self.work.pc, github=no_network, mac_status=lambda: {
            "branch": "main", "behind": 0, "ahead": 0, "head": "abc1234", "tidy_pending": [], "notes": []},
                         list_pull_requests=lambda root: [])
        text = "\n".join(board_graph.render_workspace(view))
        self.assertIn("(no checkout is on it): 2 commit(s) behind and 1 ahead of GitHub's main", text)
        self.assertIn("Here, this checkout (no branch) at", text)

    def test_hash_at_matches_hash_path_of_the_checked_out_files(self):
        self.merged()
        git_text(self.work.pc, "pull", "-q", "--ff-only")
        for path in ("board/reports/pr84", "board/demo.kicad_sch", "board"):
            self.assertEqual(hash_at(self.work.pc, "HEAD", path), hash_path(self.work.pc / path), path)
        self.assertIsNone(hash_at(self.work.pc, "HEAD", "board/missing"))


class BranchProgressTests(unittest.TestCase):
    """Main's page shows where an open pull request's branch stands by the records only that branch holds."""

    def setUp(self):
        self.graph = Graph(FIXTURE_GRAPH)
        self.track = next(track for track in self.graph.data["tracks"] if track["ordered"])
        self.cards = self.graph.track_nodes(self.track["id"])

    def row(self, node, outcome, target=None, at="2026-11-17T04:00:00+00:00"):
        return {"at": at, "track": self.track["id"], "node": node, "outcome": outcome, "target": target,
                "actor": "agent:test", "note": "n"}

    def test_passes_move_the_branch_and_a_stay_outcome_keeps_it(self):
        first, second, third = self.cards[:3]
        [entry] = board_graph.branch_progress(self.graph, [
            self.row(first, "pass", second), self.row(second, "more_work"), self.row(second, "pass", third),
            self.row(third, "needs_owner", at="2026-11-17T05:00:00+00:00")], {third: "A label only the branch has"})
        self.assertEqual((entry["track"], entry["position"], entry["passed"]), (self.track["id"], third, [first, second]))
        self.assertEqual(entry["position_label"], "A label only the branch has")
        self.assertEqual((entry["last"]["node"], entry["last"]["outcome"]), (third, "needs_owner"))

    def test_a_return_takes_back_the_cards_passed_after_its_target(self):
        first, second, third = self.cards[:3]
        [entry] = board_graph.branch_progress(self.graph, [
            self.row(first, "pass", second), self.row(second, "pass", third), self.row(third, "returned", second)])
        self.assertEqual((entry["position"], entry["passed"]), (second, [first]))

    def test_no_records_on_the_branch_is_nothing_to_show(self):
        self.assertEqual(board_graph.branch_progress(self.graph, []), [])

    def test_records_are_read_from_the_branch_as_fetched_and_only_those_main_lacks(self):
        def git(root, *args):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=root, check=True,
                           capture_output=True)

        with tempfile.TemporaryDirectory() as directory:
            origin, here = pathlib.Path(directory) / "origin", pathlib.Path(directory) / "here"
            origin.mkdir()
            git(origin, "init", "-q", "-b", "main")
            records = origin / "tools" / "board_graph_records"
            records.mkdir(parents=True)
            (origin / "tools" / "graph.json").write_text(FIXTURE_GRAPH.read_text(encoding="utf-8"), encoding="utf-8")
            first, second = self.cards[:2]
            (records / "a.json").write_text(json.dumps(self.row(first, "more_work")), encoding="utf-8")
            git(origin, "add", "-A")
            git(origin, "commit", "-q", "-m", "main")
            git(origin, "checkout", "-q", "-b", "work")
            (records / "b.json").write_text(json.dumps(self.row(first, "pass", second)), encoding="utf-8")
            data = json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))
            next(node for node in data["nodes"] if node["id"] == second)["label"] = "Renamed on the branch"
            (origin / "tools" / "graph.json").write_text(json.dumps(data), encoding="utf-8")
            git(origin, "add", "-A")
            git(origin, "commit", "-q", "-m", "work")
            git(origin, "checkout", "-q", "main")
            git(pathlib.Path(directory), "clone", "-q", str(origin), str(here))
            graph = Graph(here / "tools" / "graph.json")
            state = mock.Mock(records=here / "tools" / "board_graph_records")
            rows, labels = board_graph.records_on_branch(graph, state, here, "work")
            self.assertEqual([(row["node"], row["outcome"]) for row in rows], [(first, "pass")])
            self.assertEqual(labels[second], "Renamed on the branch")
            self.assertEqual(board_graph.records_on_branch(graph, state, here, "no-such-branch"), ([], {}))
            tied = board_graph.tied_pull_requests([], [{"number": 7, "headRefName": "work"}],
                                                  lambda branch: board_graph.branch_progress(
                                                      graph, *board_graph.records_on_branch(graph, state, here, branch)))
            self.assertEqual(tied[0]["node"], None)
            self.assertEqual((tied[0]["on_branch"][0]["position"], tied[0]["on_branch"][0]["position_label"]),
                             (second, "Renamed on the branch"))
            view = {"at": "x", "fetch_error": None, "here": {"checkouts": []}, "mac": {"reached": True, "this_machine": True},
                    "merge_questions": [], "pull_requests": {"open": tied, "error": None}, "last_upkeep": None}
            self.assertIn("on its branch, not merged", "\n".join(
                board_graph.render_workspace(dict(view, pull_requests={"open": [dict(tied[0], checks="no checks")],
                                                                      "error": None}))))


class WorkspaceTests(unittest.TestCase):
    """where's workspace block: checkouts against GitHub's main, tidy left to do, and the merge a position waits on."""

    def setUp(self):
        self.graph = Graph(FIXTURE_GRAPH)
        self.directory = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.directory.name)
        self.work = GitWork(base)
        self.state = State(base / "state.sqlite3", base / "records")

    def tearDown(self):
        self.work.stop()
        self.directory.cleanup()

    def unreachable(self):
        raise mac.MacError("ssh failed: no route to host")

    def test_where_reports_checkouts_tidy_pending_the_mac_and_the_pull_request(self):
        # A branch pushed under its own name and merged on GitHub, its worktree still here: tidy has work left.
        done = self.work.base / "done"
        git_text(self.work.pc, "worktree", "add", "-q", "-b", "done", str(done))
        self.work.commit({"docs/done.md": "done\n"}, "done", where=done)
        git_text(done, "push", "-q", "-u", "origin", "done")
        git_text(self.work.seed, "fetch", "-q", "origin")
        git_text(self.work.seed, "merge", "-q", "--no-ff", "origin/done", "-m", "Merge done")
        git_text(self.work.seed, "push", "-q", "origin", "main")
        for node in ("inventory", "decisions", "constraints"):
            self.state.append(self.graph, "board", node, "pass", self.graph.nodes[node]["outcomes"]["pass"],
                              "owner" if node == "decisions" else "agent:test", "seeded")
        head = self.work.head
        pr = {"state": "OPEN", "isDraft": True, "mergeCommit": None, "headRefOid": head,
              "statusCheckRollup": [{"name": "validate", "status": "IN_PROGRESS", "conclusion": ""}]}
        record(self.graph, self.state, "schematic", "needs_owner", "agent:test", "merge it?", (),
               root=self.work.pc, pull_request=84, then="pass", github=lambda number: pr)
        pr["headRefOid"] = "f" * 40
        view = workspace(self.graph, self.state, self.work.pc, github=lambda number: pr,
                         mac_status=self.unreachable, list_pull_requests=lambda root: [])
        [main] = view["here"]["checkouts"]
        self.assertEqual((main["branch"], main["this"], main["behind"], main["ahead"], main["dirty_owned"]),
                         ("main", True, 2, 0, []))
        [pending] = view["here"]["tidy_pending"]
        self.assertEqual(pending["branch"], "done")
        self.assertRegex(pending["kept"], r"it was used \d+ min ago")  # committed in just now: tidy keeps it for an hour
        self.assertEqual(view["mac"], {"reached": False, "error": "ssh failed: no route to host"})
        [question] = view["merge_questions"]
        self.assertEqual((question["pull_request"], question["state"], question["draft"], question["checks"],
                          question["head_moved"]), (84, "open", True, "running (validate)", True))
        text = render(dict(where(self.graph, self.state), workspace=view))
        for line in ("  Here, main at", "2 commit(s) behind GitHub's main",
                     "  Here, tidy pending: done is merged and still holds",
                     "  Mac: not reached (ssh failed: no route to host)",
                     "  Pull request 84 (schematic waits on its merge, then pass): open, draft; checks running "
                     "(validate); its head moved since the question"):
            self.assertIn(line, text)
        json.dumps(view)
        self.assertNotIn("as of", text)

        # The full read is kept in the database under this machine's name, and the quick read carries it, saying
        # from when. It reads this checkout now with no fetch (GitHub's main has moved on since: still 2 behind,
        # not 3), and it asks neither the Mac nor GitHub (both stand-ins are gone; the real ones refuse in every
        # test).
        kept = self.state.last_full_read(board_graph.MACHINE)
        self.assertEqual((kept["at"], kept["read_on"]), (view["at"], board_graph.MACHINE))
        self.work.push_to_main({"docs/later.md": "later\n"}, "a commit after the full read")
        write(self.work.pc, {"board/demo.kicad_pro": "edited by hand\n"})
        quick = board_graph.quick_workspace(self.graph, self.state, self.work.pc)
        [main] = quick["here"]["checkouts"]
        self.assertEqual((main["branch"], main["this"], main["behind"], main["ahead"], main["dirty_owned"]),
                         ("main", True, 2, 0, ["board/demo.kicad_pro"]))
        self.assertNotIn("carried", main)
        self.assertEqual(quick["quick"]["carried_from"], view["at"])
        text = "\n".join(board_graph.render_workspace(quick)) + "\n"
        since = f" (as of {board_graph.stamp(view['at'])})"
        self.assertIn("  Here, main at", text)
        self.assertIn("2 commit(s) behind GitHub's main; uncommitted changes to owned files: board/demo.kicad_pro\n",
                      text)
        # The worktrees and branches here are read now, by git alone, so their lines carry nothing.
        [pending_line] = [line for line in text.splitlines() if line.startswith("  Here, tidy pending: done ")]
        self.assertNotIn("as of", pending_line)
        self.assertIn("its head moved since the question (" + head[:8] + ", now ffffffff)" + since + "\n", text)
        self.assertIn("  Carried from the last full read, under a minute ago: each line ending \"as of\" that time",
                      text)
        mac_line = ("  Mac: this machine, above\n" if sys.platform == "darwin" else
                    f"  Mac: not reached (ssh failed: no route to host){since}\n")
        self.assertIn(mac_line, text)
        # Tidy's rule: every branch whose tip is in origin/main counts, pushed under its own name or not.
        git_text(self.work.pc, "branch", "-q", "--track", "fresh", "origin/main")
        self.assertEqual(tidy.merged_branches(cwd=self.work.pc), {"done", "fresh"})
        # Once the worktree is gone, the next quick read no longer names it: nothing about it is carried from the
        # full read. A branch with a commit main lacks is named instead, with the worktree that holds it.
        git_text(self.work.pc, "worktree", "remove", "--force", str(done))
        open_work = self.work.base / "open"
        git_text(self.work.pc, "worktree", "add", "-q", "-b", "open", str(open_work))
        self.work.commit({"docs/open.md": "open\n"}, "open work", where=open_work)
        text = "\n".join(board_graph.render_workspace(board_graph.quick_workspace(self.graph, self.state, self.work.pc)))
        self.assertNotIn("Here, tidy pending: done ", text)
        self.assertRegex(text, r"  Here, not on main yet: open, 1 commit\(s\), last \S+ open work, only here, in .+open")

    def test_the_full_read_lists_open_pull_requests_and_each_machine_keeps_its_own(self):
        self.state.append(self.graph, "board", "inventory", "pass", "decisions", "agent:test",
                          "seeded; pull request 90 is unrelated here")
        view = workspace(self.graph, self.state, self.work.pc, github=no_network, mac_status=self.unreachable,
                         list_pull_requests=lambda root: PULLS)
        found = {pr["number"]: pr for pr in view["pull_requests"]["open"]}
        self.assertEqual(list(found), [85, 90])
        self.assertEqual((found[85]["node"], found[85]["draft"], found[85]["checks"]), (None, True, "failing (validate)"))
        self.assertEqual((found[90]["node"], found[90]["named_in"], found[90]["checks"]), ("inventory", "note", "no checks"))
        text = "\n".join(board_graph.render_workspace(view))
        self.assertIn("  Open pull request 85 (y, draft): checks failing (validate); no record names it\n", text + "\n")
        self.assertIn("  Open pull request 90 (x): checks no checks; inventory names it", text)
        # Another machine's full read is kept apart: this machine's quick read carries its own, the view the newest.
        later = dict(view, at="2099-01-01T00:00:00+00:00", read_on="other", here={"checkouts": [], "tidy_pending": []})
        self.state.keep_full_read("other", later)
        self.assertEqual(self.state.last_full_read(board_graph.MACHINE)["at"], view["at"])
        self.assertEqual(self.state.last_full_read("other")["read_on"], "other")
        self.assertEqual(self.state.last_full_read()["at"], "2099-01-01T00:00:00+00:00")
        quick = board_graph.quick_workspace(self.graph, self.state, self.work.pc)
        self.assertEqual(quick["quick"]["carried_from"], view["at"])
        self.assertEqual([pr["number"] for pr in quick["pull_requests"]["open"]], [85, 90])
        text = "\n".join(board_graph.render_workspace(quick))
        self.assertIn(f"  Open pull request 90 (x): checks no checks; inventory names it (as of {board_graph.stamp(view['at'])})",
                      text)
        failed = workspace(self.graph, self.state, self.work.pc, github=no_network, mac_status=self.unreachable,
                           list_pull_requests=lambda root: (_ for _ in ()).throw(ValueError("gh pr list: offline")))
        self.assertEqual(failed["pull_requests"], {"open": [], "error": "gh pr list: offline"})
        self.assertIn("  Open pull requests: GitHub did not say (gh pr list: offline)",
                      "\n".join(board_graph.render_workspace(failed)))
        self.assertIsNone(State(self.work.base / "fresh.sqlite3", self.work.base / "no-records").last_full_read())

    def test_a_quick_read_in_a_worktree_carries_main_and_counts_a_branch_that_tracks_nothing(self):
        side = self.work.base / "side"
        git_text(self.work.pc, "worktree", "add", "-q", "-b", "side", str(side), "HEAD")
        self.work.commit({"docs/side.md": "side\n"}, "side work", where=side)
        quick = board_graph.quick_workspace(self.graph, self.state, side)
        [this] = quick["here"]["checkouts"]
        self.assertEqual((this["branch"], this["this"], this["behind"], this["ahead"]), ("side", True, 0, 1))
        text = "\n".join(board_graph.render_workspace(quick))
        # With no full read kept, main's standing is not read; the branches here are, by git alone.
        for line in ("  No full read is kept here: what only a full read reaches says \"not read\"",
                     "  Here, main: not read", "  Here, not on main yet: side, 1 commit(s), last "):
            self.assertIn(line, text)
        self.assertNotIn("tidy pending: not read", text)
        self.assertNotIn("as of", text)
        if sys.platform != "darwin":
            self.assertIn("  Mac: not read", text)
        view = workspace(self.graph, self.state, self.work.pc, github=no_network, mac_status=self.unreachable,
                         list_pull_requests=lambda root: [])
        quick = board_graph.quick_workspace(self.graph, self.state, side)
        main, this = quick["here"]["checkouts"]
        self.assertEqual((main["branch"], main["this"], main["carried"]), ("main", False, True))
        self.assertEqual((this["branch"], this["ahead"]), ("side", 1))
        text = "\n".join(board_graph.render_workspace(quick)) + "\n"
        self.assertIn(f"level with GitHub's main (as of {board_graph.stamp(view['at'])})", text)
        self.assertIn("1 commit(s) ahead of GitHub's main\n", text)

    def test_the_mac_as_tidy_sees_it(self):
        status = {"branch": "main", "behind": 2, "ahead": 0, "head": "abc1234",
                  "tidy_pending": [{"path": "/Users/builder/demo-x", "branch": "x"}], "notes": ["Mac: kept y"]}
        view = workspace(self.graph, self.state, self.work.pc, github=no_network, mac_status=lambda: status,
                         list_pull_requests=lambda root: [])
        self.assertTrue(view["mac"]["reached"])
        text = "\n".join(board_graph.render_workspace(view))
        self.assertIn("  Mac, main at abc1234: 2 commit(s) behind GitHub's main", text)
        self.assertIn("  Mac, tidy pending: x is merged and still holds /Users/builder/demo-x", text)
        self.assertIn("  Mac: kept y", text)
        status["tidy_pending"] = [{"path": "/Users/builder/demo-loose", "branch": tidy.NO_BRANCH},
                                  {"path": "/private/tmp/demo-copy", "branch": None}]
        text = "\n".join(board_graph.render_workspace(dict(view, mac=dict(status, reached=True))))
        self.assertIn("  Mac, tidy pending: a worktree on no branch has nothing main lacks and still holds "
                      "/Users/builder/demo-loose", text)
        self.assertIn("  Mac, tidy pending: /private/tmp/demo-copy is a disposable copy nothing has used for an hour", text)


class OneDatabaseTests(unittest.TestCase):
    """One database, on the Mac: place() names it from the PC and on the Mac; from the PC a Remote reaches it over
    SSH (a stand-in here, running serve on a database of its own), one round trip per write and reads from the copy;
    the Mac imports its records and refreshes the page on each write; a Mac that does not answer refuses with
    nothing written anywhere."""

    MAC_WORDS = "builder@kicad-host:~/demo-board/logs/board-graph/state.sqlite3"

    def setUp(self):
        self.graph = Graph(FIXTURE_GRAPH)
        self.directory = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.directory.name)
        self.mac = self.base / "mac"  # the Mac's main checkout: the database, the records it imports, the page's folder
        self.mac_state = self.mac / "logs/board-graph/state.sqlite3"
        self.mac_records = self.mac / "tools/board_graph_records"
        self.mac_view = self.mac / "tools/board_graph_view"
        self.pc_records = self.base / "pc/tools/board_graph_records"  # the PC checkout's record files
        for folder in (self.mac_records, self.mac_view, self.pc_records):
            folder.mkdir(parents=True)
        self.calls = []
        self.landed = []  # what record would commit and push: (root, paths, message); no test touches git here

        def fake_land(root, paths, message):
            self.landed.append((root, list(paths), message))
            return "committed abc1234 on main and pushed to origin"

        for patcher in (mock.patch.object(board_graph, "remote_call", self.fake_ssh), mock.patch.object(mac, "LOCAL", False),
                        mock.patch.object(board_graph, "land", fake_land), mock.patch.dict(os.environ),
                        mock.patch.object(board_graph, "ACTIONS", {}),  # inventory's script is not connected here
                        mock.patch.object(board_graph, "bring_current", lambda *a, **k: True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop(board_graph.STATE_ENV, None)
        self.argv = ["--records", str(self.pc_records)]

    def tearDown(self):
        self.directory.cleanup()

    def fake_ssh(self, request_text):
        """The SSH round trip, without a Mac: serve runs in another process on the Mac's stand-in database."""
        self.calls.append(json.loads(request_text)["op"])
        done = subprocess.run([sys.executable, str(SCRIPTS / "board_graph.py"), "--state", str(self.mac_state),
                               "--records", str(self.mac_records), "--view", str(self.mac_view), "serve"],
                              input=request_text.encode("utf-8"), capture_output=True, **NO_WINDOW_KWARGS)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        return json.loads(done.stdout)

    def on_mac(self):
        """The Mac's database, opened as the Mac opens it."""
        return State(self.mac_state, self.mac_records)

    def view(self):
        return json.loads((self.mac_view / "view.json").read_text(encoding="utf-8"))

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = board_graph.main([*self.argv, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_place_names_the_macs_database_from_the_pc_and_on_the_mac_and_another_only_when_named(self):
        here = board_graph.place(root=self.base, environ={})
        self.assertEqual(here, {"path": self.MAC_WORDS, "how": "ssh", "imports": None, "view": None})
        self.assertEqual(board_graph.place(view=self.base / "v", root=self.base, environ={})["view"], self.base / "v")
        self.assertEqual(board_graph.database_line(here),
                         f"State: {self.MAC_WORDS}, the one database, on the Mac over SSH")
        with mock.patch.object(mac, "LOCAL", True), mock.patch.object(board_graph, "mac_checkout", lambda: self.mac):
            there = board_graph.place(root=self.base / "worktree", environ={})
            self.assertEqual(there, {"path": self.mac_state, "how": "mac", "imports": self.mac_records,
                                     "view": self.mac_view})
            self.assertEqual(board_graph.place(records=self.pc_records, root=self.base, environ={})["imports"],
                             self.pc_records)
        self.assertEqual(board_graph.database_line(there), f"State: {self.mac_state}, the one database, on this Mac")
        odd = self.base / "odd.sqlite3"
        named = board_graph.place(root=self.base, environ={board_graph.STATE_ENV: str(odd)})
        self.assertEqual(named, {"path": odd, "how": board_graph.STATE_ENV, "imports": self.base / "tools/board_graph_records",
                                 "view": None, "named": board_graph.STATE_ENV})
        given = board_graph.place(self.base / "given.sqlite3", view=self.base / "v", root=self.base,
                                  environ={board_graph.STATE_ENV: str(odd)})
        self.assertEqual((given["path"], given["how"], given["view"]), (self.base / "given.sqlite3", "--state", self.base / "v"))
        self.assertEqual(board_graph.database_line(named), f"State: {odd}, named by {board_graph.STATE_ENV}")
        self.assertEqual(board_graph.database_line(given), f"State: {self.base / 'given.sqlite3'}, named by --state")

    def test_origin_command_preserves_default_remote_mac_and_explicit_scratch_selection(self):
        remote_place = board_graph.place(root=self.base, environ={})
        remote = board_graph.open_state(remote_place, self.pc_records)
        self.assertNotIn("--state", board_graph.origin_command(self.graph, remote, self.base))
        with mock.patch.object(mac, "LOCAL", True), mock.patch.object(board_graph, "mac_checkout", lambda: self.mac):
            live_place = board_graph.place(root=self.base, environ={})
            live = board_graph.open_state(live_place, self.pc_records)
            self.assertNotIn("--state", board_graph.origin_command(self.graph, live, self.base))
        scratch = self.base / "scratch.sqlite3"
        scratch_place = board_graph.place(scratch, self.pc_records, root=self.base, environ={})
        selected = board_graph.open_state(scratch_place, self.pc_records)
        command = board_graph.origin_command(self.graph, selected, self.base)
        self.assertIn("--state", command)
        self.assertIn(str(scratch), command)

    def test_serve_view_keeps_explicit_scratch_selection_but_omits_default_mac_state(self):
        state = State(self.mac_state, self.mac_records)
        attempt, _, _ = state.open_work(self.graph, "inventory", "agent:test", "fixture", {}, str(self.base), launch=True)
        record(self.graph, state, "inventory", "more_work", "agent:test", "saved", (), root=self.base,
               attempt_id=attempt["run_id"])
        state.finish_launch(attempt["run_id"], "stopped", True)
        for how, expected in (("--state", True), ("mac", False)):
            location = {"path": self.mac_state, "how": how, "imports": self.mac_records, "view": self.mac_view}
            answer = board_graph.serve({"op": "keep_tokens", "args": {"run_id": attempt["run_id"], "tokens": 1}},
                                       location, self.mac_records)
            self.assertNotIn("error", answer)
            launch = self.view()["where"]["tracks"][0]["launches"][0]
            self.assertEqual("--state" in launch["ack_command"], expected)
            if expected:
                self.assertIn(str(self.mac_state), launch["ack_command"])

    def test_reconnect_recovers_the_exact_export_after_a_committed_reply_was_lost(self):
        def lose_reply(request):
            answer = self.fake_ssh(request)
            if json.loads(request)["op"].endswith("append") and "error" not in answer:
                raise mac.MacError("lost after commit")
            return answer
        with mock.patch.object(board_graph, "remote_call", lose_reply):
            code, _, err = self.cli("record", "inventory", "more_work", "--by", "agent:pc", "--note", "observed")
        self.assertEqual(code, 1)
        self.assertIn("acknowledgment", err)
        self.assertEqual(len(self.on_mac().transitions()), 1)
        self.assertEqual(list(self.pc_records.glob("*.json")), [])
        self.cli("where")
        row, = self.on_mac().transitions()
        self.assertEqual(json.loads((self.pc_records / row["record_name"]).read_bytes()), json.loads(board_graph.record_text(row)))

    def test_from_the_pc_every_command_reaches_the_macs_database_one_round_trip_per_write(self):
        # where: one round trip, the copy; it says which database it read.
        code, out, err = self.cli("where")
        self.assertEqual((code, err, self.calls), (0, "", ["read"]))
        self.assertIn(f"State: {self.MAC_WORDS}, the one database, on the Mac over SSH", out)
        self.assertIn("Board: before Inventory (auto), the track's start", out)
        self.assertFalse(self.mac_state.with_name("workspace.json").exists())
        # next: the attempt opens on the Mac, under its lock, and the Mac's page shows it at once.
        self.calls.clear()
        code, out, err = self.cli("next", "--by", "agent:pc", "--note", "reading")
        wire_prefix = "approval_" if Graph().data.get("approval_mode") == "enforce" else ""
        self.assertEqual((code, err, self.calls), (2, "", ["read", wire_prefix + "open_work"]))
        self.assertIn("Inventory is on: working since", out)
        self.assertNotIn("--state", next(line for line in out.splitlines() if line.startswith("  Close it:")))
        [held] = self.on_mac().attempts(open_only=True)
        self.assertEqual((held["actor"], held["note"], held["checkout"]), ("agent:pc", "reading", str(board_graph.ROOT.resolve())))
        self.assertEqual(self.view()["state"]["nodes"]["inventory"]["attempt"]["by"], "agent:pc")
        # Another session's next finds the card on, from the copy it read: no write is asked for.
        self.calls.clear()
        code, out, err = self.cli("next", "--by", "agent:two")
        self.assertEqual((code, err, self.calls), (2, "", ["read"]))
        self.assertIn("Inventory is on: working since", out)
        self.assertIn("by agent:pc (under a minute ago): reading", out)
        self.assertEqual(len(self.on_mac().attempts(open_only=True)), 1)
        # record: the Mac writes the row and closes the attempt; this checkout writes the record file, named alike.
        self.calls.clear()
        code, out, err = self.cli("record", "inventory", "pass", "--attempt", held["run_id"], "--by", "agent:pc", "--note", "read cleanly",
                                  "--evidence", "tools/board_graph.json", "--tokens", "1234")
        self.assertEqual((code, err, self.calls), (0, "", ["read", wire_prefix + "append"]))
        # A manual thread's tokens go on the attempt its record closes, and where says them on the transition.
        self.assertEqual([attempt["tokens"] for attempt in self.on_mac().attempts("inventory")], [1234])
        self.assertIn("inventory pass -> decisions (agent:pc): read cleanly [1,234 tokens]", self.cli("where")[1])
        code, _, err = self.cli("record", "decisions", "more_work", "--by", "owner", "--note", "no thread", "--tokens", "5")
        self.assertEqual((code, err.strip()), (1, "--tokens needs an open attempt on the card to keep them on"))
        self.assertIn("closed the open attempt by agent:pc", out)
        self.assertIn("board is between inventory and decisions", out)
        [record_file] = list(self.pc_records.glob("*.json"))
        there = self.on_mac()
        [row] = there.transitions()
        self.assertEqual((row["record_name"], row["actor"], row["target"]), (record_file.name, "agent:pc", "decisions"))
        self.assertEqual(json.loads(record_file.read_text(encoding="utf-8"))["evidence"], row["evidence"])
        self.assertEqual(list(self.mac_records.iterdir()), [])
        [held] = there.attempts("inventory")
        self.assertEqual((held["outcome"], bool(held["finished_at"])), ("pass", True))
        self.assertEqual(self.view()["where"]["tracks"][0]["position"], "decisions")
        self.assertIsNone(self.view()["state"]["nodes"]["inventory"]["attempt"])
        # where on the PC and on the Mac agree.
        code, from_pc, _ = self.cli("where", "--json")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(board_graph.main(["--state", str(self.mac_state), "--records", str(self.mac_records),
                                               "where", "--json"]), 0)
        pc, on_mac = json.loads(from_pc), json.loads(out.getvalue())
        self.assertEqual(pc["tracks"], on_mac["tracks"])
        self.assertEqual((pc["database"]["how"], on_mac["database"]["how"]), ("ssh", "--state"))
        # A record file that reached the Mac's main through git is imported when the Mac opens the database, and
        # a record this checkout already wrote is known by its name, not imported twice.
        shutil.copy(record_file, self.mac_records / record_file.name)
        State(self.base / "elsewhere.sqlite3", self.mac_records).append(
            self.graph, "board", "decisions", "pass", "constraints", "owner", "came through git", export=True)
        code, out, _ = self.cli("where")
        self.assertIn("Board: between Decisions and Constraints (agent)", out)
        self.assertEqual([row["actor"] for row in self.on_mac().transitions()], ["agent:pc", "owner"])
        self.assertEqual(self.view()["where"]["tracks"][0]["position"], "constraints")
        # A record file corrected on main corrects the database's note when the Mac opens it again; nothing else.
        copied = self.mac_records / record_file.name
        item = json.loads(copied.read_text(encoding="utf-8"))
        item["note"] = "the words, with their spaces back"
        copied.write_text(json.dumps(item, indent=1, sort_keys=True), encoding="utf-8")
        self.assertEqual([(row["note"], row["actor"]) for row in self.on_mac().transitions()
                          if row["record_name"] == record_file.name],
                         [("the words, with their spaces back", "agent:pc")])
        self.assertEqual(len(self.on_mac().transitions()), 2)
        # where --full keeps its read on the Mac, and the Mac's page carries it at once.
        this = {"path": "C:/pc", "branch": "main", "this": True, "head": "abc12345", "behind": 0, "ahead": 0,
                "dirty_owned": []}
        full = {"at": "2026-11-06T04:00:00+00:00", "fetch_error": None, "here": {"checkouts": [this], "tidy_pending": []},
                "mac": {"reached": False, "error": "offline"}, "merge_questions": [], "last_upkeep": None}
        self.calls.clear()
        with mock.patch.object(board_graph, "checkout_here", lambda *a, **k: dict(this)), \
                mock.patch.object(board_graph, "workspace", lambda graph, state, root=None: state.keep_full_read("pc", full) or full):
            code, out, err = self.cli("where", "--full")
        self.assertEqual((code, self.calls), (0, ["read", "keep_full_read"]))
        self.assertEqual(self.view()["workspace"]["quick"]["carried_from"], "2026-11-06T04:00:00+00:00")
        self.assertEqual(self.on_mac().last_full_read("pc")["at"], "2026-11-06T04:00:00+00:00")

    def test_a_record_carries_itself_to_its_branch_and_one_on_another_database_stays(self):
        code, out, err = self.cli("record", "inventory", "more_work", "--by", "agent:pc", "--note", "  the words,\n said ")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.endswith("committed abc1234 on main and pushed to origin\n"), out)
        (root, paths, message), = self.landed
        self.assertEqual(root, board_graph.ROOT)
        self.assertEqual([p.parent for p in paths], [self.pc_records])
        self.assertTrue(paths[0].exists() and paths[0].name.endswith("-inventory-more_work.json"), paths)
        self.assertEqual(message, "Record: Inventory, more_work by agent:pc: the words, said")
        # Another database, named with --state: a test's or an experiment's record stays where it is.
        self.landed.clear()
        other = self.base / "other.sqlite3"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = board_graph.main(["--state", str(other), "--records", str(self.base / "other-records"),
                                     "record", "inventory", "more_work", "--by", "agent:pc", "--note", "aside"])
        self.assertEqual((code, err.getvalue(), self.landed), (0, "", []))
        self.assertNotIn("committed", out.getvalue())

    def test_a_local_view_follows_the_copy_when_named_and_the_copy_is_read_only_and_goes_at_close(self):
        local = self.base / "pc-view"
        code, out, err = self.cli("--view", str(local), "next", "--by", "agent:pc")
        self.assertEqual((code, err), (2, ""))
        self.assertEqual(json.loads((local / "view.json").read_text(encoding="utf-8"))["state"]["nodes"]["inventory"]
                         ["attempt"]["by"], "agent:pc")
        remote = board_graph.Remote(self.MAC_WORDS, self.pc_records)
        self.assertTrue(remote.copy_path.exists())
        self.assertEqual([a["actor"] for a in remote.attempts(open_only=True)], ["agent:pc"])
        with contextlib.closing(remote.connect()) as db, self.assertRaises(sqlite3.OperationalError):
            db.execute("DELETE FROM attempts")
        with self.assertRaises(RuntimeError):
            with remote.writing():
                pass
        remote.close()
        self.assertFalse(remote.copy_path.exists())

    def test_the_mac_not_answering_refuses_in_one_line_and_writes_nothing(self):
        def down(request_text):
            raise mac.MacError("ssh failed: no route to host")

        with mock.patch.object(board_graph, "remote_call", down):
            code, out, err = self.cli("where")
            self.assertEqual((code, out), (1, ""))
            self.assertEqual(err, f"where: the graph's database is on the Mac ({self.MAC_WORDS}) and the Mac did not "
                                  "answer (ssh failed: no route to host); it was not read\n")
            code, out, err = self.cli("record", "inventory", "pass", "--by", "agent:pc", "--note", "n",
                                      "--evidence", "tools/board_graph.json")
            self.assertEqual((code, out), (1, ""))
            self.assertEqual(err, f"record: the graph's database is on the Mac ({self.MAC_WORDS}) and the Mac did not "
                                  "answer (ssh failed: no route to host); nothing was written, here or there\n")
            code, out, err = self.cli("next", "--by", "agent:pc")
            self.assertEqual((code, err.split(":")[0]), (1, "next"))
            code, out, err = self.cli("next")
            self.assertEqual((code, err.split(":")[0]), (1, "next"))
        self.assertEqual(list(self.pc_records.iterdir()), [])
        self.assertFalse(self.mac_state.exists())
        self.assertFalse(list(self.base.rglob("*.sqlite3")))
        # The Mac going away between the read and the write: the record file is not written either.
        answers = [self.fake_ssh]

        def flaky(request_text):
            if json.loads(request_text)["op"] != "read":
                raise mac.MacError("the Mac did not finish within 60 s")
            return answers[0](request_text)

        with mock.patch.object(board_graph, "remote_call", flaky):
            code, out, err = self.cli("record", "inventory", "pass", "--by", "agent:pc", "--note", "n",
                                      "--evidence", "tools/board_graph.json")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Mac write acknowledgment missing for append", err)
        self.assertEqual(list(self.pc_records.iterdir()), [])
        self.assertEqual(self.on_mac().transitions(), [])
        # serve itself refuses what it does not know, and an error there is the same error here.
        where_from = board_graph.place(self.mac_state, self.mac_records, None, self.base, {})
        answer = board_graph.serve({"op": "drop_tables"}, where_from, self.mac_records)
        self.assertEqual(answer["error"], {"type": "ValueError", "message": "serve does not know the operation 'drop_tables'"})
        answer = board_graph.serve({"op": "append", "graph": self.graph.data,
                                    "args": {"track": "board", "node": "decisions", "outcome": "pass", "target": "x",
                                             "actor": "a", "note": "n"}}, where_from, self.mac_records)
        self.assertEqual(answer["error"], {"type": "ValueError", "message": "unknown target x"})
        answer = board_graph.serve({"op": "read"}, where_from, self.mac_records)
        self.assertIsNone(answer["result"])
        copy = self.base / "copy.sqlite3"
        copy.write_bytes(base64.b64decode(answer["copy"]))
        self.assertEqual(State(copy, self.base / "no-records").transitions(), [])
        with mock.patch.object(board_graph, "remote_call", lambda text: {"error": {"type": "OperationalError",
                                                                                   "message": "database is locked"}}):
            code, out, err = self.cli("where")
        self.assertEqual(code, 1)
        self.assertIn(f"the state database {self.MAC_WORDS} did not answer (database is locked)", err)

    def test_where_refreshes_mac_from_live_keep_pace_status_over_ssh(self):
        old_full = {
            "at": "2026-11-10T01:00:00+00:00",
            "fetch_error": None,
            "here": {"checkouts": [], "tidy_pending": []},
            "mac": {
                "reached": True, "branch": "main", "head": "old1234", "behind": 7, "ahead": 0,
                "tidy_pending": [{"path": "/Users/builder/demo-old", "branch": "old-branch"}],
                "notes": ["Mac: kept stale"],
                "keep_pace": {"at": "2026-11-10T00:59:00Z", "by": "push", "result": "held",
                              "said": "held earlier"},
            },
            "merge_questions": [],
            "last_upkeep": None,
        }
        self.on_mac().keep_full_read(board_graph.MACHINE, old_full)
        kp_dir = self.mac / "logs" / "mac-keep-pace"
        kp_dir.mkdir(parents=True, exist_ok=True)
        kp_status = {
            "at": "2026-11-21T06:32:50Z", "by": "tidy", "result": "level",
            "said": "main is level with GitHub's main", "head": "983061a5",
            "branch": "main", "behind": 0, "ahead": 0, "pending": [],
        }
        (kp_dir / "status.json").write_text(json.dumps(kp_status), encoding="utf-8")
        this = {"path": "C:/pc", "branch": "main", "this": True, "head": "983061a5", "behind": 0, "ahead": 0,
                "dirty_owned": []}
        self.calls.clear()
        with (mock.patch.object(mac, "ONE_MACHINE", False),
              mock.patch.object(board_graph, "checkout_here", lambda *a, **k: dict(this)),
              mock.patch.object(board_graph, "branches_here", lambda *a, **k: {"tidy_pending": [], "not_on_main": []})):
            code, out, err = self.cli("where")
        self.assertEqual((code, err, self.calls), (0, "", ["read"]))
        self.assertIn("  Mac, main at 983061a5: level with GitHub's main (as of 2026-11-21T06:32:50Z, keep-pace)", out)
        self.assertIn("  Mac, keep-pace: last run 2026-11-21T06:32:50Z by tidy", out)
        for stale in ("old1234", "7 commit(s) behind", "tidy pending", "Mac: kept stale"):
            self.assertNotIn(stale, out)

        # A second board database with no full_read of its own picks up the same checkout's status.json.
        strip_state = self.mac / "logs" / "board-graph" / "strip" / "state.sqlite3"
        strip_records = self.base / "strip-records"
        strip_records.mkdir()
        State(strip_state, strip_records)
        def fake_strip_ssh(request_text):
            self.calls.append(json.loads(request_text)["op"])
            done = subprocess.run(
                [sys.executable, str(SCRIPTS / "board_graph.py"), "--state", str(strip_state),
                 "--records", str(strip_records), "serve"],
                input=request_text.encode("utf-8"), capture_output=True, **NO_WINDOW_KWARGS,
            )
            self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
            return json.loads(done.stdout)
        self.calls.clear()
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with (mock.patch.object(board_graph, "remote_call", fake_strip_ssh),
              mock.patch.object(mac, "ONE_MACHINE", False),
              mock.patch.object(board_graph, "checkout_here", lambda *a, **k: dict(this)),
              mock.patch.object(board_graph, "branches_here", lambda *a, **k: {"tidy_pending": [], "not_on_main": []}),
              contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf)):
            code = board_graph.main(["--records", str(strip_records), "where"])
        self.assertEqual((code, err_buf.getvalue(), self.calls), (0, "", ["read"]))
        self.assertIn("  Mac, main at 983061a5: level with GitHub's main (as of 2026-11-21T06:32:50Z, keep-pace)",
                      out_buf.getvalue())


# A session writing the state, for TwoWritersTests: it says it is ready, waits for the go, then does its job.
WRITER = """
import json, pathlib, sys, time
sys.path.insert(0, sys.argv[1])
from board_graph import Graph, State, now, record

job, base, me = sys.argv[2], pathlib.Path(sys.argv[3]), sys.argv[4]
if "slow" in sys.argv[5:]:  # a gap between each check and its write, as on a loaded machine
    for name in ("attempts", "position"):
        def slowed(self, *args, _read=getattr(State, name), **kwargs):
            found = _read(self, *args, **kwargs)
            time.sleep(0.3)
            return found
        setattr(State, name, slowed)
graph = Graph(pathlib.Path(sys.argv[1]) / "fixtures" / "board_graph.json")
(base / ("ready-" + me)).write_text("", encoding="utf-8")
while not (base / "go").exists():
    time.sleep(0.002)
state = State(base / "state.sqlite3", base / ("records-" + me), base / "imports")
said = {"imported": state.imported}
if job == "many":
    # At least the count asked for, and on until the other session's first write is seen, so the two overlap
    # however fast one machine writes.
    node, other = {"a": ("usage", "automation"), "b": ("automation", "usage")}[me]
    said["first"], deadline, i = time.time(), time.time() + 60, 0
    while i < int(sys.argv[5]) or (not state.transitions(node=other) and time.time() < deadline):
        state.append(graph, "framework", node, "more_work", node, "agent:" + me, "%s %d" % (me, i))
        run = state.start_attempt(node, "agent", {}, now(), actor="agent:" + me)
        state.finish_attempt(run, now(), "more_work", None, None)
        upkeep = state.start_upkeep(node, "write %d" % i, now())
        state.finish_upkeep(upkeep, now(), "current", "", None, [])
        i += 1
    said["last"], said["writes"] = time.time(), i
else:
    try:
        if job == "start":
            state.open_work(graph, "inventory", "agent:" + me, "", {}, str(base))
        else:
            record(graph, state, "inventory", "pass", "agent:" + me, "read", ["evidence.txt"], root=base)
        said["done"] = True
    except ValueError as error:
        said["refused"] = str(error)
print(json.dumps(said))
"""


class TwoWritersTests(unittest.TestCase):
    """Two sessions writing the one database at the same moment, as two processes: nothing is lost, nothing is
    imported twice, and a check that decides a write is made under the same lock as the write."""

    def setUp(self):
        self.graph = Graph(FIXTURE_GRAPH)
        self.directory = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.directory.name)
        (self.base / "writer.py").write_text(WRITER, encoding="utf-8")
        (self.base / "evidence.txt").write_text("read\n", encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def both(self, job, *args):
        """Run `job` in two processes that start at one go; what each said, by name."""
        children = {me: subprocess.Popen([sys.executable, str(self.base / "writer.py"), str(SCRIPTS), job,
                                          str(self.base), me, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         **NO_WINDOW_KWARGS) for me in ("a", "b")}
        deadline = time.monotonic() + 120
        while not all((self.base / f"ready-{me}").exists() for me in children):
            if time.monotonic() > deadline or any(child.poll() is not None for child in children.values()):
                break
            time.sleep(0.01)
        (self.base / "go").write_text("", encoding="utf-8")
        ended = {me: child.communicate(timeout=300) for me, child in children.items()}  # both, before any assert
        for me, child in children.items():
            self.assertEqual(child.returncode, 0, ended[me][1].decode("utf-8", "replace"))
        return {me: json.loads(out) for me, (out, err) in ended.items()}

    def state(self):
        return State(self.base / "state.sqlite3", self.base / "no-records")

    def test_two_writers_at_once_lose_nothing_and_import_each_record_once(self):
        seed = State(self.base / "seed.sqlite3", self.base / "imports")
        for second in range(20):
            seed.append(self.graph, "framework", "shared_evidence", "more_work", "shared_evidence", "agent:git",
                        f"record {second}", at=f"2026-11-06T00:00:{second:02d}+00:00", export=True)
        said = self.both("many", "40")
        a, b = said["a"], said["b"]
        self.assertLess(a["first"], b["last"])  # they wrote at the same time
        self.assertLess(b["first"], a["last"])
        self.assertEqual(a["imported"] + b["imported"], 20)
        state = self.state()
        rows = state.transitions()
        self.assertEqual(sorted(row["record_name"] for row in rows if row["record_name"]),
                         sorted(path.name for path in (self.base / "imports").glob("*.json")))
        for me, node in (("a", "usage"), ("b", "automation")):
            writes = said[me]["writes"]
            self.assertGreaterEqual(writes, 40)
            self.assertEqual([row["note"] for row in rows if row["actor"] == f"agent:{me}"],
                             [f"{me} {i}" for i in range(writes)])
            self.assertEqual([attempt["outcome"] for attempt in state.attempts(node)], ["more_work"] * writes)
        self.assertEqual([upkeep["outcome"] for upkeep in state.upkeeps()], ["current"] * (a["writes"] + b["writes"]))

    def test_two_sessions_starting_one_node_at_once_open_one_attempt(self):
        said = self.both("start", "slow")
        self.assertEqual(sorted("done" in item for item in said.values()), [False, True])
        self.assertIn("inventory is on: agent:", next(item["refused"] for item in said.values()
                                                        if "refused" in item))
        self.assertEqual(len(self.state().attempts("inventory", open_only=True)), 1)

    def test_two_sessions_recording_one_position_at_once_move_it_once(self):
        said = self.both("record", "slow")
        self.assertEqual(sorted("done" in item for item in said.values()), [False, True])
        refused = next(item["refused"] for item in said.values() if "refused" in item)
        # Refused under the lock; or before it, by the plain check, if one session stalled until the other was done.
        self.assertIn(refused, ("board moved to decisions while inventory was being recorded; nothing was written",
                                "board is at decisions, not inventory"))
        state = self.state()
        self.assertEqual([(row["node"], row["target"]) for row in state.transitions()], [("inventory", "decisions")])
        self.assertEqual(len(list(self.base.glob("records-*/*.json"))), 1)


class QuickReadTests(unittest.TestCase):
    """The parts of the quick read that need no repository: git's status words and the labels."""

    def test_status_reads_branch_lines_and_every_kind_of_change(self):
        said = ("# branch.oid 0123456789abcdef0123456789abcdef01234567\n# branch.head side\n"
                "# branch.upstream origin/main\n# branch.ab +1 -2\n"
                "1 .M N... 100644 100644 100644 aaa bbb board/demo.kicad_pro\n"
                "2 R. N... 100644 100644 100644 aaa bbb R100 board/new name.md\tboard/old.md\n"
                "u UU N... 100644 100644 100644 100644 aaa bbb ccc board/demo.kicad_sch\n"
                "? board/reports/new.json\n")
        with mock.patch.object(board_graph, "git_text", lambda *args, **kwargs: said):
            info, changed = board_graph.status(pathlib.Path("."), ["board"], branch=True)
            here = board_graph.checkout_here(Graph(), pathlib.Path("."))
        self.assertEqual(info["branch.ab"], "+1 -2")
        self.assertEqual(changed, ["board/demo.kicad_pro", "board/new name.md", "board/demo.kicad_sch",
                                   "board/reports/new.json"])
        # Tracking origin/main, the status's own counts are the counts: no second git call.
        self.assertEqual((here["branch"], here["head"], here["ahead"], here["behind"]), ("side", "01234567", 1, 2))

    def test_a_question_newer_than_the_full_read_says_github_was_not_asked(self):
        view = {"quick": {"at": "2026-11-06T02:00:00+00:00", "carried_from": None},
                "here": {"checkouts": [], "tidy_pending": None}, "last_upkeep": None,
                "merge_questions": [{"pull_request": 84, "node": "schematic", "then": "pass"}]}
        text = "\n".join(board_graph.render_workspace(view))
        self.assertIn("  Pull request 84 (schematic waits on its merge, then pass): GitHub not asked since the "
                      "question (`where --full` asks)", text)
        self.assertIn("  Mac: not read", text)
        self.assertEqual(board_graph.stamp("2026-11-06T01:34:31.943682+00:00"), "2026-11-06T01:34:31Z")


class KeepPaceTests(unittest.TestCase):
    """The Mac's last keep-pace run in where's workspace: who made it, when, and what it said."""

    READ_AT = "2026-11-06T03:00:00+00:00"

    def lines(self, keep_pace, **mac_view):
        view = {"at": self.READ_AT, "fetch_error": None, "here": {"checkouts": [], "tidy_pending": []},
                "mac": dict({"reached": True, "branch": "main", "behind": 0, "ahead": 0, "head": "abc1234",
                             "tidy_pending": [], "notes": [], "keep_pace": keep_pace}, **mac_view),
                "merge_questions": [], "last_upkeep": None}
        return [line for line in board_graph.render_workspace(view) if "keep-pace" in line]

    def test_the_last_run_against_the_time_of_the_read(self):
        status = {"at": "2026-11-06T02:59:00Z", "by": "push", "result": "forwarded",
                  "said": "fast-forwarded main by 3 commit(s), aaaaaaaa to bbbbbbbb; the page was told"}
        self.assertEqual(self.lines(status), [
            "  Mac, keep-pace: last run 2026-11-06T02:59:00Z by push (1 min before this read): forwarded, "
            "fast-forwarded main by 3 commit(s), aaaaaaaa to bbbbbbbb; the page was told"])

    def test_no_status_a_broken_one_and_a_read_from_before_the_job(self):
        self.assertEqual(self.lines(None), ["  Mac, keep-pace: no run recorded (no logs/mac-keep-pace/status.json "
                                            "there: has nothing been pushed to main since the Mac was set up?)"])
        self.assertEqual(self.lines({"error": "its status.json did not read (Expecting value)"}),
                         ["  Mac, keep-pace: its status.json did not read (Expecting value)"])
        view = {"reached": True, "branch": "main", "behind": 0, "ahead": 0, "head": "abc1234", "tidy_pending": [],
                "notes": []}
        self.assertEqual(board_graph.keep_pace_lines(view, self.READ_AT), [])


class PageLinesTests(unittest.TestCase):
    """The graph page, served from the Mac, in where's workspace: answering, stopped, not installed, not read."""

    def lines(self, **mac_view):
        view = {"at": "2026-11-06T03:00:00+00:00", "fetch_error": None, "here": {"checkouts": [], "tidy_pending": []},
                "mac": dict({"reached": True, "branch": "main", "behind": 0, "ahead": 0, "head": "abc1234",
                             "tidy_pending": [], "notes": []}, **mac_view), "merge_questions": [], "last_upkeep": None}
        return [line for line in board_graph.render_workspace(view) if "graph page" in line]

    def test_answering_stopped_not_installed_and_not_read(self):
        served = {"address": "http://kicad-host:8000", "job": "installed", "launchd": {"state": "running", "pid": 7},
                  "answers": "yes"}
        self.assertEqual(self.lines(page=served), ["  Mac, graph page: http://kicad-host:8000 answers; launchd job running, pid 7"])
        stopped = dict(served, launchd={"state": "not running", "pid": None}, answers="no (connection refused)")
        self.assertEqual(self.lines(page=stopped),
                         ["  Mac, graph page: http://kicad-host:8000 does not answer (no (connection refused)); launchd job "
                          "not running"])
        self.assertEqual(self.lines(page=dict(served, launchd=None, job="not installed")),
                         ["  Mac, graph page: http://kicad-host:8000 answers; launchd job not loaded; launchd's copy of its "
                          "plist: not installed (run `python3 scripts/board_graph_serve.py --install` there)"])
        self.assertEqual(self.lines(page=None), ["  Mac, graph page: no status (scripts/board_graph_serve.py --status "
                                                 "said nothing there: is the Mac's main current?)"])
        self.assertEqual(self.lines(page={"error": "its status line did not read (x)"}),
                         ["  Mac, graph page: its status line did not read (x)"])
        self.assertEqual(self.lines(), [])


class ChecksTests(unittest.TestCase):
    def test_checks_in_a_few_words(self):
        self.assertEqual(checks([]), "no checks")
        self.assertEqual(checks([{"name": "a", "status": "COMPLETED", "conclusion": "SUCCESS"},
                                 {"context": "b", "state": "SUCCESS"}]), "passing")
        self.assertEqual(checks([{"name": "a", "status": "COMPLETED", "conclusion": "FAILURE"},
                                 {"context": "b", "state": "PENDING"}]), "failing (a)")
        self.assertEqual(checks([{"name": "a", "status": "QUEUED", "conclusion": ""}]), "running (a)")


class ActionTests(unittest.TestCase):
    """The connected scripts run offline against the committed project and report the documented shape."""

    def test_every_graph_action_is_connected_or_waiting_for_kicad(self):
        named = {node["action"] for node in Graph().data["nodes"] if node["action"]}
        self.assertEqual(named - set(ACTIONS) - set(LATER) - set(COMMANDS), set())
        self.assertEqual(len(set(ACTIONS) | set(LATER) | set(COMMANDS)), len(ACTIONS) + len(LATER) + len(COMMANDS))

    def test_board_checks_findings_read_a_validate_summary(self):
        drc = "Board rules check, DRC (G2), zones refilled"
        clean = {"pass": True, "checks": [
            {"check": "Schematic rules check, ERC (G1)", "ok": True, "detail": "0 errors, 0 warnings"},
            {"check": drc, "ok": True, "detail": "0 copper violations", "open": 0},
            {"check": "Routing progress", "ok": True, "detail": "none open"}]}
        self.assertEqual(board_graph_actions.board_checks_findings(clean), ([], 0))
        unfinished = {"pass": True, "checks": [
            {"check": drc, "ok": True, "detail": "0 copper violations", "open": 72},
            {"check": "Electrical audit, R1 to R13 (G4)", "ok": True, "detail": "3 pads wait on routing, no other error"},
            {"check": "Routing progress", "ok": True, "detail": "72 open, not ready to order"}]}
        self.assertEqual(board_graph_actions.board_checks_findings(unfinished),
                         (["72 connections still open: the checks allowed an unfinished board (unconnected items, "
                           "pads waiting on routing, electrical findings by name), so none of them is a pass for "
                           "this card"], 72))
        failed = {"pass": False, "checks": [
            {"check": drc, "ok": False, "detail": "2 violations: {'clearance': 2}", "open": 0},
            {"check": "Route quality (G3)", "ok": True, "inherited": True,
             "detail": "inherited, the base fails the same way: 1 errors"},
            {"check": "Routing progress", "ok": True, "detail": "none open"}]}
        self.assertEqual(board_graph_actions.board_checks_findings(failed),
                         ([f"{drc}: FAIL, 2 violations: {{'clearance': 2}}",
                           "Route quality (G3): inherited, the base fails the same way: 1 errors"], 0))
        skipped = {"pass": True, "checks": [
            {"check": "Board checks", "ok": True, "detail": "this change touches nothing the board checks read"}]}
        self.assertEqual(board_graph_actions.board_checks_findings(skipped),
                         (["the run skipped the checks (an incremental run on a change that touches nothing they "
                           "read): the full run is the one on main"], None))
        no_count = {"pass": False, "checks": [
            {"check": drc, "ok": False, "detail": "DRC report has no usable unconnected-items list", "open": None}]}
        self.assertEqual(board_graph_actions.board_checks_findings(no_count)[0][-1],
                         "DRC gave no usable unconnected-items count")

    def test_board_checks_reads_the_row_the_validate_run_kept_on_the_graph(self):
        head = git_text(board_graph.ROOT, "rev-parse", "HEAD").strip()
        asked = []

        def lookup(row):
            def found(commit):
                asked.append(commit)
                return row
            return found

        checks = [{"check": "Board rules check, DRC (G2), zones refilled", "ok": True, "detail": "0 copper violations",
                   "open": 0},
                  {"check": "Routing progress", "ok": True, "detail": "none open"}]
        row = {"at": "2026-11-06T21:34:59+00:00", "commit": head, "run_id": "1", "event": "push", "passed": True,
               "open": 0, "checks": checks, "note": None}
        found = board_graph_actions.board_checks(board_graph.ROOT, lookup=lookup(row))
        self.assertEqual(asked, [head])
        self.assertEqual((found["outcome"], found["findings"]), ("pass", []))
        self.assertEqual((found["detail"]["commit"], found["detail"]["run"], found["detail"]["open"],
                          found["detail"]["pass"]),
                         (head, {"id": "1", "event": "push", "at": "2026-11-06T21:34:59+00:00"}, 0, True))
        self.assertEqual(found["detail"]["checks"], checks)
        # No row yet, and a run that left no checks: findings, never a pass.
        found = board_graph_actions.board_checks(board_graph.ROOT, lookup=lookup(None))
        self.assertEqual(found["findings"], [
            f"no board checks result on the graph for {head[:8]}: with a validate workflow, push it and run next "
            "when the run has finished; without one, run scripts/run_board_checks.py on the KiCad machine in a "
            "checkout of this commit"])
        found = board_graph_actions.board_checks(board_graph.ROOT, lookup=lookup(dict(
            row, run_id="2", passed=None, checks=[], note="no summary.json: the board checks did not finish")))
        self.assertEqual(found["findings"], [f"validate run 2 on {head[:8]} left no checks: no summary.json: the "
                                             "board checks did not finish"])


class CardScriptTests(unittest.TestCase):
    """The Schematic, Stock, Verification and Package cards' scripts, on a project made here under the names this
    checkout's board description gives. Each case shows the script failing as well as passing."""

    NOW = datetime.datetime(2026, 11, 17, tzinfo=datetime.timezone.utc)
    BOARD = ('(kicad_pcb (version 1)\n'
             ' (footprint "lib:R" (layer "F.Cu") (attr smd) (property "Reference" "R1")\n'
             '  (property "Supplier Part" "C1") (property "Manufacturer Part" "MPN1"))\n'
             ' (footprint "lib:R" (layer "F.Cu") (attr smd) (property "Reference" "R2")\n'
             '  (property "Supplier Part" "C1") (property "Manufacturer Part" "MPN1"))\n'
             ' (footprint "lib:J" (layer "F.Cu") (attr through_hole) (property "Reference" "J1")\n'
             '  (property "Supplier Part" "C9") (property "Manufacturer Part" "JACK"))\n'
             ' (footprint "lib:R" (layer "F.Cu") (attr smd dnp) (property "Reference" "R3")\n'
             '  (property "Supplier Part" "C3") (property "Manufacturer Part" "MPN3")))\n')

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.write(board_graph_actions.SCHEMATIC, "(kicad_sch (version 1))")
        self.write(board_graph_actions.BOARD, self.BOARD)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text if isinstance(text, str) else json.dumps(text), encoding="utf-8")
        return path

    def stamp(self, folder, *names):
        """Stamp the named reports in `folder` against the board and schematic as they are now."""
        import kicad_report_freshness
        board = self.root / board_graph_actions.BOARD
        stamps = {name: dict(kicad_report_freshness.source_hashes(board), board=board.name) for name in names}
        self.write(f"{folder}/provenance.json", stamps)

    # ERC

    def test_erc_passes_on_no_violations_and_names_each_one(self):
        clean = {"kicad_version": "10.0.8", "included_severities": ["error", "warning"], "sheets": [{"violations": []}]}
        found = board_graph_actions.erc(self.root, run=lambda root: clean)
        self.assertEqual((found["outcome"], found["findings"], found["detail"]["kicad_version"]), ("pass", [], "10.0.8"))
        dirty = dict(clean, sheets=[{"violations": [
            {"severity": "warning", "type": "pin_not_connected", "description": "Pin not connected",
             "items": [{"description": "Symbol U1 Pin 3"}]}]}])
        found = board_graph_actions.erc(self.root, run=lambda root: dirty)
        self.assertEqual((found["outcome"], found["findings"]),
                         ("needs_lead", ["warning: Pin not connected (pin_not_connected): Symbol U1 Pin 3"]))
        self.assertEqual(board_graph_actions.erc_findings(dict(clean, included_severities=["error"])),
                         ["the ERC report does not include both errors and warnings"])

    def test_erc_without_a_schematic_says_so_and_without_kicad_is_a_finding_not_a_pass(self):
        (self.root / board_graph_actions.SCHEMATIC).unlink()
        found = board_graph_actions.erc(self.root, run=lambda root: self.fail("ERC ran with no schematic"))
        self.assertEqual(found["outcome"], "needs_lead")
        self.assertIn("is not there yet", found["findings"][0])
        self.write(board_graph_actions.SCHEMATIC, "(kicad_sch (version 1))")
        with mock.patch.object(board_graph_actions, "kicad_cli", return_value=None):
            found = board_graph_actions.erc(self.root)
            self.assertEqual((found["outcome"], found["findings"]), ("needs_lead", [
                "KiCad's command-line tool is not installed here, so ERC was not run: start the card on a machine "
                "that has KiCad"]))
            with self.assertRaises(FileNotFoundError):
                board_graph_actions.erc_report(self.root)

    # Stock

    def listing(self, **changes):
        return dict({"part": "C1", "read_at": "2026-11-10T07:10:28Z", "listed_manufacturer": "Maker",
                     "listed_mpn": "MPN1", "class": "Basic", "stock": 500, "unit_price_usd": 0.01}, **changes)

    def supply(self, *rows, quantity=5):
        self.write(board_graph_actions.STOCK_LISTINGS, {"planning_quantity": quantity, "factory_parts": list(rows)})
        return board_graph_actions.supply_receipt(self.root, now=self.NOW)

    def test_stock_passes_when_every_factory_part_has_a_dated_listing(self):
        found = board_graph_actions.supply_receipt(self.root, now=self.NOW)
        self.assertEqual(found["findings"], [f"{board_graph_actions.STOCK_LISTINGS} is not there: the dated "
                                             "listings, one per factory part (1 on this board)"])
        found = self.supply(self.listing(), self.listing(part="C77"))
        self.assertEqual((found["outcome"], found["findings"]), ("pass", []))
        # The hand-soldered jack and the unfitted resistor need no listing; two placements of one part count twice.
        self.assertEqual((found["detail"]["factory_parts"], found["detail"]["factory_placements"]), (1, 2))
        self.assertEqual(found["detail"]["notes"], ["listed but not a factory part on the board: C77"])

    def test_stock_findings(self):
        self.assertEqual(self.supply()["findings"], ["C1 (R1, R2): no listing"])
        self.assertEqual(self.supply(self.listing(read_at="2026-10-01"))["findings"],
                         ["C1 (R1, R2): read 47 days ago, more than 30"])
        self.assertEqual(self.supply(self.listing(read_at="2026-12-01"))["findings"],
                         ["C1 (R1, R2): read_at 2026-12-01 is in the future"])
        self.assertEqual(self.supply(self.listing(listed_mpn="OTHER"))["findings"],
                         ["C1 (R1, R2): listed as OTHER, the board names MPN1"])
        bare = {"part": "C1"}
        self.assertEqual(self.supply(bare)["findings"], [
            "C1 (R1, R2): the listing has no listed_manufacturer", "C1 (R1, R2): the listing has no listed_mpn",
            "C1 (R1, R2): the listing has no class", "C1 (R1, R2): the listing has no unit_price_usd",
            "C1 (R1, R2): the listing has no read_at, the date it was read",
            "C1 (R1, R2): the listing has no stock count"])
        self.assertEqual(self.supply(self.listing(), self.listing())["findings"], ["C1: listed twice"])
        self.assertIn("planning_quantity is missing", self.supply(self.listing(), quantity=None)["findings"][0])

    def test_a_shortage_needs_the_owners_plan(self):
        short = self.supply(self.listing(stock=9))
        self.assertEqual(short["findings"], [
            "C1 (R1, R2): 9 in stock, 5 boards need 10, and the listing carries no owner_plan: the plan the owner "
            "accepted, in the owner's words"])
        planned = self.supply(self.listing(stock=9, owner_plan='"pre-order it"'))
        self.assertEqual((planned["outcome"], planned["detail"]["notes"]),
                         ("pass", ['C1 (R1, R2): 9 in stock, 5 boards need 10; the owner\'s plan: "pre-order it"']))

    def test_a_factory_part_with_no_supplier_part_is_a_finding(self):
        self.write(board_graph_actions.BOARD, self.BOARD.replace('(property "Supplier Part" "C1") ', "", 1))
        self.assertEqual(self.supply(self.listing())["findings"],
                         ["R1: a factory part with no Supplier Part, so no listing can name it"])

    # Verification

    def checks(self):
        folder = board_graph_actions.VERIFICATION_CHECKS
        names = board_graph_actions.gate_report_names()
        for name in names:
            self.write(f"{folder}/{name}", "{}")
        exclusions = pathlib.PurePosixPath(board_graph_actions.EXCLUSIONS)
        self.write(exclusions.as_posix(), "{}")
        return folder, names, exclusions

    def test_freshness_passes_only_when_every_report_is_stamped_against_this_board(self):
        found = board_graph_actions.freshness(self.root)
        self.assertEqual(found["outcome"], "needs_lead")
        self.assertIn(f"{board_graph_actions.VERIFICATION_CHECKS}: checks/ is not there: no report is stamped",
                      found["findings"])
        folder, names, exclusions = self.checks()
        found = board_graph_actions.freshness(self.root)
        self.assertEqual(found["findings"], [*(f"{folder}: {name} has no stamp" for name in names),
                                             f"{exclusions} has no stamp"])
        self.stamp(folder, *names)
        self.stamp(exclusions.parent.as_posix(), exclusions.name)
        self.write(f"{folder}/notes.md", "a narrative")
        found = board_graph_actions.freshness(self.root)
        self.assertEqual((found["outcome"], found["findings"], found["detail"]["notes"]),
                         ("pass", [], ["notes.md has no stamp"]))
        self.write(board_graph_actions.BOARD, self.BOARD + "\n")  # the board changed after the stamps
        found = board_graph_actions.freshness(self.root)
        self.assertEqual(found["findings"],
                         [*(f"{folder}: {name} is stamped against another board" for name in names),
                          f"{exclusions} is stamped against another board"])

    # Package

    def seal(self):
        """A sealed package as the packager leaves it: (the production folder, the complete package's folder)."""
        from kicad_package_production import CRITIC_REPORT, VISUAL_INSPECTION
        production = board_graph_actions.PRODUCTION
        board, schematic = self.root / board_graph_actions.BOARD, self.root / board_graph_actions.SCHEMATIC
        evidence = [*board_graph_actions.gate_report_names(), VISUAL_INSPECTION, CRITIC_REPORT]
        files = {"NAME.zip": "gerbers", "assembly-manifest.json": json.dumps({"board_sha256": hash_path(board).upper()}),
                 **{f"validation/{name}": "PASS: reviewed\n" for name in evidence}}
        for name, text in files.items():
            self.write(f"{production}/{name}", text)
        self.stamp(f"{production}/validation", *evidence)
        files["validation/provenance.json"] = None
        self.write(f"{production}/package-manifest.json", {
            "design_sources": {"pcb": {"sha256": hash_path(board)}, "schematic": {"sha256": hash_path(schematic)}},
            "gates": {"erc_violations": 0, "active_drc_violations": 0, "schematic_parity_issues": 0,
                      "unconnected_items": 0, "route_quality_errors": 0, "route_quality_warnings": 0,
                      "pad_mask_findings": 0, "accepted_library_warnings": [], "visual_inspection": "pass",
                      "fabrication_matches_fresh_export": {"all_layers_match_fresh_export": True}},
            "files": [{"path": name, "sha256": hash_path(self.root / production / name)} for name in files]})
        folder = board_graph_actions.PACKAGE_FOLDER
        archive = self.write(f"{folder}/NAME-complete-package.zip", "the bundle")
        self.write(f"{folder}/NAME-complete-package.sha256", f"{hash_path(archive).upper()}  {archive.name}\n")
        return production, folder

    def test_package_passes_on_a_sealed_package_made_from_this_board(self):
        found = board_graph_actions.package(self.root)
        self.assertEqual(found["outcome"], "needs_lead")
        self.assertIn("holds no sealed package (package-manifest.json, assembly-manifest.json not there)",
                      found["findings"][0])
        production, folder = self.seal()
        found = board_graph_actions.package(self.root)
        self.assertEqual((found["outcome"], found["findings"]), ("pass", []))
        self.assertEqual(found["detail"]["complete_package"]["file"], f"{folder}/NAME-complete-package.zip")

    def test_order_rechecks_the_archive_and_the_approved_preview(self):
        _, folder = self.seal()
        # Installed projects own their card definitions. Adopt the release scopes only on the test fixture.
        data = json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))
        data["open_issues"] = []
        nodes = {node["id"]: node for node in data["nodes"]}
        nodes["package"].update(who="agent", action=None, pass_check="package", approval_source="sealed_package")
        nodes["orientation_preview"].update(presentation="assembler_preview", approval_source="sealed_package",
                                            requires_current=["package"])
        nodes["order"].update(approval_source="sealed_package", requires_current=["package", "orientation_preview"])
        data["approval_mode"] = "enforce"
        graph = Graph(self.write("tools/test-graph.json", data))
        for key in ("package", "orientation_preview", "order"):
            for reading in graph.docs(key):
                self.write(reading, "Project review instructions")
        state = State(self.root / "db", self.root / "records")
        state.append(graph, "board", "verification", "pass", "package", "agent:test", "Begin at the sealed release")
        archive = f"{folder}/NAME-complete-package.zip"
        preview = "owner/orientation.png"
        self.write(preview, "original preview from the upload")
        record(graph, state, "package", "pass", "agent:test", "Sealed package", [archive], root=self.root)
        record(graph, state, "orientation_preview", "needs_owner", "agent:test", "Preview of this upload",
               [preview], root=self.root)
        approved = record(graph, state, "orientation_preview", "pass", "owner", "Approved these orientations",
                          [preview], root=self.root)
        record(graph, state, "quote", "pass", "owner", "Disposable quote", [preview], root=self.root)
        def order():
            return record(graph, state, "order", "pass", "owner", "Disposable order record", [preview], root=self.root)
        checksum = self.root / folder / "NAME-complete-package.sha256"
        original_checksum = checksum.read_bytes()
        self.write(archive, "replacement bundle")
        with self.assertRaisesRegex(ValueError, "does not match the checksum"):
            order()
        self.assertEqual(state.position(graph, "board"), "order")
        zip_path = self.root / archive
        checksum.write_text(f"{hash_path(zip_path).upper()}  {zip_path.name}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Changed: source:sealed_package"):
            order()
        self.write(archive, "the bundle")
        checksum.write_bytes(original_checksum)
        self.write(preview, "another preview")
        with self.assertRaisesRegex(ValueError, "Presented evidence changed"):
            order()
        self.write(preview, "original preview from the upload")
        accepted = order()
        self.assertEqual(accepted["target"], "delivery")
        scope = accepted["inputs"]["approval_v1"]
        self.assertEqual(scope["source:sealed_package"]["sha256"].lower(), hash_path(zip_path))
        self.assertEqual(scope["review:orientation_preview"], approved["at"])
        self.assertEqual(approved["inputs"]["approved_presentation"]["files"], {preview: hash_path(self.root / preview)})

    def test_package_findings(self):
        production, folder = self.seal()
        self.write(f"{production}/NAME.zip", "other gerbers")
        self.write(f"{production}/OLD_2026-01-01.zip", "an older export")
        self.write(f"{folder}/NAME-complete-package.zip", "another bundle")
        self.write(f"{production}/validation/G10_BOARD_CRITIC_REPORT.md", "FAIL: the silk is mirrored\n")
        found = board_graph_actions.package(self.root)["findings"]
        self.assertEqual(found, [
            "NAME.zip: changed since the package was sealed",
            "validation/G10_BOARD_CRITIC_REPORT.md: changed since the package was sealed",
            f"OLD_2026-01-01.zip: in {production}, not in the package manifest (the folder holds only this package)",
            "G10_BOARD_CRITIC_REPORT.md's first line is not PASS: the independent review (G10) has not passed this "
            "package",
            "NAME-complete-package.zip does not match the checksum in NAME-complete-package.sha256"])

    def test_a_package_made_from_another_board_or_with_an_open_gate_does_not_pass(self):
        production, _ = self.seal()
        manifest = json.loads((self.root / production / "package-manifest.json").read_text(encoding="utf-8"))
        manifest["gates"]["unconnected_items"] = 2
        self.write(f"{production}/package-manifest.json", manifest)
        self.assertEqual(board_graph_actions.package(self.root)["findings"],
                         ["the package manifest's unconnected_items is 2, not 0"])
        self.write(board_graph_actions.BOARD, self.BOARD + "\n")
        found = board_graph_actions.package(self.root)["findings"]
        self.assertEqual(found[:2], [
            f"the package was made from another board: its manifest's hash is not "
            f"{pathlib.PurePosixPath(board_graph_actions.BOARD).name}'s",
            f"the assembly files were checked against another board: assembly-manifest.json's hash is not "
            f"{pathlib.PurePosixPath(board_graph_actions.BOARD).name}'s"])
        self.assertTrue(any("is stamped against another board" in line for line in found))

    def test_package_accepts_route_quality_warnings_only_with_verified_routing_receipts(self):
        import routing_evidence
        production, _ = self.seal()
        warning = {"severity": "warning", "rule": "micro_segment", "net": "/MOTOR_A", "layer": 0,
                   "point_mm": [10.0, 20.0], "message": "0.1200 mm: (10.0, 20.0) -> (10.085, 20.085)"}
        detail = routing_evidence.route_quality_warning_detail(warning)
        route_report = {"summary": {"errors": 0, "warnings": 1, "tracks": 4, "vias": 0}, "findings": [warning]}
        self.write(f"{production}/validation/route-quality.json", route_report)
        from kicad_package_production import CRITIC_REPORT, VISUAL_INSPECTION
        evidence = [*board_graph_actions.gate_report_names(), VISUAL_INSPECTION, CRITIC_REPORT]
        self.stamp(f"{production}/validation", *evidence)
        manifest = json.loads((self.root / production / "package-manifest.json").read_text(encoding="utf-8"))
        for entry in manifest["files"]:
            entry["sha256"] = hash_path(self.root / production / entry["path"])
        manifest["gates"]["route_quality_warnings"] = 1
        manifest["gates"]["accepted_route_quality_warnings"] = [
            {"detail": detail, "receipt": "hardware/board/routing/run-11111111.jsonl",
             "task": "01-a", "reviewer": "gpt-6.1-terra", "board_sha256": "a" * 64, "reason": "clean branch"}
        ]
        self.write(f"{production}/package-manifest.json", manifest)
        # Without the committed routing receipt, the package fails.
        self.assertTrue(any("unreviewed warning" in line for line in board_graph_actions.package(self.root)["findings"]))
        # Adding the verified adopted receipt lets the sealed package pass.
        receipt_path = (self.root / board_graph_actions.BOARD).parent / "routing" / "run-11111111.jsonl"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        step = {"task": "01-a", "kept": True, "why": "warning_accepted", "reviews": 1, "review_open": [],
                "provider": "codex", "model": "gpt-6.1-terra", "review_provider": "codex",
                "review_model": "gpt-6.1-terra", "new_findings": [detail.split(",", 1)[0]],
                "matching_geometry": [detail], "reviewed_sha256": "a" * 64, "pad_exits": [],
                "review_receipt": {"schema": "board-graph/routing-review/v1", "board_sha256": "a" * 64,
                                   "reviewer": "gpt-6.1-terra", "answer": "FINE AS IS: Warning 1: clean branch",
                                   "earlier": [], "warnings": [detail]}}
        summary = {"summary": True, "adopted": True, "kept": 1, "tasks": 1, "provider": "codex",
                   "pcb": board_graph_actions.BOARD}
        receipt_path.write_text("\n".join(json.dumps(r) for r in (step, summary)) + "\n", encoding="utf-8")
        found = board_graph_actions.package(self.root)
        self.assertEqual((found["outcome"], found["findings"]), ("pass", []))
        # Mismatching accepted_route_quality_warnings in manifest is rejected even with the receipt present.
        manifest["gates"]["accepted_route_quality_warnings"] = []
        self.write(f"{production}/package-manifest.json", manifest)
        self.assertTrue(any("accepted_route_quality_warnings does not match" in line
                            for line in board_graph_actions.package(self.root)["findings"]))


class InventoryTests(unittest.TestCase):
    """Inventory (baseline) on a project made here, under the names this checkout's board description gives: a
    board with a part needs all five files; before that the project is at the schematic stage and what is not
    there yet is named; the later cards that read the board refuse without it in a sentence."""

    BOARD_WITH_A_PART = '(kicad_pcb (version 1) (footprint "lib:R" (layer "F.Cu") (property "Reference" "R1")))'

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.write(board_graph_actions.SCHEMATIC, "(kicad_sch (version 1))")
        self.write(board_graph_actions.PROJECT_FILE, "{}")

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def rules_and_bom(self):
        self.write(board_graph_actions.RULES, "(version 1)\n# the board's own rules\n(rule narrow)\n")
        self.write(board_graph_actions.BOM, ",".join(board_graph_actions.BOM_COLUMNS) + "\nR1,1,10k,Maker,MPN1,S,C1\n")

    def test_a_board_with_a_part_needs_all_five_files_as_before(self):
        self.write(board_graph_actions.BOARD, self.BOARD_WITH_A_PART)
        found = board_graph_actions.baseline(self.root)
        self.assertEqual(found["outcome"], "needs_lead")
        self.assertEqual([finding.split(":")[0] for finding in found["findings"]],
                         [board_graph_actions.RULES, board_graph_actions.BOM])
        self.assertTrue(all("does not read cleanly" in finding for finding in found["findings"]))
        self.assertEqual((found["detail"]["stage"], found["detail"]["not_yet"]), ("board", []))
        self.assertNotIn("notes", found["detail"])
        self.rules_and_bom()
        found = board_graph_actions.baseline(self.root)
        self.assertEqual((found["outcome"], found["findings"]), ("pass", []))
        self.assertEqual(set(found), {"outcome", "findings", "detail"})
        self.assertEqual(found["detail"]["read"],
                         {name: hash_path(self.root / name) for name in board_graph_actions.DESIGN})
        self.assertEqual((found["detail"]["board"]["footprints"], found["detail"]["stage"]), (1, "board"))
        self.assertNotIn("notes", found["detail"])

    def test_a_board_file_that_does_not_parse_counts_as_a_board(self):
        self.write(board_graph_actions.BOARD, "(kicad_pcb (version 1)")
        found = board_graph_actions.baseline(self.root)
        self.assertEqual(found["findings"][0], f"{board_graph_actions.BOARD}: does not read cleanly: unterminated list")
        self.assertEqual(len(found["findings"]), 3)  # the rules and the BOM are required too
        self.assertEqual(found["detail"]["stage"], "board")

    def test_with_no_board_file_the_schematic_stage_passes_and_names_what_is_not_there_yet(self):
        found = board_graph_actions.baseline(self.root)
        self.assertEqual((found["outcome"], found["findings"]), ("pass", []))
        self.assertEqual(found["detail"]["stage"], "schematic")
        self.assertIsNone(found["detail"]["board"])
        self.assertEqual([item["file"] for item in found["detail"]["not_yet"]],
                         [board_graph_actions.BOARD, board_graph_actions.RULES, board_graph_actions.BOM])
        self.assertEqual(sorted(found["detail"]["read"]),
                         sorted([board_graph_actions.SCHEMATIC, board_graph_actions.PROJECT_FILE]))
        first, second = found["detail"]["notes"]
        self.assertEqual(first, "schematic stage: there is no board file yet")
        self.assertTrue(second.startswith(f"not there yet: {board_graph_actions.BOARD} (the board, which the first "
                                          "update from the schematic makes); "), second)
        self.assertIn(f"{board_graph_actions.BOM} (the exported BOM, ", second)
        board_graph.checked(found)  # the shape next records

    def test_a_board_file_with_no_part_is_the_schematic_stage_too(self):
        self.write(board_graph_actions.BOARD, "(kicad_pcb (version 1) (net 0 \"\"))")
        found = board_graph_actions.baseline(self.root)
        self.assertEqual((found["outcome"], found["detail"]["stage"]), ("pass", "schematic"))
        self.assertEqual(found["detail"]["board"]["footprints"], 0)
        self.assertEqual([item["file"] for item in found["detail"]["not_yet"]],
                         [board_graph_actions.RULES, board_graph_actions.BOM])
        self.assertEqual(found["detail"]["notes"][0],
                         f"schematic stage: {board_graph_actions.BOARD} holds no part yet")
        self.rules_and_bom()
        found = board_graph_actions.baseline(self.root)
        self.assertEqual((found["outcome"], found["detail"]["not_yet"]), ("pass", []))
        self.assertEqual(found["detail"]["notes"], [f"schematic stage: {board_graph_actions.BOARD} holds no part yet"])

    def test_the_schematic_stage_still_needs_the_schematic_and_what_exists_to_read_cleanly(self):
        (self.root / board_graph_actions.SCHEMATIC).unlink()
        self.write(board_graph_actions.BOM, ",".join(board_graph_actions.BOM_COLUMNS) + "\n")
        self.write(board_graph_actions.RULES, "(rule open")
        found = board_graph_actions.baseline(self.root)
        self.assertEqual(found["outcome"], "needs_lead")
        self.assertEqual([finding.split(":")[0] for finding in found["findings"]],
                         [board_graph_actions.SCHEMATIC, board_graph_actions.RULES, board_graph_actions.BOM])
        self.assertTrue(found["findings"][2].endswith("does not read cleanly: no rows"))
        self.assertEqual([item["file"] for item in found["detail"]["not_yet"]], [board_graph_actions.BOARD])

    def test_the_cards_that_read_the_board_refuse_without_it_in_a_sentence(self):
        board = (f"{board_graph_actions.BOARD} is not there yet: this card reads the board, which the first update "
                 "from the schematic makes")
        bom = (f"{board_graph_actions.BOM} is not there yet: this card reads the exported BOM, which the first BOM "
               "export from the schematic writes")
        for name, expected in (("bom", [board, bom]), ("models", [board, bom]),
                               ("footprints", [board]),
                               ("placement", [board, f"{board_graph_actions.PLACEMENT} is not there yet: this card "
                                                     "reads it"])):
            with self.subTest(action=name):
                found = ACTIONS[name].run(self.root)
                self.assertEqual((found["outcome"], found["findings"]), ("needs_lead", expected))
                board_graph.checked(found)

    def test_pre_schematic_stock_and_datasheets_validate_without_board_or_bom(self):
        # Without board or BOM, supply_receipt and datasheets validate pre-schematic part selections cleanly.
        now = board_graph_actions.datetime.datetime(2026, 11, 15, tzinfo=board_graph_actions.datetime.timezone.utc)
        self.assertEqual(board_graph_actions.supply_receipt(self.root, now=now)["outcome"], "needs_lead")
        self.assertEqual(board_graph_actions.datasheets(self.root)["outcome"], "needs_lead")
        self.write(board_graph_actions.STOCK_LISTINGS, json.dumps({
            "planning_quantity": 5,
            "factory_parts": [
                {"part": "C1", "read_at": "2026-11-10T07:10:28Z", "listed_manufacturer": "Maker",
                 "listed_mpn": "MPN1", "class": "Basic", "stock": 100, "unit_price_usd": 0.05},
            ],
        }))
        sup = board_graph_actions.supply_receipt(self.root, now=now)
        self.assertEqual((sup["outcome"], sup["detail"]["stage"], sup["detail"]["factory_parts"]),
                         ("pass", "pre_schematic", 1))
        (self.root / board_graph_actions.DATASHEETS).mkdir(parents=True, exist_ok=True)
        (self.root / board_graph_actions.DATASHEETS / "MPN1.pdf").write_bytes(b"%PDF-1.4\n")
        self.write(board_graph_actions.PARTS, json.dumps({"parts": {"MPN1": {"datasheet": "MPN1.pdf", "height_mm": 1.2}}}))
        ds = board_graph_actions.datasheets(self.root)
        self.assertEqual((ds["outcome"], ds["detail"]["stage"], ds["detail"]["parts_checked"]),
                         ("pass", "pre_schematic", 1))


class NoPartsTests(unittest.TestCase):
    """A board that carries copper shapes and no part (a flexible strip whose only footprints are its contact
    tabs, every one kept out of the BOM) goes through Inventory, BOM, Stock and Placement with nothing to compare,
    and says so in a note. The same board with one part on it gets each card's finding as before."""

    TAB = ' (footprint "lib:Tab" (layer "F.Cu") (attr smd exclude_from_bom) (property "Reference" "{ref}"))\n'
    PART = ' (footprint "lib:R" (layer "F.Cu") (attr smd) (property "Reference" "R1"))\n'
    NOTE = "this board carries no part (every footprint on it is kept out of the BOM): "

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.write(board_graph_actions.SCHEMATIC,
                   '(kicad_sch (version 1)\n'
                   ' (symbol (lib_id "x:Tab") (in_bom no) (dnp no) (property "Reference" "JB1"))\n'
                   ' (symbol (lib_id "x:Tab") (in_bom no) (dnp no) (property "Reference" "JM1")))\n')
        self.write(board_graph_actions.PROJECT_FILE, "{}")
        self.write(board_graph_actions.RULES, "(version 1)\n")
        self.write(board_graph_actions.BOM, ",".join(board_graph_actions.BOM_COLUMNS) + "\n")
        self.write(board_graph_actions.GRAPH, json.dumps({"nodes": []}))
        self.board()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def board(self, extra=""):
        self.write(board_graph_actions.BOARD, "(kicad_pcb (version 1)\n" + self.TAB.format(ref="JB1")
                   + self.TAB.format(ref="JM1") + extra + ")\n")

    def run_cards(self):
        return {name: ACTIONS[name].run(self.root) for name in ("baseline", "bom", "supply_receipt", "placement")}

    def test_a_board_with_no_part_passes_the_cards_that_compare_parts(self):
        for name, found in self.run_cards().items():
            with self.subTest(action=name):
                self.assertEqual((found["outcome"], found["findings"]), ("pass", []))
                board_graph.checked(found)  # the shape next records
        found = self.run_cards()
        self.assertEqual(found["baseline"]["detail"]["stage"], "board")
        self.assertEqual(found["baseline"]["detail"]["notes"], [self.NOTE + "its exported BOM holds no row"])
        self.assertEqual(found["bom"]["detail"]["fitted_components"], 0)
        self.assertEqual(found["supply_receipt"]["detail"]["notes"],
                         [self.NOTE + f"the factory places nothing, so {board_graph_actions.STOCK_LISTINGS} is not "
                                      "needed"])
        self.assertEqual(found["placement"]["detail"]["notes"],
                         [self.NOTE + f"there is no part to place, so {board_graph_actions.PLACEMENT} is not needed"])
        self.assertEqual(found["placement"]["detail"]["parts_compared"], 0)

    def test_one_part_on_the_board_brings_every_finding_back(self):
        self.board(self.PART)
        found = self.run_cards()
        self.assertEqual(found["baseline"]["findings"],
                         [f"{board_graph_actions.BOM}: does not read cleanly: no rows"])
        self.assertIn("Factory SMT must occupy one side: []", found["bom"]["findings"])
        self.assertEqual(found["supply_receipt"]["findings"][0],
                         "R1: a factory part with no Supplier Part, so no listing can name it")
        self.assertIn("is not there", found["supply_receipt"]["findings"][1])
        self.assertEqual(found["placement"]["findings"],
                         [f"{board_graph_actions.PLACEMENT} is not there yet: this card reads it"])
        self.assertTrue(all(item["outcome"] == "needs_lead" for item in found.values()))

    def test_a_board_file_with_no_footprint_is_not_a_board_with_no_part(self):
        self.write(board_graph_actions.BOARD, "(kicad_pcb (version 1))")
        found = self.run_cards()
        self.assertEqual(found["baseline"]["detail"]["stage"], "schematic")
        self.assertEqual(found["bom"]["findings"], ["Factory SMT must occupy one side: []"])
        self.assertIn("is not there", found["supply_receipt"]["findings"][0])
        self.assertEqual(found["placement"]["findings"],
                         [f"{board_graph_actions.PLACEMENT} is not there yet: this card reads it"])

    def test_a_placement_file_on_a_board_with_no_part_is_still_compared(self):
        self.write(board_graph_actions.PLACEMENT,
                   json.dumps({"parts": {"JB9": {"x": 0, "y": 0, "rotation": 0, "side": "front"}}}))
        found = ACTIONS["placement"].run(self.root)
        self.assertEqual((found["outcome"], found["findings"]), ("needs_lead", ["JB9: is not on the board"]))

    def test_a_board_with_no_part_still_needs_its_exported_bom_and_its_columns(self):
        (self.root / board_graph_actions.BOM).unlink()
        self.assertEqual(len(ACTIONS["baseline"].run(self.root)["findings"]), 1)
        self.write(board_graph_actions.BOM, "Refs,Qty\n")
        self.assertIn("missing columns", ACTIONS["baseline"].run(self.root)["findings"][0])
        self.write(board_graph_actions.BOM, "Refs,Qty,Value\nR1,1,1k\n")
        found = ACTIONS["bom"].run(self.root)
        self.assertEqual(found["outcome"], "needs_lead")
        self.assertIn("missing columns: Manufacturer, MPN, Supplier, Supplier Part", found["findings"][0])


class ViewTests(unittest.TestCase):
    """The views next compares: part fields move neither; positions move both; copper and drawn text move one."""

    BOARD = """(kicad_pcb (version 20260306) (generator "pcbnew") (generator_version "10.0")
  (net 0 "") (net 1 "GND")
  (footprint "lib:R0603" (layer "F.Cu") (at 10 20 90)
    (property "Reference" "R1" (at 0 0 0)) (property "Value" "220k" (at 0 0 0))
    (property "Datasheet" "https://a" (at 0 0 0)) (property "LCSC Part #" "C1" (at 0 0 0))
    (pad "1" smd rect (at -0.75 0) (size 0.8 0.9) (layers "F.Cu") (net 1 "GND")))
  (segment (start 0 0) (end 1 1) (width 0.25) (layer "F.Cu") (net 1))
)"""

    def test_part_fields_and_the_file_header_move_neither_view(self):
        changed = (self.BOARD.replace('"C1"', '"C2"').replace('"https://a"', '"https://b"')
                   .replace("(version 20260306)", "(version 20270101)"))
        self.assertEqual(board_graph_actions.placement_view(self.BOARD), board_graph_actions.placement_view(changed))
        self.assertEqual(board_graph_actions.copper_and_silk_view(self.BOARD),
                         board_graph_actions.copper_and_silk_view(changed))

    def test_edge_and_mating_pad_geometry_invalidate_placement(self):
        view = board_graph_actions.placement_view
        edge = '(gr_rect (start 0 0) (end 30 30) (layer "Edge.Cuts"))'
        board = self.BOARD.replace("(segment", edge + "\n(segment")
        self.assertNotEqual(view(board), view(board.replace("(end 30 30)", "(end 5 30)")))
        self.assertNotEqual(view(board), view(board.replace("(size 0.8 0.9)", "(size 2 2)")))
    def test_positions_copper_and_drawn_text_move_the_views_they_belong_to(self):
        moved = self.BOARD.replace("(at 10 20 90)", "(at 10.1 20 90)")
        self.assertNotEqual(board_graph_actions.placement_view(self.BOARD), board_graph_actions.placement_view(moved))
        self.assertNotEqual(board_graph_actions.copper_and_silk_view(self.BOARD),
                            board_graph_actions.copper_and_silk_view(moved))
        rerouted = self.BOARD.replace("(end 1 1)", "(end 2 1)")
        self.assertEqual(board_graph_actions.placement_view(self.BOARD), board_graph_actions.placement_view(rerouted))
        self.assertNotEqual(board_graph_actions.copper_and_silk_view(self.BOARD),
                            board_graph_actions.copper_and_silk_view(rerouted))
        relabeled = self.BOARD.replace('"Value" "220k"', '"Value" "221k"')  # the value is drawn on the silk
        self.assertNotEqual(board_graph_actions.copper_and_silk_view(self.BOARD),
                            board_graph_actions.copper_and_silk_view(relabeled))

    def test_the_graph_names_views_that_exist(self):
        named = {node.get("unchanged_pass") for node in Graph().data["nodes"] if node.get("unchanged_pass")}
        self.assertEqual(named, {"placement", "copper_and_silk"})
        self.assertEqual(named - set(board_graph_actions.VIEWS), set())

    def test_kicad10_first_save_normalizations_preserve_placement_and_purchasing_views(self):
        k9 = """(kicad_pcb (version 20241229) (generator "pcbnew")
  (gr_line (start 0 0.0) (end 30.0 0) (layer "Edge.Cuts"))
  (footprint "lib:H" (layer "F.Cu") (at 3.5 3.5 0) (attr exclude_from_bom exclude_from_pos_files)
    (property "Reference" "H1" (at 0 -3.5) (layer "F.Fab"))
    (property "Footprint" "lib:H" (at 0 0) (layer "F.Fab") (hide yes))
    (fp_circle (center 0 0) (end 3.0 0) (layer "F.SilkS"))
    (pad "1" np_thru_hole circle (at 0 0) (size 3.2 3.2) (drill 3.2) (layers "*.Cu" "*.Mask"))))"""
        k10 = """(kicad_pcb (version 20260206) (generator "pcbnew")
  (gr_line (start 0 0) (end 30 0) (layer "Edge.Cuts") (uuid "u1"))
  (footprint "lib:H" (layer "F.Cu") (at 3.5 3.5) (attr exclude_from_pos_files exclude_from_bom)
    (property "Reference" "H1" (at 0 -3.5) (layer "F.Fab") (uuid "u2"))
    (property "Description" "" (at 0 0) (layer "F.Fab") (hide yes) (uuid "u3"))
    (fp_circle (center 0 0) (end 3 0) (layer "F.SilkS") (uuid "u4"))
    (pad "1" np_thru_hole circle (at 0 0) (size 3.2 3.2) (drill 3.2) (layers "*.Cu" "*.Mask") (uuid "u5"))))"""
        self.assertEqual(board_graph_actions.placement_view(k9), board_graph_actions.placement_view(k10))
        self.assertEqual(board_graph_actions.purchasing_view(k9), board_graph_actions.purchasing_view(k10))
        self.assertNotEqual(
            board_graph_actions.placement_view(k9),
            board_graph_actions.placement_view(k10.replace("(end 30 0)", "(end 30.1 0)")),
        )
        self.assertNotEqual(
            board_graph_actions.purchasing_view(k9),
            board_graph_actions.purchasing_view(k10.replace("exclude_from_pos_files exclude_from_bom", "exclude_from_pos_files")),
        )
        self.assertNotEqual(
            board_graph_actions.purchasing_view(k9),
            board_graph_actions.purchasing_view(k10.replace('(property "Description" ""', '(property "Description" "Custom"')),
        )


class CiChecksTests(unittest.TestCase):
    """CI's board checks live in the database: the validate workflow's last step writes a row, where reads it."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        self.state = State(self.root / "state.sqlite3", self.root / "records")

    def tearDown(self):
        self.directory.cleanup()

    def test_keep_checks_writes_a_row_from_summary_json_and_where_reads_it_back(self):
        self.assertIn("record_checks", board_graph.OPS)  # a PC writes it through serve on the Mac
        drc = "Board rules check, DRC (G2), zones refilled"
        checks = [{"check": drc, "ok": True, "detail": "0 copper violations", "open": 0},
                  {"check": "Routing progress", "ok": True, "detail": "none open"}]
        summary = self.root / "summary.json"
        summary.write_text(json.dumps({"pass": True, "checks": checks}), encoding="utf-8")
        row = board_graph.keep_checks(self.state, "abc123def456", 42, "push", summary, at="2026-11-06T21:34:59+00:00")
        self.assertEqual((row["commit"], row["run_id"], row["event"], row["passed"], row["open"], row["checks"],
                          row["note"]), ("abc123def456", "42", "push", True, 0, checks, None))
        self.assertEqual(self.state.checks_for("abc123"), row)
        self.assertEqual(self.state.latest_checks(), row)
        self.assertIsNone(self.state.checks_for("fff"))
        self.assertEqual(board_graph.checks_words(row), "pass, 0 open; run 42 (push) at 2026-11-06T21:34:59Z")
        self.assertEqual(board_graph.ci_lines(board_graph.ci_section(self.state, "abc123def456")),
                         ["CI, this commit (abc123de): pass, 0 open; run 42 (push) at 2026-11-06T21:34:59Z"])
        self.assertEqual(board_graph.ci_lines(board_graph.ci_section(self.state, "9999999999")),
                         ["CI, this commit (99999999): no result on the graph yet",
                          "CI, newest: abc123de, pass, 0 open; run 42 (push) at 2026-11-06T21:34:59Z"])
        # A failing run, and one that left no summary: both rows, neither a pass; the newest row on a commit wins.
        failed = [{"check": drc, "ok": False, "detail": "2 violations", "open": 3},
                  {"check": "Route quality (G3)", "ok": True, "inherited": True, "detail": "inherited: 1 errors"}]
        summary.write_text(json.dumps({"pass": False, "checks": failed}), encoding="utf-8")
        row = board_graph.keep_checks(self.state, "abc123def456", 43, "pull_request", summary,
                                      at="2026-11-06T22:00:00+00:00")
        self.assertEqual(board_graph.checks_words(row), f"FAIL, 3 open; failing: {drc}; inherited: Route quality "
                                                        "(G3); run 43 (pull_request) at 2026-11-06T22:00:00Z")
        self.assertEqual(self.state.checks_for("abc123def456")["run_id"], "43")
        none = board_graph.keep_checks(self.state, "abc123def456", 44, "push", self.root / "none.json")
        self.assertEqual((none["passed"], none["open"], none["checks"], none["note"]),
                         (None, None, [], "no summary.json: the board checks did not finish"))
        self.assertTrue(board_graph.checks_words(none).startswith("no summary.json: the board checks did not finish; "
                                                                  "run 44 (push) at "))
        empty = State(self.root / "empty.sqlite3", self.root / "records")
        self.assertEqual(board_graph.ci_lines(board_graph.ci_section(empty, "abc123def456")),
                         ["CI, this commit (abc123de): no result on the graph yet",
                          "CI: no run has written to the graph yet"])
        self.assertEqual(board_graph.ci_lines(None), [])


class FindingTests(unittest.TestCase):
    """The checks behind the connected actions that read no board: a DRC report and reference ranges."""

    def test_drc_library_findings_read_the_two_library_checks_and_keep_the_rest_as_detail(self):
        drc = {"kicad_version": "10.0.8", "ignored_checks": [], "unconnected_items": [], "violations": [
            {"type": "lib_footprint_mismatch", "items": [{"description": "Footprint R14"}]},
            {"type": "lib_footprint_issues", "items": [{"description": "Footprint J9"}]},
            {"type": "missing_courtyard", "items": [{"description": "Footprint MH1"}]},
            {"type": "track_not_centered_on_via",
             "items": [{"description": "Track [/AMP_EN] on Top Layer"}, {"description": "Via [/AMP_EN]"}]}]}
        findings, detail = board_graph_actions.drc_library_findings(drc)
        self.assertEqual(findings, ["R14: differs from its library copy (lib_footprint_mismatch)",
                                    "J9: is not found in its library (lib_footprint_issues)"])
        self.assertEqual(detail["library_checks"], {"lib_footprint_mismatch": ["R14"], "lib_footprint_issues": ["J9"]})
        self.assertEqual(detail["other_violations_on_the_copy"],
                         {"missing_courtyard": ["MH1"],
                          "track_not_centered_on_via": ["Track [/AMP_EN] on Top Layer, Via [/AMP_EN]"]})
        drc["ignored_checks"] = ["lib_footprint_mismatch"]
        self.assertEqual(board_graph_actions.drc_library_findings(drc)[0][0],
                         "DRC ignored lib_footprint_mismatch, so the copy was not compared")

    def test_reference_ranges(self):
        self.assertEqual(refs("C7-C9,C15"), ["C7", "C8", "C9", "C15"])
        with self.assertRaises(ValueError):
            refs("C7-R9")
        with self.assertRaises(ValueError):
            refs("C9-C7")


def git_in(cwd, *args):
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False, **NO_WINDOW_KWARGS)
    if done.returncode:
        raise AssertionError(f"git {' '.join(args)}: {done.stderr}")
    return done.stdout


class LandTests(unittest.TestCase):
    """A record carries itself to its branch: land() commits the record file (and its report) and pushes, as the
    last step of a card's move, against a temporary origin; what cannot happen is said, never raised."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.directory.name)
        self.origin, self.pc, self.other = base / "origin.git", base / "pc", base / "other"
        git_in(base, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git_in(base, "init", "-q", "-b", "main", str(self.pc))
        git_in(self.pc, "remote", "add", "origin", str(self.origin))
        for key, value in (("user.email", "test@example.com"), ("user.name", "test"), ("core.autocrlf", "false")):
            git_in(self.pc, "config", key, value)
        (self.pc / "README.md").write_text("one\n")
        git_in(self.pc, "add", "-A")
        git_in(self.pc, "commit", "-q", "-m", "one")
        git_in(self.pc, "push", "-q", "-u", "origin", "main")
        self.records = self.pc / "tools" / "board_graph_records"
        self.records.mkdir(parents=True)
        self.record = self.records / "2026-11-06T18-00-00Z-inventory-more_work.json"
        self.record.write_text("{}\n")
        patcher = mock.patch.object(mac, "LOCAL", False)  # these play the PC, wherever they run
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.directory.cleanup()

    def test_the_record_and_its_report_are_committed_alone_and_pushed(self):
        report = self.pc / "board" / "reports" / "board-graph" / "r.json"
        report.parent.mkdir(parents=True)
        report.write_text("{}\n")
        (self.pc / "scratch.txt").write_text("not the record's\n")  # untracked, stays untracked
        said = board_graph.land(self.pc, [self.record, report, self.pc / "board" / "missing.json",
                                           pathlib.Path(self.directory.name) / "outside.json"], "Record: Inventory, more_work")
        head = git_in(self.pc, "rev-parse", "--short", "HEAD").strip()
        self.assertEqual(said, f"committed {head} on main and pushed to origin")
        self.assertEqual(git_in(self.pc, "log", "-1", "--format=%s").strip(), "Record: Inventory, more_work")
        self.assertEqual(sorted(git_in(self.pc, "show", "--name-only", "--format=", "HEAD").split()),
                         ["board/reports/board-graph/r.json", "tools/board_graph_records/" + self.record.name])
        self.assertEqual(git_in(self.pc, "rev-parse", "origin/main").strip(), git_in(self.pc, "rev-parse", "HEAD").strip())
        self.assertIn("?? scratch.txt", git_in(self.pc, "status", "--porcelain"))

    def test_the_pull_request_says_what_the_branch_holds_for_every_board(self):
        """The records a branch adds are read from every board's records folder, each with its board's name; a
        repository with one board reads its one folder and names no board."""
        def item(node, at, note):
            return {"at": at, "track": "board", "node": node, "outcome": "pass", "target": "x", "actor": "owner",
                    "note": note, "evidence": [], "inputs": {}}
        git_in(self.pc, "checkout", "-q", "-b", "work")
        other = self.pc / "tools" / "board_graph_records.driver"
        other.mkdir()
        self.record.write_text(json.dumps(item("inventory", "2026-11-06T18:00:00+00:00", "the strip's")))
        (other / "2026-11-06T19-00-00Z-inventory-pass.json").write_text(
            json.dumps(item("inventory", "2026-11-06T19:00:00+00:00", "the driver's")))
        (other / "deeper").mkdir()
        (other / "deeper" / "not-a-record.json").write_text("{}")
        git_in(self.pc, "add", "-A")
        git_in(self.pc, "commit", "-q", "-m", "two records")
        one = board_graph.branch_records(self.pc, "work")
        self.assertEqual([(found["board"], found["note"]) for found in one], [(None, "the strip's")])
        folders = {"tools/board_graph_records": "strip", "tools/board_graph_records.driver": "driver"}
        both = board_graph.branch_records(self.pc, "work", folders)
        self.assertEqual(sorted((found["board"], found["note"]) for found in both),
                         [("driver", "the driver's"), ("strip", "the strip's")])

        class NoChecks:
            def checks_for(self, head):
                return None

        with mock.patch.object(board_graph, "branch_records", lambda root, branch: both):
            body = board_graph.pull_request_body(Graph(FIXTURE_GRAPH), NoChecks(), self.pc, "work", "a" * 40)
        self.assertIn("Z strip: Inventory, pass by owner: the strip's", body)
        self.assertIn("Z driver: Inventory, pass by owner: the driver's", body)
        with mock.patch.object(board_graph, "branch_records", lambda root, branch: one):
            body = board_graph.pull_request_body(Graph(FIXTURE_GRAPH), NoChecks(), self.pc, "work", "a" * 40)
        self.assertIn("- 2026-11-06T18:00Z Inventory, pass by owner: the strip's", body)

    def test_a_push_that_fails_leaves_the_commit_and_says_so(self):
        git_in(pathlib.Path(self.directory.name), "clone", "-q", str(self.origin), str(self.other))
        for key, value in (("user.email", "test@example.com"), ("user.name", "test")):
            git_in(self.other, "config", key, value)
        (self.other / "README.md").write_text("two\n")
        git_in(self.other, "commit", "-q", "-am", "two")
        git_in(self.other, "push", "-q", "origin", "main")  # origin's main moved: the PC's push is not a fast-forward
        said = board_graph.land(self.pc, [self.record], "Record: Inventory, more_work")
        head = git_in(self.pc, "rev-parse", "--short", "HEAD").strip()
        self.assertTrue(said.startswith(f"committed {head} on main; the push failed (git push"), said)
        self.assertTrue(said.endswith("): it goes with the next push"), said)
        self.assertEqual(git_in(self.pc, "log", "-1", "--format=%s").strip(), "Record: Inventory, more_work")

    def test_a_record_on_a_branch_opens_the_pull_request_then_keeps_it_and_readies_it_only_at_a_checked_merge_ask(self):
        git_in(self.pc, "checkout", "-q", "-b", "work")
        self.record.write_text(json.dumps({"at": "2026-11-06T18:00:00.000000+00:00", "track": "board", "node": "inventory",
                                           "outcome": "more_work", "target": "inventory", "actor": "agent:a",
                                           "note": "read the files", "evidence": [], "inputs": {}}), encoding="utf-8")
        self.assertTrue(board_graph.land(self.pc, [self.record], "Record: Inventory, more_work").endswith("on work and pushed to origin"))
        graph = Graph()
        state = State(pathlib.Path(self.directory.name) / "state.sqlite3",
                      pathlib.Path(self.directory.name) / "no-records")  # the branch's records are read from git
        calls, listed = [], [[]]

        def fake_gh(root, *args):
            calls.append(args)
            if args[:2] == ("pr", "list"):
                return json.dumps(listed[0])
            if args[:2] == ("pr", "create"):
                body = pathlib.Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
                self.assertIn("- 2026-11-06T18:00Z Inventory, more_work by agent:a: read the files", body)
                self.assertIn("no result on the graph yet", body)
                listed[0] = [{"number": 7, "isDraft": True, "url": "https://github.com/x/y/pull/7"}]
                return "https://github.com/x/y/pull/7\n"
            return ""
        item = {"node": "inventory", "outcome": "more_work", "actor": "agent:a", "note": "read the files", "inputs": {}}
        said = board_graph.keep_pull_request(graph, state, item, self.pc, "work", github=fake_gh)
        self.assertEqual(said, "pull request 7 opened as a draft for work: https://github.com/x/y/pull/7")
        self.assertEqual([c[:2] for c in calls], [("pr", "list"), ("pr", "create")])
        self.assertEqual(calls[1][2:9], ("--draft", "--base", "main", "--head", "work", "--title", "Inventory: work"))
        listed[0][0]["isDraft"] = True
        del calls[:]
        said = board_graph.keep_pull_request(graph, state, dict(item, outcome="pass"), self.pc, "work", github=fake_gh)
        self.assertEqual(said, "pull request 7 describes the branch as of this record")  # a pass alone: still a draft
        self.assertEqual([c[:3] for c in calls], [("pr", "list", "--head"), ("pr", "edit", "7")])

        asked = git_in(self.pc, "rev-parse", "HEAD").strip()  # the head the merge ask fetched and stored
        ask = dict(item, outcome="needs_owner", inputs={"pull_request": {"number": 7, "head": asked}})
        del calls[:]
        said = board_graph.keep_pull_request(graph, state, ask, self.pc, "work", github=fake_gh)
        self.assertEqual(said, f"pull request 7 describes the branch as of this record; left a draft, not marked ready: "
                               f"CI's board checks on {asked[:8]}: no result on the graph yet; record the merge ask "
                               f"again once CI has passed to mark it ready")
        self.assertEqual([c[:3] for c in calls], [("pr", "list", "--head"), ("pr", "edit", "7")])

        state.record_checks("2026-11-06T18:05:00+00:00", asked, 41, "pull_request", False, 3,
                            [{"check": "drc", "ok": False}])
        del calls[:]
        said = board_graph.keep_pull_request(graph, state, ask, self.pc, "work", github=fake_gh)
        self.assertIn(f"; left a draft, not marked ready: CI's board checks on {asked[:8]}: FAIL, 3 open; failing: drc; "
                      f"run 41 (pull_request) at ", said)
        self.assertEqual([c[:3] for c in calls], [("pr", "list", "--head"), ("pr", "edit", "7")])

        state.record_checks("2026-11-06T18:10:00+00:00", asked, 42, "pull_request", True, 0, [{"check": "drc", "ok": True}])
        git_in(self.pc, "commit", "-q", "--allow-empty", "-m", "the merge ask's own record")  # new head, no CI on it
        del calls[:]
        said = board_graph.keep_pull_request(graph, state, ask, self.pc, "work", github=fake_gh)
        self.assertEqual(said, "pull request 7 describes the branch as of this record; marked ready for the owner")
        self.assertEqual([c[:3] for c in calls], [("pr", "list", "--head"), ("pr", "edit", "7"), ("pr", "ready", "7")])

        def failing_gh(root, *args):
            raise ValueError("gh pr list: not logged in")
        said = board_graph.keep_pull_request(graph, state, item, self.pc, "work", github=failing_gh)
        self.assertEqual(said, "the pull request step failed (gh pr list: not logged in); the branch is pushed, and the "
                               "next record tries again")

    def test_carry_keeps_the_pull_request_only_off_main(self):
        graph = Graph()
        item = {"at": "2026-11-06T18:00:00Z", "track": "board", "node": "inventory", "outcome": "more_work",
                "target": "inventory", "actor": "agent:a", "note": "read", "evidence": [], "inputs": {}}
        self.record.write_text(json.dumps(item), encoding="utf-8")  # the record carry commits, whole
        state = State(pathlib.Path(self.directory.name) / "state.sqlite3", self.records)
        calls = []
        said = board_graph.carry(graph, state, item, {"how": "ssh"}, root=self.pc,
                                 github=lambda root, *args: calls.append(args) or "[]")
        self.assertTrue(said.endswith("on main and pushed to origin\n"), said)
        self.assertEqual(calls, [])  # main: no pull request
        self.assertEqual(board_graph.carry(graph, state, item, {"how": "file"}, root=self.pc), "")

    def test_no_branch_the_macs_main_and_a_record_outside_the_checkout_commit_nothing(self):
        git_in(self.pc, "checkout", "-q", "--detach")
        self.assertEqual(board_graph.land(self.pc, [self.record], "Record: x"),
                         f"not committed: this checkout is on no branch; tools/board_graph_records/{self.record.name} "
                         "waits in the working tree")
        git_in(self.pc, "checkout", "-q", "main")
        with mock.patch.object(mac, "LOCAL", True):
            self.assertEqual(board_graph.land(self.pc, [self.record], "Record: x"),
                             "not committed: on the Mac's main a record travels by pull request; "
                             f"tools/board_graph_records/{self.record.name} waits in the working tree")
        self.assertEqual(board_graph.land(self.pc, [pathlib.Path(self.directory.name) / "elsewhere.json"], "Record: x"),
                         "not committed: the record is outside this checkout")
        self.assertEqual(git_in(self.pc, "log", "-1", "--format=%s").strip(), "one")
        self.assertIn("?? tools/", git_in(self.pc, "status", "--porcelain"))

    def test_carry_names_the_record_file_and_the_report_and_skips_another_database(self):
        graph = Graph()
        records = pathlib.Path(self.directory.name) / "records"  # the state's own folder, with nothing to import
        state = State(pathlib.Path(self.directory.name) / "s.sqlite3", records)
        item = {"at": "2026-11-06T18:00:00Z", "node": "inventory", "outcome": "more_work", "actor": "agent:pc",
                "note": "  many   words\nhere  "}
        seen = []
        with mock.patch.object(board_graph, "land", lambda root, paths, message: seen.append((root, paths, message)) or "done"):
            self.assertEqual(board_graph.carry(graph, state, item, {"how": "ssh"}, ["board/reports/board-graph/r.json"],
                                               root=self.pc), "done\n")
            self.assertEqual(board_graph.carry(graph, state, item, {"how": "--state"}, root=self.pc), "")
        (root, paths, message), = seen
        self.assertEqual((root, paths), (self.pc.resolve(), [records / "2026-11-06T18-00-00Z-inventory-more_work.json",
                                                           self.pc.resolve() / "board/reports/board-graph/r.json"]))
        self.assertEqual(message, "Record: Inventory, more_work by agent:pc: many words here")


class ReportPublicationTests(unittest.TestCase):
    """The public record command publishes its reports to a local bare origin, with a stand-in Mac database."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.directory.name).resolve()
        (self.base / "gitconfig").write_text("", encoding="utf-8")
        self.env = git_settings(self.base)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.directory.cleanup)
        self.origin, self.pc = self.base / "origin.git", self.base / "pc"
        git_in(self.base, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git_in(self.base, "init", "-q", "-b", "main", str(self.pc))
        git_in(self.pc, "remote", "add", "origin", str(self.origin))
        git_in(self.pc, "config", "core.autocrlf", "false")
        shutil.copyfile(board_graph.ROOT / ".gitattributes", self.pc / ".gitattributes")
        self.graph_path = self.pc / "tools/board_graph.json"
        self.graph_path.parent.mkdir(parents=True)
        shutil.copyfile(board_graph.GRAPH, self.graph_path)
        write(self.pc, {"board/reports/automation/old.md": "old report\n",
                        "board/demo.kicad_sch": "original design\n",
                        "staged.txt": "original staged\n", "unstaged.txt": "original unstaged\n"})
        git_in(self.pc, "add", "-A")
        git_in(self.pc, "commit", "-q", "-m", "fixture")
        git_in(self.pc, "push", "-q", "-u", "origin", "main")
        self.records = self.pc / "tools/board_graph_records"
        self.mac_records = self.base / "mac-records"
        self.mac_records.mkdir()
        self.mac_place = board_graph.place(self.base / "mac.sqlite3", self.mac_records, root=self.pc, environ={})
        patchers = (mock.patch.object(mac, "LOCAL", False),
                    mock.patch.object(board_graph, "remote_call", self.fake_ssh),
                    mock.patch.dict(os.environ))
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in (board_graph.STATE_ENV, board_graph_codex.ATTEMPT_ENV, board_graph_codex.RECORDS_ENV):
            os.environ.pop(name, None)

    def fake_ssh(self, request):
        return board_graph.serve(json.loads(request), self.mac_place, self.mac_records, self.graph_path)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = board_graph.main(["--graph", str(self.graph_path), "--records", str(self.records), *args])
        return code, out.getvalue(), err.getvalue()

    def test_public_record_pushes_cited_reports_and_preserves_other_changes(self):
        new = "board/reports/automation/new[proof].json"
        old = "board/reports/automation/old.md"
        neighbor = "board/reports/automation/newp.json"  # would match the report as a Git pathspec glob
        source = "board/reports/automation/reproducer.py"
        folder = "board/reports/other"
        design = "board/demo.kicad_sch"
        write(self.pc, {new: '{"proof":true}\n', old: "updated report\n", neighbor: "unrelated\n",
                        source: "print('source')\n", folder + "/inside.md": "not cited individually\n",
                        design: "edited design\n", "staged.txt": "edited staged\n",
                        "unstaged.txt": "edited unstaged\n"})
        git_in(self.pc, "add", "--", neighbor, design, "staged.txt")
        staged = git_in(self.pc, "diff", "--cached", "--binary")
        unstaged = git_in(self.pc, "diff", "--binary", "--", "unstaged.txt")
        code, out, err = self.cli("record", "inventory", "more_work", "--by", "agent:pc", "--note", "proof",
                                  "--evidence", new, old, source, folder, design)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("and pushed to origin", out)
        [record_path] = list(self.records.glob("*.json"))
        record_name = record_path.relative_to(self.pc).as_posix()
        self.assertEqual(sorted(git_in(self.pc, "show", "--format=", "--name-only", "HEAD").splitlines()),
                         sorted([new, old, record_name]))
        self.assertEqual(git_in(self.pc, "rev-parse", "HEAD").strip(),
                         git_in(self.origin, "rev-parse", "refs/heads/main").strip())
        record_data = json.loads(git_in(self.pc, "show", f"HEAD:{record_name}"))
        for name in (new, old):
            evidence = next(entry for entry in record_data["evidence"] if entry["path"] == name)
            self.assertEqual(evidence["sha256"], hashlib.sha256(git_in(self.pc, "show", f"HEAD:{name}").encode()).hexdigest())
        self.assertEqual(git_in(self.pc, "diff", "--cached", "--binary"), staged)
        self.assertEqual(git_in(self.pc, "diff", "--binary", "--", "unstaged.txt"), unstaged)
        status = git_in(self.pc, "status", "--porcelain", "--untracked-files=all")
        self.assertIn("?? " + source, status)
        self.assertIn("?? " + folder + "/inside.md", status)

    def test_live_record_hashes_normalized_text_and_preserves_binary_and_source(self):
        report = "board/reports/automation/crlf.json"
        picture = "board/reports/automation/proof.png"
        binary_report = "board/reports/automation/binary.json"
        export = "board/reports/automation/assembly.csv"
        source = "board/reports/automation/reproducer.py"
        design = "board/demo.kicad_sch"
        contents = {report: b'{\r\n  "proof": true\r\n}\r\n', picture: b"\x89PNG\r\n\x1a\n\0binary\r\n",
                    binary_report: b"\x00binary\r\n", export: b"Ref,Value\r\nJ1,Jack\r\n",
                    source: b"print('source')\r\n", design: b"design\r\n"}
        for name, data in contents.items():
            path = self.pc / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        attributes = self.pc / "board/reports/automation/.gitattributes"
        attributes.write_text("assembly.csv -text !eol\n", encoding="utf-8")
        git_in(self.pc, "add", "--", str(attributes))
        git_in(self.pc, "commit", "-qm", "Preserve generated assembly file bytes")
        attr = git_in(self.pc, "check-attr", "text", "eol", "--", report)
        self.assertIn("text: auto", attr)
        self.assertIn("eol: lf", attr)
        code, out, err = self.cli("record", "inventory", "more_work", "--by", "agent:pc", "--note", "CRLF proof",
                                  "--evidence", report, picture, binary_report, export, source, design)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("and pushed to origin", out)
        [record_path] = list(self.records.glob("*.json"))
        data = json.loads(record_path.read_text(encoding="utf-8"))
        for name in (report, picture, binary_report, export):
            blob = board_graph.run_git(self.pc, "show", f"HEAD:{name}")
            entry = next(e for e in data["evidence"] if e["path"] == name)
            self.assertEqual(entry["sha256"], hashlib.sha256(blob).hexdigest())
            self.assertEqual((self.pc / name).read_bytes(), blob)
        self.assertEqual((self.pc / report).read_bytes(), contents[report].replace(b"\r\n", b"\n"))
        for name in (picture, binary_report, export, source, design):
            self.assertEqual((self.pc / name).read_bytes(), contents[name])

    def test_branch_record_keeps_pr_and_does_not_publish_pr_head_evidence(self):
        head_report = "board/reports/review/pr.md"
        git_in(self.pc, "checkout", "-q", "-b", "review")
        write(self.pc, {head_report: "review head evidence\n"})
        git_in(self.pc, "add", "--", head_report)
        git_in(self.pc, "commit", "-q", "-m", "review")
        head = git_in(self.pc, "rev-parse", "HEAD").strip()
        git_in(self.pc, "push", "-q", "origin", "HEAD:refs/pull/84/head")
        git_in(self.pc, "checkout", "-q", "-b", "work", "main")
        report = "board/reports/automation/local.md"
        write(self.pc, {report: "local evidence\n"})
        # A local file appearing after the hash must not replace evidence recorded at the PR head.
        original_recorded = board_graph.recorded
        def appear_after_record(graph, state, item):
            said = original_recorded(graph, state, item)
            write(self.pc, {head_report: "unrelated local contents\n"})
            return said
        with mock.patch.object(board_graph, "pull_request_view", return_value={"state": "OPEN", "headRefOid": head}), \
                mock.patch.object(board_graph, "recorded", side_effect=appear_after_record), \
                mock.patch.object(board_graph, "keep_pull_request", return_value="pull request kept") as keep:
            code, out, err = self.cli("record", "inventory", "needs_owner", "--by", "agent:pc", "--note", "merge",
                                      "--pull-request", "84", "--then", "pass", "--evidence", report, head_report)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("on work and pushed to origin\npull request kept", out)
        self.assertEqual(keep.call_args.args[4], "work")
        [record_path] = list(self.records.glob("*.json"))
        record_name = record_path.relative_to(self.pc).as_posix()
        self.assertEqual(sorted(git_in(self.pc, "show", "--format=", "--name-only", "HEAD").splitlines()),
                         sorted([report, record_name]))
        entry = next(e for e in json.loads(record_path.read_text(encoding="utf-8"))["evidence"] if e["path"] == head_report)
        self.assertEqual((entry["at"], entry["sha256"]), (head, hashlib.sha256(b"review head evidence\n").hexdigest()))
        self.assertIn("?? " + head_report, git_in(self.pc, "status", "--porcelain", "--untracked-files=all"))
        self.assertEqual(git_in(self.pc, "rev-parse", "HEAD").strip(),
                         git_in(self.origin, "rev-parse", "refs/heads/work").strip())

    def test_scratch_record_keeps_report_and_record_out_of_git(self):
        report = "board/reports/automation/scratch.json"
        scratch_bytes = b"{\r\n}\r\n"
        (self.pc / report).parent.mkdir(parents=True, exist_ok=True)
        (self.pc / report).write_bytes(scratch_bytes)
        before = git_in(self.pc, "rev-parse", "HEAD")
        code, out, err = self.cli("--state", str(self.base / "scratch.sqlite3"), "record", "inventory", "more_work",
                                  "--by", "agent:pc", "--note", "scratch", "--evidence", report)
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("committed", out)
        self.assertEqual((self.pc / report).read_bytes(), scratch_bytes)
        self.assertEqual(git_in(self.pc, "rev-parse", "HEAD"), before)
        status = git_in(self.pc, "status", "--porcelain", "--untracked-files=all")
        self.assertIn("?? " + report, status)
        self.assertEqual(len(list(self.records.glob("*.json"))), 1)

    def test_report_links_and_linked_parent_folders_are_not_published(self):
        linked_file = "board/reports/automation/link.json"
        linked_parent = "board/reports/linked"
        report_in_link = linked_parent + "/report.md"
        write(self.pc, {linked_file: "{}\n", report_in_link: "report\n"})
        original = pathlib.Path.is_symlink
        def is_link(path):
            return path in (self.pc / linked_file, self.pc / linked_parent) or original(path)
        with mock.patch.object(pathlib.Path, "is_symlink", autospec=True, side_effect=is_link):
            code, out, err = self.cli("record", "inventory", "more_work", "--by", "agent:pc", "--note", "links",
                                      "--evidence", linked_file, report_in_link)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("and pushed to origin", out)
        [record_path] = list(self.records.glob("*.json"))
        self.assertEqual(git_in(self.pc, "show", "--format=", "--name-only", "HEAD").splitlines(),
                         [record_path.relative_to(self.pc).as_posix()])
        status = git_in(self.pc, "status", "--porcelain", "--untracked-files=all")
        self.assertIn("?? " + linked_file, status)
        self.assertIn("?? " + report_in_link, status)

    def test_real_links_appearing_after_hash_are_not_published(self):
        probe = self.base / "probe-link"
        try:
            probe.symlink_to(self.pc, target_is_directory=True)
            probe.unlink()
        except OSError as error:
            self.skipTest(f"this account cannot create symlinks: {error}")
        linked_file = "board/reports/automation/link.json"
        linked_parent = "board/reports/linked"
        report_in_link = linked_parent + "/report.md"
        write(self.pc, {linked_file: "{}\n", report_in_link: "report\n"})
        original_recorded = board_graph.recorded
        def replace_with_links(graph, state, item):
            said = original_recorded(graph, state, item)
            file = self.pc / linked_file
            file.rename(file.with_name("target.json"))
            file.symlink_to("target.json")
            folder = self.pc / linked_parent
            folder.rename(folder.with_name("actual"))
            folder.symlink_to("actual", target_is_directory=True)
            return said
        with mock.patch.object(board_graph, "recorded", side_effect=replace_with_links):
            code, out, err = self.cli("record", "inventory", "more_work", "--by", "agent:pc", "--note", "links",
                                      "--evidence", linked_file, report_in_link)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("and pushed to origin", out)
        [record_path] = list(self.records.glob("*.json"))
        self.assertEqual(git_in(self.pc, "show", "--format=", "--name-only", "HEAD").splitlines(),
                         [record_path.relative_to(self.pc).as_posix()])


@unittest.skipUnless(os.name == "nt", "the Windows wrapper shadows a different codex.exe")
class CodexExecutableTests(unittest.TestCase):
    def test_official_node_package_native_cli_wins_over_stale_codex_exe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wrapper = root / "codex.cmd"
            wrapper.write_text("fixture", encoding="utf-8")
            package = root / "node_modules/@openai/codex"
            package.mkdir(parents=True)
            (package / "package.json").write_text("{}", encoding="utf-8")
            native_package = package / "node_modules/@openai/codex-win32-x64"
            native = native_package / "vendor/x86_64-pc-windows-msvc/bin/codex.exe"
            native.parent.mkdir(parents=True)
            native.write_text("fixture", encoding="utf-8")
            package_json = native_package / "package.json"
            package_json.write_text("{}", encoding="utf-8")
            invoked = []
            def fake_run(args, **kwargs):
                invoked.append(args[0])
                if args[0] == "node.exe":
                    return subprocess.CompletedProcess(args, 0, json.dumps({"arch": "x64", "path": str(package_json)}).encode(), b"")
                if args[0] == str(native):
                    if args[1:] == ["exec", "--help"]:
                        return subprocess.CompletedProcess(args, 0, b"--approve-for-me\n", b"")
                    return subprocess.CompletedProcess(args, 0, b"codex-cli 0.167.0\n", b"")
                self.fail(f"unexpected executable {args[0]}")
            def which(name):
                return {"codex.cmd": str(wrapper), "node.exe": "node.exe", "codex.exe": "stale.exe"}.get(name)
            with mock.patch.dict(os.environ, {board_graph_codex.CLI_ENV: ""}), \
                    mock.patch.object(board_graph_codex.shutil, "which", side_effect=which), \
                    mock.patch.object(board_graph_codex.subprocess, "run", side_effect=fake_run):
                path, version = board_graph_codex.codex_cli()
            self.assertEqual((path, version), (str(native.resolve()), "codex-cli 0.167.0"))
            self.assertNotIn("stale.exe", invoked)

    def test_rejects_native_cli_without_automatic_review_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            native = pathlib.Path(directory) / "codex.exe"
            native.write_text("fixture", encoding="utf-8")
            replies = [subprocess.CompletedProcess([], 0, b"codex-cli 0.167.0\n", b""),
                       subprocess.CompletedProcess([], 0, b"--sandbox workspace-write\n", b"")]
            with mock.patch.object(board_graph_codex.subprocess, "run", side_effect=replies):
                with self.assertRaisesRegex(ValueError, "does not support --approve-for-me"):
                    board_graph_codex.codex_cli(str(native))


class CodexHandoffTests(unittest.TestCase):
    """The one-card launch path, with the provider and every live side effect replaced."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        self.graph = Graph(FIXTURE_GRAPH)
        self.state = State(self.root / "state.sqlite3", self.root / "records")
        board = self.root / "board/demo.kicad_pcb"
        board.parent.mkdir(parents=True)
        board.write_text("(kicad_pcb (version 20260206) (generator pcbnew))\n", encoding="utf-8")
        (self.root / "review.txt").write_text("review pending\n", encoding="utf-8")
        self.state.append(self.graph, "board", "placement", "pass", "routing_batch", "owner", "approved")
        self.previous = self.state.append(self.graph, "board", "routing_batch", "more_work", "routing_batch",
                                          "agent:old", "review and merge question remain",
                                          evidence=[{"path": "review.txt", "sha256": hash_path(self.root / "review.txt")}],
                                          inputs={"board/demo.kicad_pcb": hash_path(board)}, export=True)
        self.name = self.state.transitions("board")[-1]["record_name"]
        self.calls = []

    def tearDown(self):
        self.directory.cleanup()

    def git(self, root, *args, **kwargs):
        if args[:1] == ("symbolic-ref",):
            return "finishing-branch\n"
        if args[:1] == ("rev-parse",):
            return "a" * 40 + "\n"
        raise AssertionError(args)

    def launch(self, worker, **options):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(board_graph, "git_text", side_effect=self.git):
            code = next_step(self.graph, self.state, root=self.root, actions={}, out=out, err=err,
                             upkeep=lambda *args: True, by="agent:fixture", note="continue the review",
                             codex=True, minutes=options.get("minutes", 60), launcher=worker,
                             state_path=self.state.path)
        return code, out.getvalue(), err.getvalue()

    def test_worker_receives_checkpoint_and_its_more_work_survives_return(self):
        def worker(root, prompt, minutes, run_id, **kwargs):
            [attempt] = self.state.attempts("routing_batch", open_only=True)
            self.assertEqual((run_id, prompt), (attempt["run_id"], attempt["brief"]))
            for value in ("finishing-branch", "a" * 40, self.name, "review and merge question remain",
                          "review.txt sha256", "60 minutes", run_id, "does not detect every kind of loop"):
                self.assertIn(value, prompt)
            record(self.graph, self.state, "routing_batch", "more_work", "agent:fixture",
                   "critic still pending", ("review.txt",), root=self.root, attempt_id=run_id)
            return {"reason": "Codex exited with code 0", "tokens": None, "ok": True}
        code, out, err = self.launch(worker)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("worker's recorded outcome stands", out)
        self.assertEqual(self.state.transitions("board")[-1]["outcome"], "more_work")
        self.assertEqual(self.state.attempts("routing_batch", open_only=True), [])

    def test_crash_and_timeout_record_needs_lead_for_the_attempt(self):
        def crash(*args, **kwargs):
            raise RuntimeError("fixture crash")
        code, _, err = self.launch(crash)
        self.assertEqual(code, 1)
        self.assertIn("fixture crash", err)
        row = self.state.transitions("board")[-1]
        self.assertEqual(row["outcome"], "needs_lead")
        self.assertIn("fixture crash", row["note"])
        self.assertEqual(self.state.attempts("routing_batch", open_only=True), [])
        [stopped] = self.state.launch_blocks(self.graph, "board", "routing_batch")
        board_graph.settle(self.graph, self.state, "routing_batch", "agent:lead", note="verified no process; retry",
                           root=self.root, attempt_id=stopped["run_id"], acknowledge=True, verified_stopped=True)
        record(self.graph, self.state, "routing_batch", "more_work", "agent:lead", "ready to retry", (), root=self.root)
        code, _, err = self.launch(lambda *args, **kwargs: {"reason": "Codex exceeded the 60-minute stop", "tokens": None, "ok": False})
        self.assertEqual(code, 1)
        self.assertIn("exceeded", err)
        self.assertEqual(self.state.transitions("board")[-1]["outcome"], "needs_lead")

    def test_launch_waits_for_the_lead_record_after_an_unrecorded_stop(self):
        self.assertEqual(self.launch(lambda *args, **kwargs: {"reason": "stopped", "tokens": None, "ok": False})[0], 1)
        self.assertEqual(self.state.transitions("board")[-1]["outcome"], "needs_lead")
        code, _, err = self.launch(lambda *args, **kwargs: self.fail("must not launch"))
        self.assertEqual(code, 1)
        self.assertIn("waits on the lead", err)
        self.assertEqual(self.state.attempts("routing_batch", open_only=True), [])
        record(self.graph, self.state, "routing_batch", "more_work", "agent:lead", "inspect before retry", (), root=self.root)
        def worker(root, prompt, minutes, run_id, **kwargs):
            self.assertIn("inspect before retry", prompt)
            record(self.graph, self.state, "routing_batch", "more_work", "agent:fixture", "retried", (),
                   root=self.root, attempt_id=run_id)
            return {"reason": "returned", "tokens": None, "ok": True}
        self.assertEqual(self.launch(worker)[0], 0)

    def test_post_record_acknowledgment_survives_resume_and_later_revised_record(self):
        run_ids = []
        def stopped(root, prompt, minutes, run_id, **kwargs):
            run_ids.append(run_id)
            record(self.graph, self.state, "routing_batch", "more_work", "agent:fixture", "critic pending", (),
                   root=self.root, attempt_id=run_id)
            return {"reason": "repeat checkpoint", "tokens": None, "ok": False}
        self.assertEqual(self.launch(stopped)[0], 1)
        board_graph.settle(self.graph, self.state, "routing_batch", "agent:lead", note="inspect the actual board first",
                           root=self.root, attempt_id=run_ids[0], acknowledge=True)
        def resumed(root, prompt, minutes, run_id, **kwargs):
            for expected in ("inspect the actual board first", "finishing-branch", "60 minutes"):
                self.assertIn(expected, prompt)
            record(self.graph, self.state, "routing_batch", "more_work", "agent:fixture", "resume pending", (),
                   root=self.root, attempt_id=run_id)
            return {"reason": "returned", "tokens": None, "ok": True}
        self.assertEqual(self.launch(resumed)[0], 0)  # acknowledgment alone is enough
        next_attempt, _, _ = self.state.open_work(self.graph, "routing_batch", "agent:fixture", "check", {}, str(self.root))
        record(self.graph, self.state, "routing_batch", "needs_lead", "agent:fixture", "pictures need attention", (),
               root=self.root, attempt_id=next_attempt["run_id"])
        record(self.graph, self.state, "routing_batch", "more_work", "agent:lead", "check the saved pictures too",
               (), root=self.root)
        def after_lead_record(root, prompt, minutes, run_id, **kwargs):
            self.assertIn("check the saved pictures too", prompt)
            return resumed(root, prompt, minutes, run_id, **kwargs)
        self.assertEqual(self.launch(after_lead_record)[0], 0)

    def test_acknowledged_approach_follows_pass_to_the_next_ordered_card(self):
        self.graph.nodes["routing_batch"]["requires"] = []
        self.graph.data["open_issues"] = [issue for issue in self.graph.data["open_issues"]
                                          if "routing_batch" not in issue["close_before"]]
        self.graph.nodes["board_checks"]["who"] = "agent"
        self.graph.nodes["board_checks"]["action"] = None
        run_ids = []
        def stopped(root, prompt, minutes, run_id, **kwargs):
            run_ids.append(run_id)
            record(self.graph, self.state, "routing_batch", "pass", "agent:fixture", "passed", ("review.txt",),
                   root=self.root, attempt_id=run_id)
            return {"reason": "stopped after pass", "tokens": None, "ok": False}
        self.assertEqual(self.launch(stopped)[0], 1)
        self.assertEqual(self.state.position(self.graph, "board"), "board_checks")
        board_graph.settle(self.graph, self.state, "routing_batch", "agent:lead", note="inspect the downstream report",
                           root=self.root, attempt_id=run_ids[0], acknowledge=True)
        preview = board_graph.brief(self.graph, self.state, "board_checks", root=self.root)
        self.assertIn("inspect the downstream report", preview)
        def next_card(root, prompt, minutes, run_id, **kwargs):
            self.assertIn("inspect the downstream report", prompt)
            record(self.graph, self.state, "board_checks", "more_work", "agent:fixture", "review continues", (),
                   root=self.root, attempt_id=run_id)
            return {"reason": "returned", "tokens": None, "ok": True}
        self.assertEqual(self.launch(next_card)[0], 0)

    def assert_unconfirmed_stop_needs_verified_acknowledgment(self, after_record):
        def worker(root, prompt, minutes, run_id, **kwargs):
            if after_record:
                record(self.graph, self.state, "routing_batch", "more_work", "agent:fixture", "saved first", (),
                       root=self.root, attempt_id=run_id)
            return {"reason": "could not confirm child termination", "tokens": None, "ok": False,
                    "termination_unconfirmed": True}
        self.assertEqual(self.launch(worker)[0], 1)
        [stopped] = self.state.launch_blocks(self.graph, "board", "routing_batch")
        self.assertTrue(stopped["report"]["termination_unconfirmed"])
        self.assertIn("--verified-stopped", render(where(self.graph, self.state)))
        with self.assertRaisesRegex(ValueError, "--verified-stopped"):
            board_graph.settle(self.graph, self.state, "routing_batch", "agent:lead", note="revised",
                               root=self.root, attempt_id=stopped["run_id"], acknowledge=True)
        with self.assertRaisesRegex(ValueError, "still owns"):
            self.state.open_work(self.graph, "routing_batch", "agent:next", "", {}, str(self.root))
        board_graph.settle(self.graph, self.state, "routing_batch", "agent:lead", note="verified and revised",
                           root=self.root, attempt_id=stopped["run_id"], acknowledge=True, verified_stopped=True)
        [attempt] = [item for item in self.state.attempts("routing_batch") if item["run_id"] == stopped["run_id"]]
        self.assertTrue(attempt["report"]["acknowledged"]["verified_stopped"])
        self.assertEqual(self.state.launch_blocks(self.graph, "board", "routing_batch"), [])

    def test_unconfirmed_stop_before_record_needs_verified_acknowledgment(self):
        self.assert_unconfirmed_stop_needs_verified_acknowledgment(False)

    def test_unconfirmed_stop_after_record_needs_verified_acknowledgment(self):
        self.assert_unconfirmed_stop_needs_verified_acknowledgment(True)

    def test_late_return_and_late_record_do_not_close_a_successor(self):
        def worker(root, prompt, minutes, run_id, **kwargs):
            record(self.graph, self.state, "routing_batch", "more_work", "agent:fixture", "paused", (),
                   root=self.root, attempt_id=run_id)
            with self.assertRaisesRegex(ValueError, "still owns"):
                self.state.open_work(self.graph, "routing_batch", "agent:next", "next", {}, str(self.root))
            with self.assertRaisesRegex(ValueError, "no longer open"):
                record(self.graph, self.state, "routing_batch", "more_work", "agent:fixture", "late", (),
                       root=self.root, attempt_id=run_id)
            self.calls.append(run_id)
            return {"reason": "Codex exited with code 0", "tokens": None, "ok": True}
        code, _, err = self.launch(worker)
        self.assertEqual(code, 0, err)
        successor, _, _ = self.state.open_work(self.graph, "routing_batch", "agent:next", "next", {}, str(self.root))
        board_graph.settle(self.graph, self.state, "routing_batch", "agent:late", root=self.root, attempt_id=self.calls[0])
        self.assertEqual([row["run_id"] for row in self.state.attempts("routing_batch", open_only=True)], [successor["run_id"]])
        with self.assertRaisesRegex(ValueError, "name it with --attempt"):
            record(self.graph, self.state, "routing_batch", "more_work", "agent:ssh", "stale", (), root=self.root)
        self.assertEqual(self.state.transitions("board")[-1]["note"], "paused")

    def test_post_record_stop_is_visible_and_requires_acknowledgment(self):
        def worker(root, prompt, minutes, run_id, **kwargs):
            self.calls.append(run_id)
            record(self.graph, self.state, "routing_batch", "more_work", "agent:fixture", "review pending", (),
                   root=self.root, attempt_id=run_id)
            return {"reason": "repeat-failure checkpoint", "tokens": None, "ok": False}
        code, out, err = self.launch(worker)
        self.assertEqual(code, 1)
        self.assertIn("original record stands", err)
        self.assertIn("needs the lead", render(where(self.graph, self.state)))
        self.assertEqual(self.state.transitions("board")[-1]["note"], "review pending")
        with self.assertRaisesRegex(ValueError, "still owns"):
            self.state.open_work(self.graph, "routing_batch", "agent:next", "next", {}, str(self.root))
        with self.assertRaisesRegex(ValueError, "revised approach"):
            board_graph.settle(self.graph, self.state, "routing_batch", "agent:lead", root=self.root,
                               attempt_id=self.calls[0], acknowledge=True)
        board_graph.settle(self.graph, self.state, "routing_batch", "agent:lead", note="inspect the failed command",
                           root=self.root, attempt_id=self.calls[0], acknowledge=True)
        [previous] = [attempt for attempt in self.state.attempts("routing_batch") if attempt["run_id"] == self.calls[0]]
        self.assertEqual(previous["launch_state"], "acknowledged")
        self.assertEqual(previous["report"]["acknowledged"]["note"], "inspect the failed command")
        self.state.open_work(self.graph, "routing_batch", "agent:next", "next", {}, str(self.root))

    def test_recorded_pass_keeps_the_ordered_track_owned_until_process_returns(self):
        self.graph.nodes["routing_batch"]["requires"] = []
        self.graph.data["open_issues"] = [issue for issue in self.graph.data["open_issues"]
                                          if "routing_batch" not in issue["close_before"]]
        def worker(root, prompt, minutes, run_id, **kwargs):
            record(self.graph, self.state, "routing_batch", "pass", "agent:fixture", "reviewed", ("review.txt",),
                   root=self.root, attempt_id=run_id)
            self.assertEqual(self.state.position(self.graph, "board"), "board_checks")
            with self.assertRaisesRegex(ValueError, "still owns"):
                self.state.open_work(self.graph, "board_checks", "agent:other", "", {}, str(self.root))
            with self.assertRaisesRegex(ValueError, "still owns"):
                self.state.start_attempt("board_checks", "script", {}, board_graph.now(), track="board", graph=self.graph)
            self.state.selected_state_path = self.state.path
            blocked_out = io.StringIO()
            code = next_step(self.graph, self.state, root=self.root, actions={}, out=blocked_out, err=io.StringIO(),
                             upkeep=lambda *args: self.fail("blocked next ran upkeep"), by="agent:other")
            self.assertEqual(code, 2)
            for expected in ("--state", str(self.state.path), "--records", str(self.state.records),
                             "settle routing_batch", "--attempt", run_id):
                self.assertIn(expected, blocked_out.getvalue())
            return {"reason": "Codex exited with code 0", "tokens": None, "ok": True}
        code, _, err = self.launch(worker)
        self.assertEqual(code, 0, err)
        successor, _, _ = self.state.open_work(self.graph, "board_checks", "agent:other", "", {}, str(self.root))
        with self.assertRaisesRegex(ValueError, "name it with --attempt"):
            record(self.graph, self.state, "board_checks", "more_work", "agent:ssh", "stale", (), root=self.root)
        record(self.graph, self.state, "board_checks", "more_work", "agent:lead", "reviewed successor", (),
               root=self.root, attempt_id=successor["run_id"])

    def test_dead_launcher_after_record_is_visible_until_lead_acknowledges(self):
        attempt, _, _ = self.state.open_work(self.graph, "routing_batch", "agent:fixture", "work", {}, str(self.root),
                                             launch=True)
        record(self.graph, self.state, "routing_batch", "more_work", "agent:fixture", "recorded", (),
               root=self.root, attempt_id=attempt["run_id"])
        self.assertIn(attempt["run_id"], render(where(self.graph, self.state)))
        _, said = board_graph.settle(self.graph, self.state, "routing_batch", "agent:lead", root=self.root,
                                     attempt_id=attempt["run_id"])
        self.assertIn("stopped after its record", said)
        text = render(where(self.graph, self.state))
        self.assertIn("launcher ended without reporting", text)
        self.assertIn(f"--attempt {attempt['run_id']} --ack", text)
        self.assertIn("--verified-stopped", text)
        with self.assertRaisesRegex(ValueError, "--verified-stopped"):
            board_graph.settle(self.graph, self.state, "routing_batch", "agent:lead", note="inspect and retry",
                               root=self.root, attempt_id=attempt["run_id"], acknowledge=True)
        board_graph.settle(self.graph, self.state, "routing_batch", "agent:lead", note="inspect and retry",
                           root=self.root, attempt_id=attempt["run_id"], acknowledge=True, verified_stopped=True)
        self.assertEqual(self.state.transitions("board")[-1]["note"], "recorded")

    def test_dead_launcher_before_record_keeps_unconfirmed_ownership_until_verified(self):
        attempt, _, _ = self.state.open_work(self.graph, "routing_batch", "agent:fixture", "work", {}, str(self.root),
                                             launch=True)
        item, said = board_graph.settle(self.graph, self.state, "routing_batch", "agent:lead", root=self.root,
                                        attempt_id=attempt["run_id"])
        self.assertEqual(item["outcome"], "needs_lead")
        self.assertIn("--verified-stopped", said)
        with self.assertRaisesRegex(ValueError, "--verified-stopped"):
            board_graph.settle(self.graph, self.state, "routing_batch", "agent:lead", note="retry",
                               root=self.root, attempt_id=attempt["run_id"], acknowledge=True)
        with self.assertRaisesRegex(ValueError, "still owns"):
            self.state.open_work(self.graph, "routing_batch", "agent:next", "work", {}, str(self.root))
        board_graph.settle(self.graph, self.state, "routing_batch", "agent:lead", note="verified; revise",
                           root=self.root, attempt_id=attempt["run_id"], acknowledge=True, verified_stopped=True)
        self.assertEqual(self.state.launch_blocks(self.graph, "board", "routing_batch"), [])

    def test_framework_launch_only_blocks_its_own_card(self):
        self.state.open_work(self.graph, "automation", "agent:one", "", {}, str(self.root), launch=True)
        self.state.open_work(self.graph, "usage", "agent:two", "", {}, str(self.root))
        with self.assertRaisesRegex(ValueError, "still owns"):
            self.state.open_work(self.graph, "automation", "agent:three", "", {}, str(self.root))

    def test_excess_time_refuses_before_start(self):
        code, _, err = self.launch(lambda *args, **kwargs: self.fail("launched"), minutes=121)
        self.assertEqual(code, 1)
        self.assertIn("give --minutes from 1 to 120", err)
        self.assertEqual(self.state.attempts(), [])

    def test_owner_card_and_live_provider_gate_refuse_without_launch(self):
        self.state.append(self.graph, "board", "inventory", "pass", "decisions", "agent:test", "seeded")
        code, _, err = self.launch(lambda *args, **kwargs: self.fail("launched"))
        self.assertEqual(code, 1)
        self.assertEqual(self.state.attempts(), [])
        self.assertNotIn("fixture crash", err)
        out = io.StringIO()
        code = next_step(self.graph, self.state, root=self.root, actions={}, out=out, err=io.StringIO(),
                         upkeep=lambda *args: True, by="agent:fixture", note="try", codex=True, minutes=2)
        self.assertEqual(code, 1)
        self.assertIn("no configured launcher", out.getvalue())

    def test_action_card_refuses_codex_whether_action_is_connected_or_not(self):
        self.graph.nodes["routing_batch"]["action"] = "fixture_action"
        for actions in ({}, {"fixture_action": (lambda *args: self.fail("action ran"), None)}):
            out, err = io.StringIO(), io.StringIO()
            code = next_step(self.graph, self.state, root=self.root, actions=actions, out=out, err=err,
                             upkeep=lambda *args: True, by="agent:fixture", note="bounded", codex=True,
                             minutes=2, launcher=lambda *args, **kwargs: self.fail("worker launched"))
            self.assertEqual(code, 1)
            self.assertIn("without an action", out.getvalue())
            self.assertEqual(self.state.attempts(), [])

    def test_cli_passes_selected_graph_state_records_and_attempt_to_worker(self):
        graph_path = self.root / "tools/board_graph.json"
        graph_path.parent.mkdir(parents=True)
        graph_path.write_text(json.dumps(self.graph.data), encoding="utf-8")
        seen = []
        def worker(root, prompt, minutes, run_id, **kwargs):
            seen.append((root, kwargs, run_id, prompt))
            self.assertEqual(board_graph.main(["--graph", str(graph_path), "--state", str(self.state.path),
                             "--records", str(self.state.records), "record", "routing_batch", "more_work",
                             "--attempt", run_id, "--by", "agent:fixture", "--note", "CLI fixture"]), 0)
            return {"reason": "fixture returned", "ok": True, "tokens": None}
        argv = ["--graph", str(graph_path), "--state", str(self.state.path), "--records", str(self.state.records),
                "next", "--codex", "--minutes", "60", "--by", "agent:fixture", "--note", "finish review"]
        simulated_os = mock.Mock(wraps=os)
        simulated_os.name, simulated_os.environ = "nt", os.environ
        with mock.patch.object(board_graph, "os", simulated_os), \
                mock.patch.object(board_graph, "git_text", side_effect=self.git), \
                mock.patch.object(board_graph, "bring_current", return_value=True), \
                mock.patch.object(board_graph_codex, "codex_cli", return_value=("fixture-cli", "codex-cli 0.167.0")), \
                mock.patch.object(board_graph_codex, "run", side_effect=worker), \
                mock.patch.object(board_graph, "carry", return_value=""), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(board_graph.main(argv), 0)
        [(root, options, run_id, prompt)] = seen
        self.assertEqual(pathlib.Path(root), self.root.resolve())
        self.assertEqual(pathlib.Path(options["state_path"]), self.state.path)
        self.assertEqual(pathlib.Path(options["records_path"]), self.state.records)
        self.assertEqual(pathlib.Path(options["graph_path"]), graph_path)
        self.assertEqual(options["executable"], "fixture-cli")
        for value in (str(self.state.path), str(self.state.records), str(graph_path), run_id, "Do not assume"):
            self.assertIn(value, prompt)
        close = next(line for line in prompt.splitlines() if line.startswith("  Close it: "))
        for value in ("--graph", str(graph_path), "--state", str(self.state.path), "--records",
                      str(self.state.records), "record routing_batch", "--attempt", run_id):
            self.assertIn(value, close)
        self.assertNotIn(";", close)

    def test_non_windows_launch_refuses_before_opening_an_attempt(self):
        graph_path = self.root / "tools/board_graph.json"
        graph_path.parent.mkdir(parents=True)
        graph_path.write_text(json.dumps(self.graph.data), encoding="utf-8")
        simulated_os = mock.Mock(wraps=os)
        simulated_os.name, simulated_os.environ = "posix", os.environ
        error = io.StringIO()
        with mock.patch.object(board_graph, "os", simulated_os), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(error):
            code = board_graph.main(["--graph", str(graph_path), "--state", str(self.state.path),
                                     "--records", str(self.state.records), "next", "--codex", "--minutes", "60"])
        self.assertEqual(code, 1)
        self.assertIn("verified on Windows only", error.getvalue())
        self.assertEqual(self.state.attempts(), [])

    @unittest.skipUnless(os.name == "nt", "automatic launch is verified on Windows")
    def test_unsupported_cli_refuses_before_opening_an_attempt(self):
        graph_path = self.root / "tools/board_graph.json"
        graph_path.parent.mkdir(parents=True)
        graph_path.write_text(json.dumps(self.graph.data), encoding="utf-8")
        argv = ["--graph", str(graph_path), "--state", str(self.state.path), "--records", str(self.state.records),
                "next", "--codex", "--minutes", "60", "--by", "agent:fixture", "--note", "finish review"]
        error = io.StringIO()
        with mock.patch.object(board_graph_codex, "codex_cli", side_effect=ValueError("missing --approve-for-me")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(error):
            self.assertEqual(board_graph.main(argv), 1)
        self.assertIn("no attempt was opened", error.getvalue())
        self.assertEqual(self.state.attempts(), [])


class StaleMacCacheTests(unittest.TestCase):
    """carried_workspace, render_workspace and quick_snapshot freshness against live keep-pace, upkeep and age."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.dir.name)
        self.graph = Graph(FIXTURE_GRAPH)
        self.state = State(self.root / "logs" / "board-graph" / "state.sqlite3", self.root / "records")
        self.one_machine_patch = mock.patch.object(mac, "ONE_MACHINE", False)
        self.one_machine_patch.start()
        self.addCleanup(self.one_machine_patch.stop)
        self.addCleanup(self.dir.cleanup)

    def _base_full(self, at="2026-11-21T06:30:00+00:00", behind=3, keep_pace=None):
        mac_view = {
            "reached": True, "branch": "main", "head": "old1234", "behind": behind, "ahead": 0,
            "tidy_pending": [], "notes": [],
        }
        if keep_pace is not None:
            mac_view["keep_pace"] = keep_pace
        return {
            "at": at, "fetch_error": None, "here": {"checkouts": [], "tidy_pending": []},
            "mac": mac_view, "merge_questions": [], "last_upkeep": None,
        }

    def test_keep_pace_timestamp_tiebreak_and_fresher_full_read_protection(self):
        full_kp = {"at": "2026-11-21T06:35:00Z", "by": "push", "result": "held", "said": "held",
                   "head": "old1234", "branch": "main", "behind": 2, "ahead": 0, "pending": []}
        prev = self._base_full(at="2026-11-21T06:35:00.400000+00:00", behind=2, keep_pace=full_kp)

        # Older status.json does not overwrite a fresher full read.
        self.state.mac_keep_pace = dict(full_kp, at="2026-11-21T06:34:59Z", behind=0, head="new9999", result="level")
        section = board_graph.carried_workspace(self.graph, self.state, prev)
        self.assertEqual((section["mac"]["head"], section["mac"]["behind"]), ("old1234", 2))
        self.assertFalse(section["mac"].get("from_keep_pace"))

        # Identical status.json in the same whole second preserves the full read (including tidy_pending / page).
        self.state.mac_keep_pace = dict(full_kp)
        section = board_graph.carried_workspace(self.graph, self.state, prev)
        self.assertEqual((section["mac"]["head"], section["mac"]["behind"]), ("old1234", 2))
        self.assertFalse(section["mac"].get("from_keep_pace"))

        # Different status.json updated within the same whole second supersedes the full read.
        self.state.mac_keep_pace = dict(full_kp, by="tidy", result="level", said="level now",
                                        head="983061a5", behind=0)
        section = board_graph.carried_workspace(self.graph, self.state, prev)
        self.assertTrue(section["mac"].get("from_keep_pace"))
        self.assertEqual((section["mac"]["head"], section["mac"]["behind"]), ("983061a5", 0))

    def test_broken_keep_pace_status_renders_error_safely(self):
        prev = self._base_full(at="2026-11-20T00:00:00+00:00", behind=4)
        self.state.mac_keep_pace = {
            "at": "2026-11-21T06:40:00Z", "by": "tidy", "result": "failed",
            "said": "ssh timed out", "head": "old1234", "branch": "main",
            "behind": None, "ahead": None, "pending": [],
        }
        with mock.patch.object(board_graph, "now", return_value="2026-11-21T06:45:00+00:00"):
            section = board_graph.carried_workspace(self.graph, self.state, prev)
        text = "\n".join(board_graph.render_workspace(section))
        self.assertIn("  Mac: reached, but ssh timed out (as of 2026-11-21T06:40:00Z, keep-pace)", text)
        self.assertIn("  Mac, keep-pace: last run 2026-11-21T06:40:00Z by tidy (5 min before this read): failed, "
                      "ssh timed out", text)
        self.assertNotIn("4 commit(s) behind", text)

    def test_upkeep_sync_without_status_json_hides_stale_behind_count(self):
        prev = self._base_full(at="2026-11-21T06:00:00+00:00", behind=5)
        run_id = self.state.start_upkeep("inventory", "starting it", "2026-11-21T06:05:00+00:00")
        self.state.finish_upkeep(run_id, "2026-11-21T06:05:10+00:00", "current", "ok\n", None, [])
        with mock.patch.object(board_graph, "now", return_value="2026-11-21T06:10:00+00:00"):
            section = board_graph.carried_workspace(self.graph, self.state, prev)
        text = "\n".join(board_graph.render_workspace(section))
        self.assertIn("  Mac: synced during upkeep at 2026-11-21T06:05:10Z (cached full read from "
                      "2026-11-21T06:00:00Z omitted; run `where --full` to check now)", text)
        self.assertNotIn("5 commit(s) behind", text)

    def test_stale_full_read_over_one_hour_omits_stale_behind_count(self):
        prev = self._base_full(at="2026-11-20T00:00:00+00:00", behind=9)
        with mock.patch.object(board_graph, "now", return_value="2026-11-20T02:00:00+00:00"):
            section = board_graph.carried_workspace(self.graph, self.state, prev)
        text = "\n".join(board_graph.render_workspace(section))
        self.assertIn("  Mac: cached status from 2 h ago (2026-11-20T00:00:00Z) is stale; run `where --full` "
                      "to check the Mac now", text)
        self.assertNotIn("9 commit(s) behind", text)
        self.assertNotIn("old1234", text)

    def test_quick_snapshot_reads_live_keep_pace_from_logs_dir(self):
        import board_graph_view
        self.state.keep_full_read("pc", self._base_full(at="2026-11-10T01:00:00+00:00", behind=6))
        kp_dir = self.root / "logs" / "mac-keep-pace"
        kp_dir.mkdir(parents=True, exist_ok=True)
        kp_status = {
            "at": "2026-11-21T06:32:50Z", "by": "tidy", "result": "level",
            "said": "main is level with GitHub's main", "head": "983061a5",
            "branch": "main", "behind": 0, "ahead": 0, "pending": [],
        }
        (kp_dir / "status.json").write_text(json.dumps(kp_status), encoding="utf-8")
        out_dir = self.root / "view"
        out_dir.mkdir()
        opened = board_graph.open_state(
            {"path": self.state.path, "how": "--state", "imports": self.state.records, "view": None},
            self.state.records,
        )
        view, view_file = board_graph_view.quick_snapshot(self.graph, opened, out_dir, self.root)
        written = json.loads(view_file.read_text(encoding="utf-8"))
        self.assertEqual(written["workspace"]["mac"]["head"], "983061a5")
        self.assertEqual(written["workspace"]["mac"]["behind"], 0)
        self.assertEqual(written["workspace"]["mac"]["tidy_pending"], [])
        self.assertTrue(view["workspace"]["mac"]["from_keep_pace"])


if __name__ == "__main__":
    unittest.main()
