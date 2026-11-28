#!/usr/bin/env python3
"""The board graph view's snapshot: the log and the workspace beside the graph, with no network and no Mac."""
import base64
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
# Fixture machine settings, read at import: the KiCad machine's database is named consistently across tests.
os.environ["BOARD_GRAPH_SITE"] = str(pathlib.Path(__file__).resolve().parent / "fixtures" / "board_graph_site.json")
# The tests read every checkout as a repository with one board: a project's own list of boards (scripts/boards.py)
# is not read here. The file named is not there.
os.environ["BOARD_GRAPH_BOARDS"] = str(pathlib.Path(__file__).resolve().parent / "fixtures" / "no_boards.json")
import board_graph  # noqa: E402
import board_graph_view  # noqa: E402
import mac  # noqa: E402
import tidy  # noqa: E402
from board_graph import Graph, State, follow, kept_report, naming_record, tied_pull_requests  # noqa: E402
from board_graph_view import log, read_copy, snapshot, workspace_section  # noqa: E402

UNREACHED = {"at": "2026-11-05T18:00:00+00:00", "fetch_error": None,
             "here": {"checkouts": [{"path": "/pc", "branch": "main", "this": True, "head": "abc1234", "behind": 0,
                                     "ahead": 0, "dirty_owned": []}], "tidy_pending": []},
             "mac": {"reached": False, "error": "ssh failed: no route to host"}, "merge_questions": [],
             "last_upkeep": None}


def refuse(*args, **kwargs):
    raise AssertionError("a test reached for the real workspace or GitHub")


board_graph_view.workspace = refuse
board_graph.open_pull_requests = refuse
board_graph.remote_call = refuse  # the SSH round trip to the Mac's database
board_graph.mac_checkout = refuse  # on the Mac, the live database: every test names its own


class Base(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.directory.name)
        self.graph = Graph()
        self.state = State(self.base / "state.sqlite3", self.base / "records")

    def tearDown(self):
        self.directory.cleanup()

    def hand(self, node, outcome, at, note="seeded", inputs=None, target=None):
        target = target or (self.graph.nodes[node]["outcomes"]["pass"] if outcome == "pass" else node)
        return self.state.append(self.graph, "board", node, outcome, target, "agent:test", note, inputs=inputs,
                                 at=at, export=True)


class LogTests(Base):
    def test_post_record_checkpoint_keeps_position_and_is_visible_in_work_log(self):
        inputs = board_graph.approval_inputs(self.graph, self.state, "inventory", self.base)
        attempt, _, _ = self.state.open_work(self.graph, "inventory", "agent:fixture", "review", inputs, str(self.base),
                                             launch=True)
        (self.base / "evidence.txt").write_text("review\n", encoding="utf-8")
        board_graph.record(self.graph, self.state, "inventory", "pass", "agent:fixture", "recorded",
                           ("evidence.txt",), root=self.base, attempt_id=attempt["run_id"])
        self.state.finish_launch(attempt["run_id"], "repeat checkpoint", True)
        self.assertEqual(self.state.position(self.graph, "board"), "decisions")
        row = next(row for row in log(self.state, self.base)["rows"] if row["kind"] == "work")
        self.assertEqual((row["outcome"], row["launch_state"]), ("pass", "needs_lead"))
        self.assertIn("repeat checkpoint", row["note"])
        self.assertIn(f"--attempt {attempt['run_id']} --ack", row["note"])

    def test_rows_newest_first_with_the_record_a_script_run_produced(self):
        self.hand("inventory", "pass", "2026-11-05T10:00:00+00:00")
        upkeep = self.state.start_upkeep("decisions", "before decisions_check", "2026-11-05T11:00:00+00:00")
        self.state.finish_upkeep(upkeep, "2026-11-05T11:00:05+00:00", "current", "main is up to date (/pc)\n",
                                 "the Mac did not answer", [])
        run = self.state.start_attempt("decisions", "decisions_check", {}, "2026-11-05T11:00:06+00:00")
        report = "board/reports/board-graph/20261105T110006Z-decisions-abcd1234.json"
        (self.base / report).parent.mkdir(parents=True)
        (self.base / report).write_text(json.dumps({"findings": ["one", "two"], "outcome": "needs_lead"}),
                                        encoding="utf-8")
        self.state.finish_attempt(run, "2026-11-05T11:00:09+00:00", "needs_lead", report, "f" * 64,
                                  kept_report({"findings": ["one", "two"], "outcome": "needs_lead"}))
        made = self.state.append(self.graph, "board", "decisions", "needs_lead", "decisions", "script:decisions_check",
                                 "2 findings", evidence=[{"path": report, "sha256": "f" * 64}],
                                 at="2026-11-05T11:00:09.500000+00:00", export=True)
        rows = log(self.state, self.base)
        self.assertEqual([(row["kind"], row["node"]) for row in rows["rows"]],
                         [("attempt", "decisions"), ("upkeep", "decisions"), ("record", "inventory")])
        attempt, tidy_run, by_hand = rows["rows"]
        name = sorted((self.base / "records").glob("*decisions-needs_lead.json"))[0].name
        self.assertEqual((attempt["what"], attempt["outcome"], attempt["record"], attempt["actor"]),
                         ("decisions_check", "needs_lead", name, "script:decisions_check"))
        self.assertEqual(attempt["report_read"]["findings"], ["one", "two"])
        self.assertEqual(made["note"], attempt["note"])
        self.assertEqual((tidy_run["outcome"], tidy_run["mac_skipped"], tidy_run["record"]),
                         ("current", "the Mac did not answer", None))
        self.assertIn("main is up to date", tidy_run["said"])
        self.assertEqual((by_hand["outcome"], by_hand["target"], by_hand["actor"]), ("pass", "decisions", "agent:test"))
        self.assertEqual((rows["total"], rows["counts"]), (3, {"attempt": 1, "work": 0, "upkeep": 1, "record": 1}))

    def test_an_unfinished_run_and_a_missing_report(self):
        self.state.start_attempt("inventory", "inventory_check", {}, "2026-11-05T12:00:00+00:00")
        self.state.start_upkeep("inventory", "before inventory_check", "2026-11-05T11:59:59+00:00")
        attempt, tidy_run = log(self.state, self.base)["rows"]
        self.assertEqual((attempt["outcome"], attempt["finished_at"], attempt["report_read"]), (None, None, None))
        self.assertEqual((tidy_run["outcome"], tidy_run["problems"]), (None, []))

    def test_a_reports_parts_come_from_the_row_and_an_older_row_reads_its_file_where_the_run_was(self):
        # The row keeps what the view shows, so a view written on another machine has it without the file.
        report = "board/reports/board-graph/20261106T020000Z-models-abcd1234.json"
        run = self.state.start_attempt("models", "models", {}, "2026-11-06T02:00:00+00:00", checkout="/elsewhere")
        self.state.finish_attempt(run, "2026-11-06T02:00:09+00:00", "needs_lead", report, "f" * 64,
                                  kept_report({"findings": ["a height is missing"]}))
        [row] = log(self.state, self.base)["rows"]
        self.assertEqual(row["report_read"], {"findings": ["a height is missing"], "findings_total": 1})
        last = board_graph_view.node_states(self.graph, self.state, self.base)[1]["models"]["runs"]["last"]
        self.assertEqual(last["report_read"]["findings"], ["a height is missing"])
        # A row from before the parts were kept: its file, in the checkout the run was in, else in root.
        there = self.base / "worktree"
        old = "board/reports/board-graph/20261106T010000Z-models-00000000.json"
        (there / old).parent.mkdir(parents=True)
        (there / old).write_text(json.dumps({"findings": ["an older finding"]}), encoding="utf-8")
        run = self.state.start_attempt("models", "models", {}, "2026-11-06T01:00:00+00:00", checkout=there)
        self.state.finish_attempt(run, "2026-11-06T01:00:09+00:00", "needs_lead", old, "f" * 64)
        rows = {row["report"]: row for row in log(self.state, self.base)["rows"]}
        self.assertEqual(rows[old]["report_read"], {"findings": ["an older finding"], "findings_total": 1})
        self.assertEqual(self.state.attempts()[-1]["checkout"], str(there))
        (there / old).unlink()
        self.assertIsNone(log(self.state, self.base)["rows"][1]["report_read"])

    def test_capped_at_the_limit_with_the_total_kept(self):
        for minute in range(5):
            self.state.start_upkeep("inventory", f"run {minute}", f"2026-11-05T12:0{minute}:00+00:00")
        rows = log(self.state, self.base, limit=3)
        self.assertEqual([row["what"] for row in rows["rows"]], ["run 4", "run 3", "run 2"])
        self.assertEqual((rows["total"], rows["limit"]), (5, 3))


class WhatRunsTests(Base):
    """Each node's panel says what actually runs there, read from the code, and its last run."""

    def nodes(self):
        return board_graph_view.node_states(self.graph, self.state, self.base)[1]

    def test_a_connected_action_names_its_function_the_scripts_it_runs_and_what_it_reads(self):
        import inspect
        import board_graph_actions
        nodes = self.nodes()
        runs = nodes["models"]["runs"]
        self.assertEqual({key: runs[key] for key in ("action", "connected", "later", "function", "source", "line")},
                         {"action": "models", "connected": True, "later": False, "function": "models",
                          "source": "scripts/board_graph_actions.py",
                          "line": inspect.getsourcelines(board_graph_actions.models)[1]})
        self.assertIn("within LIBRARY_HEIGHT_MM (0.3) of the datasheet height", runs["doc"])
        self.assertEqual(runs["uses"], ["scripts/board_bom_export.py", "scripts/board_space.py", "scripts/graph_pack.py", "scripts/kicad_sexp.py"])
        self.assertEqual(runs["reads"], list(board_graph_actions.LIBRARY_READS))
        self.assertNotIn("last", runs)
        # Datasheets uses board_pre_schematic and builds Project without reaching model code.
        self.assertEqual(nodes["datasheets"]["runs"]["uses"], ["scripts/board_bom_export.py", "scripts/board_pre_schematic.py", "scripts/kicad_sexp.py"])
        package = nodes["package"]["runs"]
        if self.graph.nodes["package"].get("pass_check"):
            self.assertEqual(package["when"], "pass")
        self.assertEqual((package["connected"], package["later"], package["function"], package["reads"]),
                         (True, False, "package", list(board_graph_actions.ACTIONS["package"].reads)))
        # An action a pack names with no script yet says so (the electronics pack has none now).
        with mock.patch.object(board_graph_view, "LATER", ("package",)):
            self.assertEqual(board_graph_view.what_runs(self.graph, self.state, "package", actions={}),
                             {"action": "package", "connected": False, "later": True})
        checks = nodes["board_checks"]["runs"]
        self.assertEqual((checks["connected"], checks["later"], checks["function"], checks["reads"]),
                         (True, False, "board_checks", list(board_graph_actions.BOARD_CHECK_READS)))
        self.assertEqual((nodes["footprints"]["runs"]["connected"], nodes["footprints"]["runs"]["function"]),
                         (True, "footprints"))
        self.assertIsNone(nodes["decisions"]["runs"])

    def test_the_last_run_carries_its_findings_and_report_and_work_is_not_a_run(self):
        report = "board/reports/board-graph/20261106T020000Z-models-abcd1234.json"
        (self.base / report).parent.mkdir(parents=True)
        (self.base / report).write_text(json.dumps({"findings": ["J5: model height not measured"]}),
                                        encoding="utf-8")
        run = self.state.start_attempt("models", "models", {}, "2026-11-06T02:00:00+00:00")
        self.state.finish_attempt(run, "2026-11-06T02:00:09+00:00", "needs_lead", report, "f" * 64)
        self.state.start_attempt("models", "agent", {}, "2026-11-06T02:05:00+00:00", actor="agent:lead")
        last = self.nodes()["models"]["runs"]["last"]
        self.assertEqual({key: last[key] for key in ("run_id", "outcome", "by", "report", "finished_at")},
                         {"run_id": run, "outcome": "needs_lead", "by": "script:models", "report": report,
                          "finished_at": "2026-11-06T02:00:09+00:00"})
        self.assertEqual(last["report_read"]["findings"], ["J5: model height not measured"])

    def test_a_stand_in_action_runs_nothing_in_turn(self):
        from board_graph_actions import Action

        def stand_in(root):
            """A test's script."""
        runs = board_graph_view.what_runs(self.graph, self.state, "models", self.base,
                                          actions={"models": Action(stand_in, ("input.txt",))})
        self.assertEqual((runs["function"], runs["uses"], runs["reads"], runs["doc"]),
                         ("stand_in", [], ["input.txt"], "A test's script."))


class WorkspaceSectionTests(Base):
    PULLS = [
        {"number": 90, "title": "Nothing names it", "headRefName": "x", "isDraft": False, "url": "u90",
         "updatedAt": "2026-11-05T09:00:00Z", "statusCheckRollup": []},
        {"number": 85, "title": "Asked", "headRefName": "y", "isDraft": True, "url": "u85",
         "updatedAt": "2026-11-05T09:00:00Z",
         "statusCheckRollup": [{"name": "validate", "status": "COMPLETED", "conclusion": "FAILURE"}]},
        {"number": 82, "title": "Named in a note", "headRefName": "z", "isDraft": True, "url": "u82",
         "updatedAt": "2026-11-05T09:00:00Z", "statusCheckRollup": None},
    ]

    def test_open_pull_requests_tied_to_the_node_whose_record_names_them(self):
        self.hand("inventory", "pass", "2026-11-05T10:00:00+00:00", note="Pull request 82 holds the audit fix")
        asked = self.hand("decisions", "needs_owner", "2026-11-05T11:00:00+00:00", note="merge it?",
                          inputs={"pull_request": {"number": 85, "then": "pass", "head": "a" * 40, "expected": {}}})
        section = workspace_section(self.graph, self.state, self.base, read_workspace=lambda *args: dict(UNREACHED))
        self.assertEqual(section["mac"], {"reached": False, "error": "ssh failed: no route to host"})
        tied = tied_pull_requests(self.state.transitions(), self.PULLS)
        found = {pr["number"]: pr for pr in tied}
        self.assertEqual([pr["number"] for pr in tied], [82, 85, 90])
        self.assertEqual((found[85]["node"], found[85]["named_in"], found[85]["draft"], found[85]["checks"]),
                         ("decisions", "inputs", True, "failing (validate)"))
        self.assertEqual(found[85]["record"], self.state.transitions()[-1]["record_name"])
        self.assertEqual(asked["note"], "merge it?")
        self.assertEqual((found[82]["node"], found[82]["named_in"], found[82]["checks"]), ("inventory", "note", "no checks"))
        self.assertEqual((found[90]["node"], found[90]["record"]), (None, None))

    def test_a_merge_question_outranks_a_newer_note(self):
        self.hand("inventory", "needs_owner", "2026-11-05T10:00:00+00:00",
                  inputs={"pull_request": {"number": 85, "then": "pass", "head": "a" * 40, "expected": {}}})
        self.hand("inventory", "more_work", "2026-11-05T11:00:00+00:00", note="pull request 85 needs one more look")
        row, named_in = naming_record(self.state.transitions(), 85)
        self.assertEqual((row["outcome"], named_in), ("needs_owner", "inputs"))
        self.assertEqual(naming_record(self.state.transitions(), 8), (None, None))

    def test_the_workspace_failing_is_said_not_raised(self):
        def broken(*args):
            raise RuntimeError("git is missing")

        section = workspace_section(self.graph, self.state, self.base, read_workspace=broken)
        self.assertEqual(section["error"], "RuntimeError: git is missing")
        self.assertTrue(section["at"])


class SnapshotTests(Base):
    def test_view_json_and_snapshot_js_carry_graph_workspace_and_log(self):
        out = self.base / "view"
        out.mkdir()
        (out / "index.html").write_bytes(PAGE)
        self.hand("inventory", "pass", "2026-11-05T10:00:00+00:00", note="a </script> in a note")
        view, data, copy = snapshot(state_path=self.base / "state.sqlite3", records=self.base / "records", out=out,
                                    root=self.base, read_workspace=lambda *args: dict(UNREACHED),
                                    list_pull_requests=lambda root: [])
        self.assertEqual(json.loads(data.read_text(encoding="utf-8")), view)
        self.assertEqual(set(view) >= {"graph", "where", "state", "workspace", "log", "private_names"}, True)
        self.assertIsInstance(view["private_names"], list)
        self.assertFalse(view["workspace"]["mac"]["reached"])
        self.assertEqual(view["log"]["rows"][0]["note"], "a </script> in a note")
        self.assertEqual(copy, out / "snapshot.js")
        self.assertNotIn("</script> in a note", copy.read_text(encoding="utf-8"))
        self.assertEqual(read_copy(copy), view)
        self.assertNotIn(b"\r", copy.read_bytes())
        # The page is the tool's and is never written.
        self.assertEqual((out / "index.html").read_bytes(), PAGE)
        # The graph and the positions are read, never written.
        self.assertEqual(len(self.state.transitions()), 1)

    def test_artifacts_discover_schematic_bom_datasheets_and_renders_from_board_description(self):
        fixture_board = pathlib.Path(__file__).resolve().parent / "fixtures" / "board.json"
        desc = json.loads(fixture_board.read_text(encoding="utf-8"))
        (self.base / "board").mkdir()
        (self.base / "board" / "board.json").write_text(json.dumps(desc), encoding="utf-8")
        sch_pdf = self.base / desc["schematic_export"]
        sch_pdf.parent.mkdir(parents=True, exist_ok=True)
        sch_pdf.write_bytes(b"%PDF-1.4\n")
        bom_csv = self.base / desc["bom_export"]
        bom_csv.parent.mkdir(parents=True, exist_ok=True)
        bom_csv.write_text("Refs,Qty,Value,Manufacturer,MPN,Supplier,Supplier Part\nU1,1,HR8833,Heroic,HR8833MTER,LCSC,C128666\n",
                           encoding="utf-8")
        ds_dir = self.base / desc["datasheets"]
        ds_dir.mkdir(parents=True, exist_ok=True)
        (ds_dir / "Heroic-HR8833.pdf").write_bytes(b"%PDF-1.4\n")
        parts_file = self.base / desc["parts_data"]
        parts_file.parent.mkdir(parents=True, exist_ok=True)
        parts_file.write_text(json.dumps({"parts": {"HR8833MTER": {
            "maker": "Heroic", "package": "ETSSOP-16", "datasheet": "Heroic-HR8833.pdf", "height_mm": 1.2,
        }}}), encoding="utf-8")
        place_png = self.base / desc["reports"] / "placement" / "3d_top.png"
        place_png.parent.mkdir(parents=True, exist_ok=True)
        place_png.write_bytes(b"\x89PNG\r\n")

        art = board_graph_view.artifacts(self.graph, self.state, self.base)
        self.assertEqual(art["cards"]["schematic"]["badge"], "PDF ready")
        self.assertTrue(art["cards"]["schematic"]["items"][0]["exists"])
        self.assertEqual(art["cards"]["bom"]["badge"], "1 part")
        self.assertEqual(art["bom"]["total_qty"], 1)
        self.assertEqual(art["cards"]["datasheets"]["badge"], "1 file")
        self.assertEqual(art["datasheets"]["board_parts"][0]["mpn"], "HR8833MTER")
        self.assertTrue(art["datasheets"]["board_parts"][0]["exists"])
        self.assertEqual(art["cards"]["placement"]["badge"], "1 view")


class PrivateNamesTests(unittest.TestCase):
    """The names the page's Hide private takes out come with the view, so the page holds no machine's names."""

    def test_the_settings_this_machine_and_the_workspace_name_them(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            (root / "tools").mkdir()
            site = json.loads((pathlib.Path(board_graph_view.ROOT) / "tools/board_graph_site.json").read_text(
                encoding="utf-8"))
            site["kicad_machine"]["ssh_host"] = "builder@kicad-box.local"
            (root / "tools/board_graph_site.json").write_text(json.dumps(site), encoding="utf-8")
            section = {"read_on": "OFFICE-PC", "here": {"checkouts": [
                {"path": "C:\\Users\\Pat\\project"}, {"path": "/Users/builder/project-x"}, {"path": "/srv/x"}]}}
            with mock.patch.dict(os.environ):
                os.environ.pop("BOARD_GRAPH_SITE", None)
                names = board_graph_view.private_names(section, root, user="pat", host="laptop.lan")
        self.assertEqual(names, ["OFFICE-PC", "Pat", "builder", "kicad-box", "kicad-box.local", "laptop",
                                 "laptop.lan", "pat"])

    def test_no_settings_and_no_workspace_leave_this_machines_names(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ):
            os.environ.pop("BOARD_GRAPH_SITE", None)
            self.assertEqual(board_graph_view.private_names(None, folder, user="pat", host="laptop"), ["laptop", "pat"])
            self.assertEqual(board_graph_view.private_names({"error": "x"}, folder, user="", host=""), [])

    def test_the_page_names_no_machine_and_reads_the_views_names(self):
        page = (pathlib.Path(board_graph_view.ROOT) / "tools/board_graph_view/index.html").read_text(encoding="utf-8")
        self.assertIn("private_names", page)
        for name in ("DESKTOP-", "/Users/", "C:\\Users\\", "kicad-host"):
            self.assertNotIn(name, page)

    def test_the_page_hides_a_home_folders_path_on_every_system_and_says_graph_where_it_is_not_a_board(self):
        page = (pathlib.Path(board_graph_view.ROOT) / "tools/board_graph_view/index.html").read_text(encoding="utf-8")
        # Windows, macOS and Linux home folders: the same three private_names() reads the user's name from.
        self.assertIn("[/[A-Za-z]:\\\\[^\\s\"'<>]+/g, '[path hidden]']", page)
        self.assertIn("[/\\/(?:Users|home)\\/[^\\s\"'<>]+/g, '[path hidden]']", page)
        for words in ("<title>Board graph</title>", 'aria-label="Boards"', 'aria-label="The board graph'):
            self.assertNotIn(words, page)
        self.assertIn("id=\"stages\"", page)

    def test_the_page_has_a_page_per_track(self):
        # ?track=<id> shows one track as the main path cleanly in the flat 2D view.
        page = (pathlib.Path(board_graph_view.ROOT) / "tools/board_graph_view/index.html").read_text(encoding="utf-8")
        self.assertIn("get('track')", page)
        self.assertIn('id="tabs"', page)
        self.assertIn("mainTrackId", page)
        self.assertIn("n.track === mainTrack", page)
        self.assertNotIn("three.module.min.js", page)



PAGE = b"<p>page</p>\n<script src=\"snapshot.js\"></script>\n"
ASKED_AT = "2026-11-05T10:00:00+00:00"
FULL = dict(UNREACHED, mac={"reached": True, "branch": "main", "head": "def5678", "behind": 2, "ahead": 0,
                            "tidy_pending": [], "notes": []},
            pull_requests={"open": [{"number": 85, "title": "Asked", "node": "inventory"}], "error": None},
            merge_questions=[{"track": "board", "node": "inventory", "pull_request": 85, "then": "pass",
                              "asked_at": ASKED_AT, "asked_at_head": "a" * 40, "state": "open", "draft": False,
                              "checks": "passing", "merge_commit": None, "head": "a" * 40, "head_moved": False,
                              "url": "u85"}])
QUESTION = {"pull_request": {"number": 85, "then": "pass", "head": "a" * 40, "expected": {}}}


class MacViewTests(Base):
    """On the Mac, a snapshot from any checkout writes the main checkout's view.json, the folder the page is served
    from, and snapshot.js beside the checkout's own page, which its branch commits; off the Mac, a snapshot
    reaches the Mac's database and writes this checkout's view.json."""

    def test_a_snapshot_on_the_mac_writes_the_served_view_and_its_own_page(self):
        main, side = self.base / "main", self.base / "side"
        for folder in (main, side):
            (folder / "tools/board_graph_view").mkdir(parents=True)
            (folder / "tools/board_graph_view/index.html").write_bytes(PAGE)
        self.hand("inventory", "pass", "2026-11-05T10:00:00+00:00")
        (main / "logs/board-graph").mkdir(parents=True)
        self.state.path.replace(main / "logs/board-graph/state.sqlite3")
        with mock.patch.dict(os.environ), mock.patch.object(board_graph.mac, "LOCAL", True), \
                mock.patch.object(board_graph, "mac_checkout", lambda: main):
            os.environ.pop(board_graph.STATE_ENV, None)
            view, data, page = snapshot(records=self.state.records, root=side,
                                        read_workspace=lambda *args: dict(UNREACHED), list_pull_requests=lambda root: [])
            quick, quick_data, quick_page = snapshot(records=self.state.records, root=side, quick=True)
        self.assertEqual((data, quick_data), (main / "tools/board_graph_view/view.json",) * 2)
        self.assertEqual((page, quick_page), (side / "tools/board_graph_view/snapshot.js", None))
        self.assertEqual((main / "tools/board_graph_view/index.html").read_bytes(), PAGE)
        self.assertEqual((side / "tools/board_graph_view/index.html").read_bytes(), PAGE)
        self.assertFalse((main / "tools/board_graph_view/snapshot.js").exists())
        self.assertEqual(read_copy(page), view)
        self.assertFalse((side / "tools/board_graph_view/view.json").exists())
        self.assertEqual(quick["where"]["tracks"][0]["position"], "decisions")

    def test_a_snapshot_off_the_mac_writes_this_checkouts_view_from_the_copy_or_says_the_mac_did_not_answer(self):
        side = self.base / "side"
        (side / "tools/board_graph_view").mkdir(parents=True)
        (side / "tools/board_graph_view/index.html").write_bytes(PAGE)
        self.hand("inventory", "pass", "2026-11-05T10:00:00+00:00")
        copy = base64.b64encode(self.state.copy()).decode("ascii")
        with mock.patch.dict(os.environ), mock.patch.object(board_graph.mac, "LOCAL", False), \
                mock.patch.object(board_graph, "remote_call", lambda text: {"result": None, "copy": copy}):
            os.environ.pop(board_graph.STATE_ENV, None)
            quick, data, page = snapshot(records=self.state.records, root=side, quick=True)
        self.assertEqual((data, page), (side / "tools/board_graph_view/view.json", None))
        self.assertEqual(quick["where"]["tracks"][0]["position"], "decisions")

        def down(text):
            raise board_graph.mac.MacError("ssh failed: no route to host")

        with mock.patch.dict(os.environ), mock.patch.object(board_graph.mac, "LOCAL", False), \
                mock.patch.object(board_graph, "remote_call", down), redirect_stderr(io.StringIO()) as err:
            os.environ.pop(board_graph.STATE_ENV, None)
            self.assertEqual(board_graph_view.main(["snapshot", "--quick"]), 1)
        self.assertIn("snapshot: the graph's database is on the Mac (builder@kicad-host:~/demo-board/logs/board-graph/"
                      "state.sqlite3) and the Mac did not answer (ssh failed: no route to host); nothing was written",
                      err.getvalue())


class QuickTests(Base):
    """The quick snapshot reads the database alone: no git, no GitHub, no Mac, and never the page."""

    def setUp(self):
        super().setUp()
        self.out = self.base / "view"
        self.out.mkdir()
        (self.out / "index.html").write_bytes(PAGE)
        for patch in (mock.patch.object(board_graph, "run_git", refuse), mock.patch.object(tidy, "git", refuse)):
            patch.start()
            self.addCleanup(patch.stop)

    def view(self):
        return json.loads((self.out / "view.json").read_text(encoding="utf-8"))

    def test_view_json_alone_with_the_workspace_carried_and_the_database_read_now(self):
        self.state.keep_full_read("pc", FULL)
        self.hand("inventory", "needs_owner", ASKED_AT, note="merge it?", inputs=QUESTION)
        run = self.state.start_upkeep("inventory", "before inventory_check", "2026-11-05T11:00:00+00:00")
        self.state.finish_upkeep(run, "2026-11-05T11:00:30+00:00", "current", "main is up to date\n", None, [])
        argv = ["--state", str(self.state.path), "--records", str(self.state.records), "--out", str(self.out),
                "snapshot", "--quick"]
        with redirect_stdout(io.StringIO()) as said:
            self.assertEqual(board_graph_view.main(argv), 0)
        self.assertIn("quick: snapshot.js stays as it is", said.getvalue())
        self.assertEqual((self.out / "index.html").read_bytes(), PAGE)
        self.assertEqual(sorted(path.name for path in self.out.iterdir()), ["index.html", "view.json"])
        view = self.view()
        section = view["workspace"]
        self.assertEqual(section["quick"]["carried_from"], FULL["at"])
        for key in ("at", "fetch_error", "here", "mac", "pull_requests"):
            self.assertEqual(section[key], FULL[key])
        [question] = section["merge_questions"]
        self.assertEqual((question["state"], question["checks"], question["url"]), ("open", "passing", "u85"))
        self.assertEqual((section["last_upkeep"]["outcome"], section["last_upkeep"]["finished_at"]),
                         ("current", "2026-11-05T11:00:30+00:00"))
        self.assertEqual([row["kind"] for row in view["log"]["rows"]], ["upkeep", "record"])
        self.assertEqual(view["state"]["nodes"]["inventory"]["last"]["outcome"], "needs_owner")
        self.assertEqual(view["where"]["tracks"][0]["position"], "inventory")

    def test_a_new_question_has_no_word_from_github_and_no_full_read_carries_nothing(self):
        self.hand("inventory", "needs_owner", "2026-11-05T12:00:00+00:00", inputs=QUESTION)
        view, data, page = snapshot(state_path=self.state.path, records=self.state.records, out=self.out,
                                    root=self.base, quick=True)
        self.assertIsNone(page)
        self.assertEqual(self.view(), view)
        section = view["workspace"]
        self.assertIsNone(section["quick"]["carried_from"])
        self.assertFalse({"at", "here", "mac", "pull_requests"} & set(section))
        [question] = section["merge_questions"]
        self.assertEqual((question["pull_request"], question["asked_at"]), (85, "2026-11-05T12:00:00+00:00"))
        self.assertNotIn("state", question)

    def test_a_record_on_a_temporary_database_refreshes_view_json_in_a_temporary_view_folder(self):
        argv = ["--state", str(self.state.path), "--records", str(self.state.records), "--view", str(self.out)]
        for note in ("first", "second"):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(board_graph.main(argv + ["record", "inventory", "more_work", "--by", "agent:test",
                                                          "--note", note]), 0)
            view = self.view()
            self.assertEqual(view["log"]["rows"][0]["note"], note)
        self.assertEqual([row["note"] for row in view["log"]["rows"]], ["second", "first"])
        self.assertEqual(view["state"]["nodes"]["inventory"]["transitions"], 2)
        self.assertEqual((self.out / "index.html").read_bytes(), PAGE)

    def test_every_write_next_makes_refreshes_the_view(self):
        follow(self.graph, self.state, self.out, self.base)
        run = self.state.start_upkeep("inventory", "before inventory_check", "2026-11-05T11:00:00+00:00")
        self.assertEqual(self.view()["log"]["rows"][0]["outcome"], None)
        self.state.finish_upkeep(run, "2026-11-05T11:00:30+00:00", "current", "said\n", None, [])
        self.assertEqual(self.view()["workspace"]["last_upkeep"]["outcome"], "current")
        attempt = self.state.start_attempt("inventory", "inventory_check", {}, "2026-11-05T11:00:31+00:00")
        self.assertEqual([row["kind"] for row in self.view()["log"]["rows"]], ["attempt", "upkeep"])
        self.assertEqual(self.view()["state"]["nodes"]["inventory"]["attempt"]["by"], "script:inventory_check")
        self.state.finish_attempt(attempt, "2026-11-05T11:00:40+00:00", "needs_lead", None, None)
        self.assertEqual(self.view()["log"]["rows"][0]["outcome"], "needs_lead")
        self.assertIsNone(self.view()["state"]["nodes"]["inventory"]["attempt"])

    def test_each_nodes_open_attempt_is_in_the_view_until_its_record(self):
        argv = ["--state", str(self.state.path), "--records", str(self.state.records), "--view", str(self.out)]
        # Inventory's script is not connected here (no actions), so next starts the card for agent:a and leaves it on.
        with redirect_stdout(io.StringIO()), mock.patch.object(board_graph, "ACTIONS", {}), mock.patch.object(board_graph, "head_here", lambda root: None),                 mock.patch.object(board_graph, "bring_current", lambda *a, **k: True):
            self.assertEqual(board_graph.main(argv + ["next", "--by", "agent:a", "--note", "reading"]), 2)
        view = self.view()
        attempt = view["state"]["nodes"]["inventory"]["attempt"]
        self.assertEqual((attempt["by"], attempt["action"], attempt["note"]), ("agent:a", "agent", "reading"))
        self.assertEqual([key for key, node in view["state"]["nodes"].items() if node["attempt"]], ["inventory"])
        self.assertEqual(view["where"]["tracks"][0]["node"]["working"][0]["run_id"], attempt["run_id"])
        [row] = view["log"]["rows"]
        self.assertEqual((row["kind"], row["actor"], row["outcome"], row["note"]), ("work", "agent:a", None, "reading"))
        self.assertEqual(view["log"]["counts"]["work"], 1)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(board_graph.main(argv + ["record", "inventory", "needs_lead", "--by", "agent:a",
                                                      "--attempt", attempt["run_id"], "--note", "stuck"]), 0)
        view = self.view()
        self.assertIsNone(view["state"]["nodes"]["inventory"]["attempt"])
        self.assertEqual(view["where"]["tracks"][0]["node"]["working"], [])
        self.assertEqual(view["where"]["tracks"][0]["node"]["waiting_since"], view["log"]["rows"][0]["at"])
        work = next(row for row in view["log"]["rows"] if row["kind"] == "work")
        self.assertEqual((work["outcome"], bool(work["finished_at"])), ("needs_lead", True))

    def test_a_snapshot_that_fails_never_fails_the_record(self):
        argv = ["--state", str(self.state.path), "--records", str(self.state.records), "--view", str(self.out),
                "record", "inventory", "more_work", "--by", "agent:test", "--note", "kept"]

        def broken(*args):
            raise RuntimeError("disk full")

        with mock.patch.object(board_graph_view, "quick_snapshot", broken), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(board_graph.main(argv), 0)
        self.assertEqual(err.getvalue(), "the view was not refreshed (RuntimeError: disk full); the state is written\n")
        self.assertEqual([row["note"] for row in self.state.transitions()], ["kept"])
        self.assertFalse((self.out / "view.json").exists())

    def test_on_the_mac_the_view_follows_the_one_database_and_no_other_unless_named(self):
        main = self.base / "main"
        served = main / "tools/board_graph_view/view.json"
        record = ["--records", str(self.state.records), "record", "inventory", "more_work", "--by", "agent:test",
                  "--note", "n"]
        with mock.patch.dict(os.environ), mock.patch.object(board_graph.mac, "LOCAL", True), \
                mock.patch.object(board_graph, "mac_checkout", lambda: main), redirect_stdout(io.StringIO()):
            os.environ.pop(board_graph.STATE_ENV, None)
            self.assertEqual(board_graph.main(["--state", str(self.state.path), *record]), 0)
            os.environ[board_graph.STATE_ENV] = str(self.state.path)
            self.assertEqual(board_graph.main(record), 0)
            self.assertFalse(served.exists())
            del os.environ[board_graph.STATE_ENV]
            self.assertEqual(board_graph.main(record), 0)
        view = json.loads(served.read_text(encoding="utf-8"))
        # The Mac's database imported the two records the others wrote (--records named the folder), then its own.
        self.assertEqual(view["state"]["nodes"]["inventory"]["transitions"], 3)
        self.assertEqual(len(State(main / "logs/board-graph/state.sqlite3", self.state.records).transitions()), 3)
        self.assertEqual(len(self.state.transitions()), 2)


class TellTests(Base):
    """After view.json is written the page server is told, so an open page reads it again; a server that could
    not be told is one line on stderr and never a failure; off the Mac there is no server and nothing is said."""

    def test_the_server_is_told_after_the_file_is_whole(self):
        seen = []

        def tell():
            seen.append(json.loads((self.base / "view" / "view.json").read_text(encoding="utf-8")))
            return None

        err = io.StringIO()
        with redirect_stderr(err):
            data = board_graph_view.write_view({"a": 1}, self.base / "view", tell=tell)
        self.assertEqual(json.loads(data.read_text(encoding="utf-8")), {"a": 1})
        self.assertEqual(seen, [{"a": 1}], "told after the whole file is in place")
        self.assertEqual(err.getvalue(), "")

    def test_a_server_that_could_not_be_told_is_one_line_and_the_file_stands(self):
        err = io.StringIO()
        with redirect_stderr(err):
            data = board_graph_view.write_view({"a": 2}, self.base / "view", tell=lambda: "no (gone)")
        self.assertEqual(json.loads(data.read_text(encoding="utf-8")), {"a": 2})
        self.assertEqual(err.getvalue(), f"the page was not told that {self.base / 'view'} changed (no (gone))\n")

    def test_the_macs_own_server_is_told_on_the_mac_for_the_served_folder_only(self):
        import board_graph_serve
        served = self.base / "tools" / "board_graph_view"
        served.mkdir(parents=True)
        err = io.StringIO()
        with mock.patch.object(mac, "LOCAL", False), redirect_stderr(err), \
                mock.patch.object(board_graph_serve, "notify", side_effect=AssertionError("not on the Mac")):
            self.assertIsNone(board_graph_view.told(served))
        with mock.patch.object(mac, "LOCAL", True), mock.patch.object(mac, "CHECKOUT", str(self.base)), \
                redirect_stderr(err), \
                mock.patch.object(board_graph_serve, "notify", return_value="HTTP 403") as notify:
            self.assertIsNone(board_graph_view.told(self.base / "elsewhere"), "a folder no page reads")
            notify.assert_not_called()
            self.assertEqual(board_graph_view.told(served), "HTTP 403")
        notify.assert_called_once_with()
        self.assertEqual(err.getvalue(), f"the page was not told that {served} changed (HTTP 403)\n")


if __name__ == "__main__":
    unittest.main()
